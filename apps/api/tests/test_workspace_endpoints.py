"""Tenant workspace endpoints end to end: real app, PostgreSQL 18 (app_test), local issuer.

Users are provisioned through the API; memberships are granted by the schema
owner, as an operator would.
"""

import logging
import time
from collections.abc import Callable, Iterator
from typing import Any
from uuid import UUID, uuid4

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from api import main
from api.auth import JwksFetcher
from api.authorization import authorize_tenant
from api.database import DatabaseSettings
from api.main import MAX_BODY_BYTES, create_app
from tests import fake_clamd
from tests.conftest import SeededTenant
from tests.token_issuer import AUDIENCE, CLIENT_ID, ISSUER, LocalIssuer

Seed = Callable[..., SeededTenant]
NOT_FOUND = {"detail": "not_found"}
INVALID_NAME = {"detail": "invalid_name"}
UNAUTHORIZED = {"detail": "unauthorized"}
UNAVAILABLE = {"detail": "unavailable"}
# Valid JSON with a valid name, one byte over the limit (padding is whitespace).
OVERSIZED = b'{"name": "ok"' + b" " * (MAX_BODY_BYTES - 13) + b"}"
MALFORMED_IDS = [
    "not-a-uuid",
    "00000000-0000-0000-0000-000000000000",
    "0192a8f0-7c1e-7b3a-9c2d-4e5f6a7b8c9",  # one digit short
    "0192a8f0-7c1e-7b3a-9c2d-4e5f6a7b8c9d0",  # one digit long
    "0192A8F0-7C1E-7B3A-9C2D-4E5F6A7B8C9D",  # upper case
    "{0192a8f0-7c1e-7b3a-9c2d-4e5f6a7b8c9d}",
    "urn:uuid:0192a8f0-7c1e-7b3a-9c2d-4e5f6a7b8c9d",
    "0192a8f07c1e7b3a9c2d4e5f6a7b8c9d",
    " 0192a8f0-7c1e-7b3a-9c2d-4e5f6a7b8c9d",
    "0192a8f0-7c1e-7b3a-9c2d-4e5f6a7b8c9\u0661",  # Arabic-Indic digit one
    "x" * 5000,
    "'; DROP TABLE workspaces; --",
]


class Api:
    """A test client plus helpers to act as named users."""

    def __init__(self, client: TestClient, issuer: LocalIssuer, owner_engine: Engine) -> None:
        self.client = client
        self.issuer = issuer
        self.owner_engine = owner_engine
        self.responses: list[httpx.Response] = []

    def headers(self, subject: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.issuer.token(sub=subject)}"}

    def request(self, method: str, path: str, subject: str | None, **kwargs: Any) -> httpx.Response:
        headers = kwargs.pop("headers", None) or ({} if subject is None else self.headers(subject))
        response: httpx.Response = self.client.request(method, path, headers=headers, **kwargs)
        self.responses.append(response)
        return response

    def list(self, tenant: object, subject: str | None) -> httpx.Response:
        return self.request("GET", f"/v1/tenants/{tenant}/workspaces", subject)

    def create(
        self, tenant: object, subject: str | None, body: Any = None, **kw: Any
    ) -> httpx.Response:
        if body is not None:
            kw["json"] = body
        return self.request("POST", f"/v1/tenants/{tenant}/workspaces", subject, **kw)

    def get(self, tenant: object, workspace: object, subject: str | None) -> httpx.Response:
        return self.request("GET", f"/v1/tenants/{tenant}/workspaces/{workspace}", subject)

    def user(self, subject: str) -> UUID:
        """Provision `subject` through GET /v1/me and return its user id."""
        response = self.request("GET", "/v1/me", subject)
        assert response.status_code == 200
        return UUID(response.json()["user_id"])

    def grant(self, subject: str, tenant_id: UUID, role: str = "member") -> UUID:
        user_id = self.user(subject)
        with self.owner_engine.begin() as connection:
            connection.execute(
                text("INSERT INTO memberships (tenant_id, user_id, role) VALUES (:t, :u, :r)"),
                {"t": tenant_id, "u": user_id, "r": role},
            )
        return user_id

    def revoke(self, subject: str, tenant_id: UUID) -> None:
        user_id = self.user(subject)
        with self.owner_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM memberships WHERE tenant_id = :t AND user_id = :u"),
                {"t": tenant_id, "u": user_id},
            )

    def workspace_count(self, tenant_id: UUID) -> int:
        with self.owner_engine.connect() as connection:
            count: int = connection.execute(
                text("SELECT count(*) FROM workspaces WHERE tenant_id = :t"), {"t": tenant_id}
            ).scalar_one()
        return count


