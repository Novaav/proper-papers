"""Processing endpoints end to end: real app, PostgreSQL (app_test), local issuer, fake sandbox.

POST/GET /v1/tenants/{t}/workspaces/{w}/documents/{d}/processing. The worker
runs with a fake sandbox here; the real one is tests/test_processing_e2e.py.
"""

import threading
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import quote
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from api.database import (
    DatabaseSettings,
    create_runtime_engine,
    create_session_factory,
    tenant_transaction,
)
from api.main import create_app
from api.processing import ProcessingConfigError, pipeline_version, start_processing
from api.sandbox import ParseFailed
from api.worker import Worker
from tests.conftest import SeededTenant
from tests.processing_support import PDF, Seeded, block_version
from tests.test_worker import SANDBOX, SETTINGS, TEXT, parsed
from tests.test_workspace_endpoints import Api, _environ, _fetcher, _subject
from tests.token_issuer import LocalIssuer

Seed = Callable[..., SeededTenant]
NOT_FOUND = {"detail": "not_found"}


@pytest.fixture
def image() -> str:
    """A parser image id of this test only, so its queue holds only its own runs."""
    return "sha256:" + uuid.uuid4().hex * 2


@pytest.fixture
def issuer() -> LocalIssuer:
    return LocalIssuer()


def _api(
    runtime_settings: DatabaseSettings, issuer: LocalIssuer, owner_engine: Engine, **env: str
) -> Iterator[Api]:
    app = create_app(_environ(runtime_settings, **env), jwks_fetcher=_fetcher(issuer))
    with TestClient(app) as client:
        api = Api(client, issuer, owner_engine)
        yield api
    assert [r for r in api.responses if r.headers.get("cache-control") != "no-store"] == []


@pytest.fixture
def api(
    runtime_settings: DatabaseSettings, issuer: LocalIssuer, owner_engine: Engine, image: str
) -> Iterator[Api]:
    yield from _api(runtime_settings, issuer, owner_engine, PARSER_IMAGE_ID=image)


@pytest.fixture
def member(api: Api, seed_tenant: Seed) -> tuple[SeededTenant, str, UUID]:
    """A member and a clean document uploaded through the API."""
    tenant = seed_tenant()
    subject = _subject()
    api.grant(subject, tenant.ctx.tenant_id)
    headers = api.headers(subject) | {
        "Content-Type": "application/pdf",
        "X-Document-Name": quote("contract.pdf", safe=""),
    }
    uploaded = api.request("POST", _docs(tenant), None, headers=headers, content=PDF)
    assert uploaded.status_code == 201
    return tenant, subject, UUID(uploaded.json()["id"])


def _docs(tenant: SeededTenant) -> str:
    return f"/v1/tenants/{tenant.ctx.tenant_id}/workspaces/{tenant.workspace_id}/documents"


def _processing(tenant: SeededTenant, document: object) -> str:
    return f"{_docs(tenant)}/{document}/processing"


def _worker(worker_sessions: sessionmaker[Session], image: str, outcome: Any) -> Worker:
    return Worker(
        worker_sessions,
        sandbox=SANDBOX,
        pipeline=pipeline_version(image),
        settings=SETTINGS,
        parse=lambda _content, _settings: outcome,
    )


def test_start_is_202_then_200_with_the_same_run(
    api: Api, member: tuple[SeededTenant, str, UUID], image: str
) -> None:
    tenant, subject, document = member
    first = api.request("POST", _processing(tenant, document), subject)
    assert first.status_code == 202
    run = first.json()["run"]
    assert (run["status"], run["attempts"], run["failure_code"]) == ("queued", 0, None)
    assert run["pipeline_version"] == f"service_agreement.v1+rules-1+parser@{image}"
    again = api.request("POST", _processing(tenant, document), subject)
    assert again.status_code == 200
    assert again.json()["run"]["id"] == run["id"]


def test_nothing_started_is_200_with_a_null_run(
    api: Api, member: tuple[SeededTenant, str, UUID]
) -> None:
    tenant, subject, document = member
    got = api.request("GET", _processing(tenant, document), subject)
    assert (got.status_code, got.json()) == (200, {"run": None, "result": None})


def test_a_completed_run_returns_its_result_with_exact_evidence(
    api: Api,
    member: tuple[SeededTenant, str, UUID],
    worker_sessions: sessionmaker[Session],
    image: str,
) -> None:
    tenant, subject, document = member
    api.request("POST", _processing(tenant, document), subject)
    queued = api.request("GET", _processing(tenant, document), subject).json()
    assert (queued["run"]["status"], queued["result"]) == ("queued", None)
    assert _worker(worker_sessions, image, parsed(TEXT)).run_once()
    body = api.request("GET", _processing(tenant, document), subject).json()
    assert body["run"]["status"] == "completed"
    result = body["result"]
    assert (result["schema"], result["extractor"], result["outcome"]) == (
        "service_agreement.v1",
        "rules-1",
        "completed",
    )
    provider = result["fields"]["provider_name"]
    assert provider["status"] == "found"
    evidence = provider["candidates"][0]["evidence"][0]
    assert TEXT[evidence["start"] : evidence["end"]] == evidence["quote"]
    assert result["fields"]["end_date"] == {
        "status": "unknown",
        "reason": "not_found",
        "candidates": [],
    }


