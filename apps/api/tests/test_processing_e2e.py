"""DI-1 end to end through the real parser sandbox: upload -> start -> worker -> result.

Mandatory under GitHub Actions or with REQUIRE_PARSER_SANDBOX=1 (as
test_parser_sandbox.py); needs PARSER_IMAGE (a locally built parser image).
Dev corpus cases only (D-063): held-out cases are measured by `make eval` alone.
The pipeline version names the image's real id, which the worker verifies.
"""

import json
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import quote
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from api.database import DatabaseSettings
from api.main import create_app
from api.processing import ProcessingConfigError
from api.sandbox import SandboxSettings, image_id
from api.worker import Worker, build_worker
from scripts.eval_scoring import Actual, score_case
from tests.conftest import SeededTenant
from tests.test_parser_sandbox import DOCKER, IMAGE, real
from tests.test_workspace_endpoints import Api, _environ, _fetcher, _subject
from tests.token_issuer import LocalIssuer

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "corpus"))

from cases import CASES  # noqa: E402

EXPECTED = ROOT / "corpus" / "expected" / "extract"
E2E_CASES = (
    "dev-n1-labelled",
    "dev-c1-conflict-date",
    "dev-a1-ambiguous-date",
    "dev-x5-embedded-file",
    "dev-m3-image-only",
)
pytestmark = real


@pytest.fixture(scope="module")
def parser_image_id() -> str:
    if not IMAGE:
        pytest.fail("PARSER_IMAGE is not set; the real parser sandbox is mandatory here")
    found = image_id(SandboxSettings(image=IMAGE, docker=DOCKER))
    assert found is not None, "the parser image is not available locally"
    return found


@pytest.fixture
def api(
    runtime_settings: DatabaseSettings, owner_engine: Engine, parser_image_id: str
) -> Iterator[Api]:
    issuer = LocalIssuer()
    app = create_app(
        _environ(runtime_settings, PARSER_IMAGE_ID=parser_image_id), jwks_fetcher=_fetcher(issuer)
    )
    with TestClient(app) as client:
        yield Api(client, issuer, owner_engine)


@pytest.fixture
def worker(worker_settings: DatabaseSettings, parser_image_id: str) -> Iterator[Worker]:
    # Built exactly as `python -m api.worker` builds it: verifies the image id first.
    built, engine = build_worker(
        {
            "DATABASE_URL_WORKER": worker_settings.url.render_as_string(hide_password=False),
            "PARSER_IMAGE_ID": parser_image_id,
            "PARSER_DOCKER": DOCKER,
        }
    )
    yield built
    engine.dispose()


def _process(
    api: Api, tenant: SeededTenant, subject: str, content: bytes, worker: Worker
) -> dict[str, object]:
    docs = f"/v1/tenants/{tenant.ctx.tenant_id}/workspaces/{tenant.workspace_id}/documents"
    headers = api.headers(subject) | {
        "Content-Type": "application/pdf",
        "X-Document-Name": quote("case.pdf", safe=""),
    }
    uploaded = api.request("POST", docs, None, headers=headers, content=content)
    assert uploaded.status_code == 201, uploaded.text
    path = f"{docs}/{uploaded.json()['id']}/processing"
    assert api.request("POST", path, subject).status_code == 202
    assert worker.run_once()
    body = api.request("GET", path, subject).json()
    assert isinstance(body, dict)
    return body


def _pages(owner_engine: Engine, run_id: UUID) -> dict[int, str]:
    with owner_engine.connect() as connection:
        return dict(
            connection.execute(
                text("SELECT page_number, text FROM document_pages WHERE run_id = :r"),
                {"r": run_id},
            ).all()
        )


@pytest.mark.parametrize("case_id", E2E_CASES)
def test_dev_case_end_to_end_through_the_real_sandbox(
    case_id: str,
    api: Api,
    worker: Worker,
    seed_tenant: Callable[..., SeededTenant],
    owner_engine: Engine,
    parser_image_id: str,
) -> None:
    case = next(c for c in CASES if c.id == case_id)
    expected = json.loads((EXPECTED / f"{case_id}.json").read_text())
    assert expected["split"] == "dev"
    tenant = seed_tenant()
    subject = _subject()
    api.grant(subject, tenant.ctx.tenant_id)

    body = _process(api, tenant, subject, case.make(), worker)
    run = body["run"]
    assert isinstance(run, dict)
    assert run["pipeline_version"].endswith(f"+parser@{parser_image_id}")
    assert run["attempts"] == 1
    if expected["outcome"] == "failed":
        assert (run["status"], run["failure_code"], body["result"]) == (
            "failed",
            expected["failure_code"],
            None,
        )
        return
    assert run["status"] == expected["outcome"]
    pages = _pages(owner_engine, UUID(run["id"]))
    result = body["result"]
    assert isinstance(result, dict)
    score = score_case(expected, Actual(failure_code=None, result=result, pages=pages))
    assert score.problems == []
    assert score.fields_correct == score.fields_total
    assert score.evidence_covered == score.evidence_expected
    assert score.spans_resolved == score.spans_total and score.unsupported == 0


def test_the_worker_refuses_an_image_id_docker_does_not_have(
    worker_settings: DatabaseSettings,
) -> None:
    with pytest.raises(ProcessingConfigError, match="Docker does not have"):
        build_worker(
            {
                "DATABASE_URL_WORKER": worker_settings.url.render_as_string(hide_password=False),
                "PARSER_IMAGE_ID": "sha256:" + "0" * 64,
                "PARSER_DOCKER": DOCKER,
            }
        )