def _environ(runtime_settings: DatabaseSettings, **overrides: str) -> dict[str, str]:
    env = {
        "DATABASE_URL": runtime_settings.url.render_as_string(hide_password=False),
        "AUTH_ISSUER": ISSUER,
        "AUTH_AUDIENCE": AUDIENCE,
        "AUTH_ALLOWED_CLIENT_IDS": CLIENT_ID,
        "CLAMD_ADDRESS": fake_clamd.shared().address,
    }
    env.update(overrides)
    return env


@pytest.fixture
def issuer() -> LocalIssuer:
    return LocalIssuer()


def _fetcher(issuer: LocalIssuer) -> Callable[[str], JwksFetcher]:
    return lambda _url: issuer.jwks


@pytest.fixture
def api(
    runtime_settings: DatabaseSettings, issuer: LocalIssuer, owner_engine: Engine
) -> Iterator[Api]:
    app = create_app(_environ(runtime_settings), jwks_fetcher=_fetcher(issuer))
    with TestClient(app) as client:
        api = Api(client, issuer, owner_engine)
        yield api
    # Every response of every test here, success or error, forbids caching.
    assert [r for r in api.responses if r.headers.get("cache-control") != "no-store"] == []


def _subject() -> str:
    return f"auth0|user-{uuid4()}"


def _comparable(response: httpx.Response) -> tuple[int, Any, dict[str, str]]:
    headers = {k: v for k, v in response.headers.items() if k not in ("date", "content-length")}
    return response.status_code, response.json(), headers


# --- Members --------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["owner", "member"])
def test_members_list_create_and_get_workspaces(api: Api, seed_tenant: Seed, role: str) -> None:
    tenant = seed_tenant()
    tenant_id = tenant.ctx.tenant_id
    alice = _subject()
    api.grant(alice, tenant_id, role)

    listed = api.list(tenant_id, alice)
    assert listed.status_code == 200
    assert [w["id"] for w in listed.json()["workspaces"]] == [str(tenant.workspace_id)]
    assert set(listed.json()["workspaces"][0]) == {"id", "name", "created_at"}

    created = api.create(tenant_id, alice, {"name": "Contracts"})
    assert created.status_code == 201
    body = created.json()
    assert set(body) == {"id", "name", "created_at"} and body["name"] == "Contracts"
    assert created.headers["location"] == f"/v1/tenants/{tenant_id}/workspaces/{body['id']}"

    fetched = api.request("GET", created.headers["location"], alice)
    assert (fetched.status_code, fetched.json()) == (200, body)
    listed_again = api.list(tenant_id, alice).json()["workspaces"]
    assert [w["id"] for w in listed_again] == [str(tenant.workspace_id), body["id"]]
    assert api.workspace_count(tenant_id) == 2


def test_names_are_stored_as_given(api: Api, seed_tenant: Seed) -> None:
    tenant_id = seed_tenant().ctx.tenant_id
    alice = _subject()
    api.grant(alice, tenant_id)

    for name in ["  padded  ", "x" * 200, "\U0001f600" * 200, "<script>alert(1)</script>"]:
        created = api.create(tenant_id, alice, {"name": name})
        assert (created.status_code, created.json()["name"]) == (201, name)