def test_a_failed_run_has_its_code_and_no_result_and_is_not_requeued(
    api: Api,
    member: tuple[SeededTenant, str, UUID],
    worker_sessions: sessionmaker[Session],
    image: str,
) -> None:
    tenant, subject, document = member
    api.request("POST", _processing(tenant, document), subject)
    _worker(worker_sessions, image, ParseFailed("no_text_layer")).run_once()
    body = api.request("GET", _processing(tenant, document), subject).json()
    assert (body["run"]["status"], body["run"]["failure_code"], body["result"]) == (
        "failed",
        "no_text_layer",
        None,
    )
    again = api.request("POST", _processing(tenant, document), subject)
    assert (again.status_code, again.json()["run"]["status"]) == (200, "failed")


def test_not_clean_versions_are_409_for_both(
    api: Api,
    member: tuple[SeededTenant, str, UUID],
    owner_engine: Engine,
    worker_sessions: sessionmaker[Session],
    image: str,
) -> None:
    tenant, subject, document = member
    api.request("POST", _processing(tenant, document), subject)
    _worker(worker_sessions, image, parsed(TEXT)).run_once()
    version = api.request("GET", f"{_docs(tenant)}/{document}", subject).json()["version"]["id"]
    block_version(owner_engine, UUID(version), tenant.ctx.user_id)
    for method in ("POST", "GET"):
        refused = api.request(method, _processing(tenant, document), subject)
        assert (refused.status_code, refused.json()) == (409, {"detail": "blocked"}), method


def test_non_members_unknown_and_malformed_ids_are_404(
    api: Api, member: tuple[SeededTenant, str, UUID], seed_tenant: Seed
) -> None:
    tenant, subject, document = member
    stranger = _subject()
    other = seed_tenant()
    api.grant(stranger, other.ctx.tenant_id)
    for method in ("POST", "GET"):
        for path, who in (
            (_processing(tenant, document), stranger),  # not a member
            (_processing(other, document), stranger),  # the document is in another tenant
            (_processing(tenant, uuid.uuid4()), subject),  # unknown document
            (_processing(tenant, "not-a-uuid"), subject),
        ):
            got = api.request(method, path, who)
            assert (got.status_code, got.json()) == (404, NOT_FOUND), (method, path)
    assert api.request("GET", _processing(tenant, document), subject).json()["run"] is None


def test_no_token_is_401(api: Api, member: tuple[SeededTenant, str, UUID]) -> None:
    tenant, _, document = member
    for method in ("POST", "GET"):
        assert api.request(method, _processing(tenant, document), None).status_code == 401


def test_without_a_parser_image_processing_cannot_start(
    runtime_settings: DatabaseSettings, issuer: LocalIssuer, owner_engine: Engine, seed_tenant: Seed
) -> None:
    for api in _api(runtime_settings, issuer, owner_engine):
        tenant = seed_tenant()
        subject = _subject()
        api.grant(subject, tenant.ctx.tenant_id)
        headers = api.headers(subject) | {
            "Content-Type": "application/pdf",
            "X-Document-Name": "a.pdf",
        }
        document = api.request("POST", _docs(tenant), None, headers=headers, content=PDF).json()[
            "id"
        ]
        refused = api.request("POST", _processing(tenant, document), subject)
        assert (refused.status_code, refused.json()) == (503, {"detail": "processing_unavailable"})
        assert api.request("GET", _processing(tenant, document), subject).status_code == 200


def test_a_malformed_parser_image_id_stops_the_api(
    runtime_settings: DatabaseSettings, issuer: LocalIssuer
) -> None:
    app = create_app(
        _environ(runtime_settings, PARSER_IMAGE_ID="parser:latest"), jwks_fetcher=_fetcher(issuer)
    )
    with pytest.raises(ProcessingConfigError), TestClient(app):
        pass


def test_concurrent_starts_create_one_run(
    seed_document: Callable[..., Seeded], runtime_settings: DatabaseSettings, image: str
) -> None:
    seeded = seed_document()
    engines = [create_runtime_engine(runtime_settings) for _ in range(8)]
    barrier = threading.Barrier(len(engines))

    def start(engine: Engine) -> tuple[UUID, bool]:
        sessions = create_session_factory(engine)
        barrier.wait()
        with tenant_transaction(sessions, seeded.tenant.ctx) as session:
            started = start_processing(
                session,
                seeded.tenant.workspace_id,
                seeded.document.id,
                pipeline=pipeline_version(image),
                user_id=seeded.tenant.ctx.user_id,
            )
        assert started is not None
        return started[0].id, started[1]

    try:
        with ThreadPoolExecutor(len(engines)) as pool:
            results = list(pool.map(start, engines))
    finally:
        for engine in engines:
            engine.dispose()
    assert len({run_id for run_id, _ in results}) == 1
    assert sum(created for _, created in results) == 1