def test_two_users_in_different_tenants_are_isolated(api: Api, seed_tenant: Seed) -> None:
    t1, t2 = seed_tenant(), seed_tenant()
    alice, bob = _subject(), _subject()
    api.grant(alice, t1.ctx.tenant_id)
    api.grant(bob, t2.ctx.tenant_id)
    a_ws = api.create(t1.ctx.tenant_id, alice, {"name": "alice"}).json()["id"]
    b_ws = api.create(t2.ctx.tenant_id, bob, {"name": "bob"}).json()["id"]

    alice_ids = {w["id"] for w in api.list(t1.ctx.tenant_id, alice).json()["workspaces"]}
    bob_ids = {w["id"] for w in api.list(t2.ctx.tenant_id, bob).json()["workspaces"]}
    assert alice_ids == {str(t1.workspace_id), a_ws}
    assert bob_ids == {str(t2.workspace_id), b_ws}


def test_member_of_two_tenants_sees_each_tenant_only_under_its_path(
    api: Api, seed_tenant: Seed
) -> None:
    t1, t2 = seed_tenant(), seed_tenant()
    alice = _subject()
    api.grant(alice, t1.ctx.tenant_id, "owner")
    api.grant(alice, t2.ctx.tenant_id, "member")

    assert [w["id"] for w in api.list(t1.ctx.tenant_id, alice).json()["workspaces"]] == [
        str(t1.workspace_id)
    ]
    # A workspace of tenant 2 under tenant 1's path is not found, even for a member of both.
    assert _comparable(api.get(t1.ctx.tenant_id, t2.workspace_id, alice))[:2] == (404, NOT_FOUND)
    assert api.get(t2.ctx.tenant_id, t2.workspace_id, alice).status_code == 200


# --- Non-members, unknown and malformed IDs: one 404 ----------------------------------------


def test_non_member_gets_404_on_every_endpoint_and_nothing_is_created(
    api: Api, seed_tenant: Seed
) -> None:
    mine, theirs = seed_tenant(), seed_tenant()
    alice = _subject()
    api.grant(alice, mine.ctx.tenant_id)
    t2 = theirs.ctx.tenant_id

    responses = [
        api.list(t2, alice),
        api.create(t2, alice, {"name": "intrusion"}),
        api.get(t2, theirs.workspace_id, alice),
        api.get(t2, uuid4(), alice),
    ]

    assert [(r.status_code, r.json()) for r in responses] == [(404, NOT_FOUND)] * 4
    assert api.workspace_count(t2) == 1


def test_user_with_no_memberships_gets_404(api: Api, seed_tenant: Seed) -> None:
    tenant = seed_tenant()
    newcomer = _subject()  # provisioned by this very request

    assert _comparable(api.list(tenant.ctx.tenant_id, newcomer))[:2] == (404, NOT_FOUND)
    assert api.user(newcomer)  # the user exists now, still without access


def test_non_member_unknown_and_malformed_tenants_are_indistinguishable(
    api: Api, seed_tenant: Seed
) -> None:
    mine, theirs = seed_tenant(), seed_tenant()
    alice = _subject()
    api.grant(alice, mine.ctx.tenant_id)

    for method in ("list", "create", "get"):
        variants = []
        for tenant in (theirs.ctx.tenant_id, uuid4(), "not-a-uuid"):
            if method == "list":
                variants.append(_comparable(api.list(tenant, alice)))
            elif method == "create":
                variants.append(_comparable(api.create(tenant, alice, {"name": "x"})))
            else:
                variants.append(_comparable(api.get(tenant, theirs.workspace_id, alice)))
        assert variants[0][:2] == (404, NOT_FOUND)
        assert variants[0] == variants[1] == variants[2], method


@pytest.mark.parametrize("malformed", MALFORMED_IDS)
def test_malformed_tenant_ids_are_not_found(api: Api, seed_tenant: Seed, malformed: str) -> None:
    tenant = seed_tenant()
    alice = _subject()
    api.grant(alice, tenant.ctx.tenant_id)

    for response in (
        api.list(malformed, alice),
        api.create(malformed, alice, {"name": "x"}),
        api.get(malformed, tenant.workspace_id, alice),
    ):
        assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert api.workspace_count(tenant.ctx.tenant_id) == 1


@pytest.mark.parametrize("malformed", MALFORMED_IDS)
def test_malformed_workspace_ids_are_not_found(api: Api, seed_tenant: Seed, malformed: str) -> None:
    tenant = seed_tenant()
    alice = _subject()
    api.grant(alice, tenant.ctx.tenant_id)

    response = api.get(tenant.ctx.tenant_id, malformed, alice)
    assert (response.status_code, response.json()) == (404, NOT_FOUND)


def test_upper_case_form_of_a_real_tenant_id_is_not_an_alias(api: Api, seed_tenant: Seed) -> None:
    tenant = seed_tenant()
    alice = _subject()
    api.grant(alice, tenant.ctx.tenant_id)

    upper = str(tenant.ctx.tenant_id).upper()
    assert api.list(upper, alice).status_code == 404
    assert api.get(tenant.ctx.tenant_id, str(tenant.workspace_id).upper(), alice).status_code == 404


def test_unknown_and_other_tenants_workspace_ids_are_indistinguishable(
    api: Api, seed_tenant: Seed
) -> None:
    mine, theirs = seed_tenant(), seed_tenant()
    alice = _subject()
    api.grant(alice, mine.ctx.tenant_id)

    other_tenants = _comparable(api.get(mine.ctx.tenant_id, theirs.workspace_id, alice))
    unknown = _comparable(api.get(mine.ctx.tenant_id, uuid4(), alice))
    malformed = _comparable(api.get(mine.ctx.tenant_id, "nope", alice))

    assert other_tenants[:2] == (404, NOT_FOUND)
    assert other_tenants == unknown == malformed


# --- Revocation -----------------------------------------------------------------------------


def test_revoked_membership_gets_404_on_the_next_request(api: Api, seed_tenant: Seed) -> None:
    tenant = seed_tenant()
    tenant_id = tenant.ctx.tenant_id
    alice = _subject()
    api.grant(alice, tenant_id)
    assert api.list(tenant_id, alice).status_code == 200

    api.revoke(alice, tenant_id)

    assert (api.list(tenant_id, alice).status_code, api.list(tenant_id, alice).json()) == (
        404,
        NOT_FOUND,
    )
    assert api.create(tenant_id, alice, {"name": "late"}).status_code == 404
    assert api.get(tenant_id, tenant.workspace_id, alice).status_code == 404
    assert api.workspace_count(tenant_id) == 1


def _revoke_after_authorization(
    monkeypatch: pytest.MonkeyPatch, api: Api, subject: str, tenant_id: UUID
) -> None:
    """Revoke the membership between the authorization and the workspace transaction."""
    real = authorize_tenant

    def authorize_then_revoke(*args: Any, **kwargs: Any) -> Any:
        ctx = real(*args, **kwargs)
        with api.owner_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM memberships WHERE tenant_id = :t"), {"t": tenant_id}
            )
        return ctx

    monkeypatch.setattr(main, "authorize_tenant", authorize_then_revoke)


def test_membership_revoked_after_authorization_cannot_create(
    api: Api, seed_tenant: Seed, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Row-level security re-checks the membership (D-A6): the write is rejected
    # and the caller gets the ordinary 404.
    tenant = seed_tenant()
    tenant_id = tenant.ctx.tenant_id
    alice = _subject()
    api.grant(alice, tenant_id)
    _revoke_after_authorization(monkeypatch, api, alice, tenant_id)

    with caplog.at_level(logging.INFO):
        response = api.create(tenant_id, alice, {"name": "raced"})

    assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert api.workspace_count(tenant_id) == 1
    assert "rejected by row-level security" in caplog.text


def test_membership_revoked_after_authorization_reads_nothing(
    api: Api, seed_tenant: Seed, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = seed_tenant()
    tenant_id = tenant.ctx.tenant_id
    alice = _subject()
    api.grant(alice, tenant_id)
    _revoke_after_authorization(monkeypatch, api, alice, tenant_id)

    listed = api.list(tenant_id, alice)
    assert (listed.status_code, listed.json()) == (200, {"workspaces": []})
    api.grant(alice, tenant_id)
    fetched = api.get(tenant_id, tenant.workspace_id, alice)
    assert (fetched.status_code, fetched.json()) == (404, NOT_FOUND)


# --- Invalid names --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"name": ""},
        {"name": "   "},
        {"name": "x" * 201},
        {"name": "a\x00b"},
        {"name": 123},
        {"name": None},
        {"name": ["x"]},
        {"name": True},
        {},
        {"name": "ok", "tenant_id": "00000000-0000-0000-0000-000000000001"},
        {"Name": "ok"},
        [],
        "ok",
        None,
    ],
)
def test_invalid_bodies_get_a_fixed_422(api: Api, seed_tenant: Seed, body: Any) -> None:
    tenant_id = seed_tenant().ctx.tenant_id
    alice = _subject()
    api.grant(alice, tenant_id)

    kwargs: dict[str, Any] = {"content": b"null"} if body is None else {"json": body}
    response = api.create(tenant_id, alice, **kwargs)

    assert (response.status_code, response.json()) == (422, INVALID_NAME)
    assert api.workspace_count(tenant_id) == 1


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"not json",
        b'{"name": "x"',
        b"\xff\xfe",
        b'{"name": "x"} trailing',
        OVERSIZED,
    ],
    ids=["empty", "not-json", "truncated", "not-utf8", "trailing", "oversized-but-valid"],
)
def test_unparseable_bodies_get_a_fixed_422(api: Api, seed_tenant: Seed, content: bytes) -> None:
    tenant_id = seed_tenant().ctx.tenant_id
    alice = _subject()
    api.grant(alice, tenant_id)

    response = api.create(
        tenant_id,
        alice,
        content=content,
        headers={**api.headers(alice), "Content-Type": "application/json"},
    )

    assert (response.status_code, response.json()) == (422, INVALID_NAME)
    assert api.workspace_count(tenant_id) == 1


def test_oversized_body_without_a_length_header_is_rejected(api: Api, seed_tenant: Seed) -> None:
    tenant_id = seed_tenant().ctx.tenant_id
    alice = _subject()
    api.grant(alice, tenant_id)

    def chunks() -> Iterator[bytes]:
        # Valid JSON with a valid name: only the size limit can reject it.
        yield b'{"name": "ok"'
        for _ in range(4):
            yield b" " * MAX_BODY_BYTES
        yield b"}"

    response = api.create(tenant_id, alice, content=chunks())

    assert (response.status_code, response.json()) == (422, INVALID_NAME)
    assert api.workspace_count(tenant_id) == 1


def test_body_of_exactly_the_limit_is_accepted(api: Api, seed_tenant: Seed) -> None:
    tenant_id = seed_tenant().ctx.tenant_id
    alice = _subject()
    api.grant(alice, tenant_id)
    at_limit = OVERSIZED[:-2] + b"}"
    assert len(at_limit) == MAX_BODY_BYTES

    assert api.create(tenant_id, alice, content=at_limit).status_code == 201


def test_body_is_checked_only_after_authentication_and_authorization(
    api: Api, seed_tenant: Seed
) -> None:
    mine, theirs = seed_tenant(), seed_tenant()
    alice = _subject()
    api.grant(alice, mine.ctx.tenant_id)
    oversized = OVERSIZED

    assert api.create(mine.ctx.tenant_id, None, content=b"not json").status_code == 401
    assert api.create(theirs.ctx.tenant_id, alice, content=b"not json").status_code == 404
    assert api.create(theirs.ctx.tenant_id, alice, content=oversized).status_code == 404
    assert api.create("bad-id", alice, {"name": ""}).status_code == 404
    assert api.create(mine.ctx.tenant_id, alice, {"name": ""}).status_code == 422


# --- Authentication and availability --------------------------------------------------------


def test_every_endpoint_requires_a_valid_token(api: Api, seed_tenant: Seed) -> None:
    tenant = seed_tenant()
    tenant_id = tenant.ctx.tenant_id
    expired = {"Authorization": f"Bearer {api.issuer.token(exp=int(time.time()) - 60)}"}

    for headers in ({}, expired, {"Authorization": "Bearer not-a-token"}):
        for response in (
            api.request(
                "GET", f"/v1/tenants/{tenant_id}/workspaces", None, headers=headers or None
            ),
            api.request(
                "POST",
                f"/v1/tenants/{tenant_id}/workspaces",
                None,
                headers=headers or None,
                json={"name": "x"},
            ),
            api.request(
                "GET",
                f"/v1/tenants/{tenant_id}/workspaces/{tenant.workspace_id}",
                None,
                headers=headers or None,
            ),
        ):
            assert (response.status_code, response.json()) == (401, UNAUTHORIZED)
            assert response.headers["www-authenticate"].startswith("Bearer")
    assert api.workspace_count(tenant_id) == 1


def test_database_outage_gives_503_after_authentication(
    runtime_settings: DatabaseSettings, issuer: LocalIssuer, caplog: pytest.LogCaptureFixture
) -> None:
    env = _environ(
        runtime_settings, DATABASE_URL="postgresql://app_runtime:pw@127.0.0.1:1/app_test"
    )
    app = create_app(env, jwks_fetcher=_fetcher(issuer))
    token = issuer.token(sub=_subject())
    tenant = uuid4()

    with caplog.at_level(logging.DEBUG), TestClient(app) as client:
        headers = {"Authorization": f"Bearer {token}"}
        responses = [
            client.get(f"/v1/tenants/{tenant}/workspaces", headers=headers),
            client.post(f"/v1/tenants/{tenant}/workspaces", headers=headers, json={"name": "x"}),
            client.get(f"/v1/tenants/{tenant}/workspaces/{uuid4()}", headers=headers),
            client.get(f"/v1/tenants/{tenant}/workspaces"),
        ]

    assert [(r.status_code, r.json()) for r in responses] == [(503, UNAVAILABLE)] * 3 + [
        (401, UNAUTHORIZED)
    ]
    assert all(r.headers["cache-control"] == "no-store" for r in responses)
    assert "authorize tenant failed: OperationalError" in caplog.text
    assert "pw" not in caplog.text and token not in caplog.text


def test_database_failure_in_the_workspace_transaction_gives_503(
    api: Api, seed_tenant: Seed, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id = seed_tenant().ctx.tenant_id
    alice = _subject()
    api.grant(alice, tenant_id)

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        from sqlalchemy.exc import OperationalError

        raise OperationalError("SELECT 1", {}, Exception("connection lost"))

    monkeypatch.setattr(main, "list_workspaces", broken)
    monkeypatch.setattr(main, "create_workspace", broken)
    monkeypatch.setattr(main, "get_workspace", broken)

    assert (api.list(tenant_id, alice).status_code, api.list(tenant_id, alice).json()) == (
        503,
        UNAVAILABLE,
    )
    assert api.create(tenant_id, alice, {"name": "x"}).status_code == 503
    assert api.get(tenant_id, uuid4(), alice).status_code == 503


def test_privilege_errors_on_reads_are_503_not_404(
    api: Api, seed_tenant: Seed, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only a rejected write means "membership revoked"; a privilege error on a
    # read is a misconfiguration and must not be disguised as not found.
    tenant = seed_tenant()
    alice = _subject()
    api.grant(alice, tenant.ctx.tenant_id)

    class Denied(Exception):
        sqlstate = "42501"

    def denied(*_args: Any, **_kwargs: Any) -> Any:
        raise DBAPIError("SELECT", {}, Denied())

    monkeypatch.setattr(main, "list_workspaces", denied)
    monkeypatch.setattr(main, "get_workspace", denied)

    assert api.list(tenant.ctx.tenant_id, alice).status_code == 503
    assert api.get(tenant.ctx.tenant_id, tenant.workspace_id, alice).status_code == 503


def test_without_auth_configuration_endpoints_are_unavailable(
    runtime_settings: DatabaseSettings, issuer: LocalIssuer
) -> None:
    app = create_app(_environ(runtime_settings, AUTH_ISSUER=""), jwks_fetcher=_fetcher(issuer))
    with TestClient(app) as client:
        response = client.get(f"/v1/tenants/{uuid4()}/workspaces")
    assert (response.status_code, response.json()) == (503, UNAVAILABLE)
    assert response.headers["cache-control"] == "no-store"


# --- Logs, caching, documentation -----------------------------------------------------------


def test_logs_never_contain_tokens_or_subjects(
    api: Api, seed_tenant: Seed, caplog: pytest.LogCaptureFixture
) -> None:
    mine, theirs = seed_tenant(), seed_tenant()
    alice = _subject()
    api.grant(alice, mine.ctx.tenant_id)
    token = api.issuer.token(sub=alice)
    headers = {"Authorization": f"Bearer {token}"}

    with caplog.at_level(logging.DEBUG):
        for path in (mine.ctx.tenant_id, theirs.ctx.tenant_id, "bad"):
            api.request("GET", f"/v1/tenants/{path}/workspaces", None, headers=headers)
            api.request(
                "POST", f"/v1/tenants/{path}/workspaces", None, headers=headers, json={"name": ""}
            )

    assert token not in caplog.text and alice not in caplog.text


def test_all_responses_forbid_caching(api: Api, seed_tenant: Seed) -> None:
    tenant = seed_tenant()
    alice = _subject()
    api.grant(alice, tenant.ctx.tenant_id)
    api.responses.clear()

    api.list(tenant.ctx.tenant_id, alice)  # 200
    api.create(tenant.ctx.tenant_id, alice, {"name": "w"})  # 201
    api.create(tenant.ctx.tenant_id, alice, {"name": ""})  # 422
    api.get(tenant.ctx.tenant_id, uuid4(), alice)  # 404
    api.list(tenant.ctx.tenant_id, None)  # 401

    assert sorted(r.status_code for r in api.responses) == [200, 201, 401, 404, 422]
    assert {r.headers["cache-control"] for r in api.responses} == {"no-store"}


def test_openapi_documents_the_endpoints_and_the_request_body(
    runtime_settings: DatabaseSettings, issuer: LocalIssuer
) -> None:
    # The schema is served only with API_DOCS=1 (see test_api_docs.py).
    app = create_app(_environ(runtime_settings, API_DOCS="1"), jwks_fetcher=_fetcher(issuer))
    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()
    paths = schema["paths"]

    assert set(paths["/v1/tenants/{tenant_id}/workspaces"]) == {"get", "post"}
    assert set(paths["/v1/tenants/{tenant_id}/workspaces/{workspace_id}"]) == {"get"}
    post = paths["/v1/tenants/{tenant_id}/workspaces"]["post"]
    body_schema = post["requestBody"]["content"]["application/json"]["schema"]
    assert body_schema["required"] == ["name"] and body_schema["additionalProperties"] is False
    assert {"201", "401", "404", "422", "503"} <= set(post["responses"])
