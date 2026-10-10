"""GET /v1/me end to end: real app, real PostgreSQL 18 (app_test, app_runtime), local issuer."""

import logging
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from api.auth import JwksFetcher
from api.database import DatabaseSettings
from api.main import create_app
from tests.token_issuer import AUDIENCE, CLIENT_ID, ISSUER, LocalIssuer

UNAUTHORIZED = {"detail": "unauthorized"}
UNAVAILABLE = {"detail": "unavailable"}
INVALID_TOKEN_CHALLENGE = 'Bearer error="invalid_token"'


@dataclass
class FetchLog:
    issuer: LocalIssuer
    urls: list[str] = field(default_factory=list)
    fetches: int = 0

    def factory(self, url: str) -> JwksFetcher:
        self.urls.append(url)

        def fetch() -> Mapping[str, Any]:
            self.fetches += 1
            return self.issuer.jwks()

        return fetch


@pytest.fixture
def issuer() -> LocalIssuer:
    return LocalIssuer()


@pytest.fixture
def fetch_log(issuer: LocalIssuer) -> FetchLog:
    return FetchLog(issuer)


def _environ(runtime_settings: DatabaseSettings, **overrides: str) -> dict[str, str]:
    env = {
        "DATABASE_URL": runtime_settings.url.render_as_string(hide_password=False),
        "AUTH_ISSUER": ISSUER,
        "AUTH_AUDIENCE": AUDIENCE,
        "AUTH_ALLOWED_CLIENT_IDS": CLIENT_ID,
    }
    env.update(overrides)
    return env


@pytest.fixture
def client(runtime_settings: DatabaseSettings, fetch_log: FetchLog) -> Iterator[TestClient]:
    app = create_app(_environ(runtime_settings), jwks_fetcher=fetch_log.factory)
    with TestClient(app) as client:
        yield client


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _user_count(owner_engine: Engine, subject: str) -> int:
    with owner_engine.connect() as connection:
        count: int = connection.execute(
            text("SELECT count(*) FROM users WHERE issuer = :i AND subject = :s"),
            {"i": ISSUER, "s": subject},
        ).scalar_one()
    return count


# --- Authentication failures ---------------------------------------------------------------


def test_missing_credentials_get_a_bare_bearer_challenge(client: TestClient) -> None:
    response = client.get("/v1/me")

    assert (response.status_code, response.json()) == (401, UNAUTHORIZED)
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "authorization",
    [
        "Bearer not-a-token",
        "Basic dXNlcjpwYXNz",
        "Bearer",
        "Bearer ",
        "Bearer a b",
        "Token abc",
    ],
)
def test_malformed_authorization_is_rejected_identically(
    client: TestClient, authorization: str
) -> None:
    response = client.get("/v1/me", headers={"Authorization": authorization})

    assert (response.status_code, response.json()) == (401, UNAUTHORIZED)
    assert response.headers["www-authenticate"] == INVALID_TOKEN_CHALLENGE


def test_repeated_authorization_headers_are_rejected(
    client: TestClient, issuer: LocalIssuer
) -> None:
    token = issuer.token()
    response = client.get(
        "/v1/me", headers=[("Authorization", f"Bearer {token}"), ("Authorization", "Bearer x")]
    )

    assert (response.status_code, response.json()) == (401, UNAUTHORIZED)


@pytest.mark.parametrize(
    "overrides",
    [
        {"exp": int(time.time()) - 60},
        {"aud": "https://other.api"},
        {"iss": "https://other.test/"},
        {"client_id": "another-client"},
    ],
    ids=["expired", "audience", "issuer", "client"],
)
def test_invalid_tokens_are_rejected_without_details(
    client: TestClient,
    issuer: LocalIssuer,
    owner_engine: Engine,
    caplog: pytest.LogCaptureFixture,
    overrides: dict[str, Any],
) -> None:
    subject = f"auth0|rejected-{time.monotonic_ns()}"
    token = issuer.token(sub=subject, **overrides)

    with caplog.at_level(logging.DEBUG):
        response = client.get("/v1/me", headers=_bearer(token))

    assert (response.status_code, response.json()) == (401, UNAUTHORIZED)
    assert response.headers["www-authenticate"] == INVALID_TOKEN_CHALLENGE
    assert token not in caplog.text and subject not in caplog.text
    assert "token rejected:" in caplog.text
    assert _user_count(owner_engine, subject) == 0  # nothing provisioned


def test_bearer_scheme_is_case_insensitive(client: TestClient, issuer: LocalIssuer) -> None:
    response = client.get("/v1/me", headers={"Authorization": f"bearer {issuer.token()}"})

    assert response.status_code == 200


# --- Success ----------------------------------------------------------------------------------


def test_first_call_provisions_the_user_and_later_calls_reuse_it(
    client: TestClient, issuer: LocalIssuer, owner_engine: Engine
) -> None:
    subject = f"auth0|new-{time.monotonic_ns()}"

    first = client.get("/v1/me", headers=_bearer(issuer.token(sub=subject)))
    second = client.get("/v1/me", headers=_bearer(issuer.token(sub=subject)))

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {"user_id": first.json()["user_id"], "memberships": []}
    assert UUID(first.json()["user_id"]).version == 7
    assert _user_count(owner_engine, subject) == 1


def _seed_memberships(owner_engine: Engine, user_id: str, roles: list[str]) -> list[str]:
    tenant_ids = []
    with owner_engine.begin() as connection:
        for role in roles:
            tenant_id: UUID = connection.execute(
                text("INSERT INTO tenants (name) VALUES ('Tenant') RETURNING id")
            ).scalar_one()
            connection.execute(
                text("INSERT INTO memberships (tenant_id, user_id, role) VALUES (:t, :u, :r)"),
                {"t": tenant_id, "u": user_id, "r": role},
            )
            tenant_ids.append(str(tenant_id))
    return tenant_ids


def test_memberships_are_the_callers_own_only(
    client: TestClient, issuer: LocalIssuer, owner_engine: Engine
) -> None:
    alice = issuer.token(sub=f"auth0|alice-{time.monotonic_ns()}")
    bob = issuer.token(sub=f"auth0|bob-{time.monotonic_ns()}")
    alice_id = client.get("/v1/me", headers=_bearer(alice)).json()["user_id"]
    bob_id = client.get("/v1/me", headers=_bearer(bob)).json()["user_id"]
    alice_tenants = _seed_memberships(owner_engine, alice_id, ["owner", "member"])
    bob_tenants = _seed_memberships(owner_engine, bob_id, ["member"])

    alice_view = client.get("/v1/me", headers=_bearer(alice)).json()
    bob_view = client.get("/v1/me", headers=_bearer(bob)).json()

    assert alice_id != bob_id
    assert alice_view == {
        "user_id": alice_id,
        "memberships": sorted(
            [
                {"tenant_id": alice_tenants[0], "role": "owner"},
                {"tenant_id": alice_tenants[1], "role": "member"},
            ],
            key=lambda m: UUID(m["tenant_id"]),
        ),
    }
    assert bob_view == {
        "user_id": bob_id,
        "memberships": [{"tenant_id": bob_tenants[0], "role": "member"}],
    }


def test_response_and_logs_never_contain_the_token_or_subject(
    client: TestClient, issuer: LocalIssuer, caplog: pytest.LogCaptureFixture
) -> None:
    subject = f"auth0|private-{time.monotonic_ns()}"
    token = issuer.token(sub=subject)

    with caplog.at_level(logging.DEBUG):
        response = client.get("/v1/me", headers=_bearer(token))

    assert response.status_code == 200
    assert subject not in response.text and ISSUER not in response.text
    assert token not in caplog.text and subject not in caplog.text


# --- Keys, configuration and availability ------------------------------------------------------


def test_keys_come_only_from_the_configured_issuer_and_not_at_startup(
    client: TestClient, issuer: LocalIssuer, fetch_log: FetchLog
) -> None:
    assert fetch_log.urls == ["https://issuer.test/.well-known/jwks.json"]
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 200
    assert fetch_log.fetches == 0  # nothing fetched at startup or by health checks

    headers = {"jku": "https://attacker.test/jwks.json", "x5u": "https://attacker.test/x"}
    client.get("/v1/me", headers=_bearer(issuer.token(headers=headers)))

    assert fetch_log.fetches == 1
    assert fetch_log.urls == ["https://issuer.test/.well-known/jwks.json"]


@pytest.mark.parametrize(
    "overrides",
    [{"AUTH_ISSUER": ""}, {"AUTH_AUDIENCE": ""}, {"AUTH_ISSUER": "http://issuer.test/"}],
)
def test_without_valid_auth_configuration_me_is_unavailable_but_health_is_not(
    runtime_settings: DatabaseSettings,
    fetch_log: FetchLog,
    issuer: LocalIssuer,
    caplog: pytest.LogCaptureFixture,
    overrides: dict[str, str],
) -> None:
    app = create_app(_environ(runtime_settings, **overrides), jwks_fetcher=fetch_log.factory)
    with caplog.at_level(logging.INFO), TestClient(app) as client:
        me = client.get("/v1/me", headers=_bearer(issuer.token()))
        live = client.get("/health/live")
        ready = client.get("/health/ready")

    assert (me.status_code, me.json()) == (503, UNAVAILABLE)
    assert (live.status_code, ready.status_code) == (200, 200)
    assert "authentication is not configured" in caplog.text
    assert fetch_log.fetches == 0


def test_database_outage_makes_me_unavailable_after_authentication(
    runtime_settings: DatabaseSettings, fetch_log: FetchLog, issuer: LocalIssuer
) -> None:
    environ = _environ(
        runtime_settings, DATABASE_URL="postgresql://app_runtime:pw@127.0.0.1:1/app_test"
    )
    with TestClient(create_app(environ, jwks_fetcher=fetch_log.factory)) as client:
        valid = client.get("/v1/me", headers=_bearer(issuer.token()))
        invalid = client.get("/v1/me", headers=_bearer("garbage"))
        live = client.get("/health/live")

    assert (valid.status_code, valid.json()) == (503, UNAVAILABLE)
    assert invalid.status_code == 401  # authentication is checked before the database
    assert live.status_code == 200


def test_health_endpoints_ignore_authorization_headers(client: TestClient) -> None:
    headers = {"Authorization": "Bearer garbage"}

    assert client.get("/health/live", headers=headers).status_code == 200
    assert client.get("/health/ready", headers=headers).status_code == 200


# --- Regressions from the A2 pre-push review ------------------------------------------------


@pytest.mark.parametrize("client_id", [["web-client"], {"id": "web-client"}])
def test_signed_token_with_non_string_client_id_gets_401_not_500(
    runtime_settings: DatabaseSettings, fetch_log: FetchLog, issuer: LocalIssuer, client_id: object
) -> None:
    app = create_app(_environ(runtime_settings), jwks_fetcher=fetch_log.factory)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/v1/me", headers=_bearer(issuer.token(client_id=client_id)))

    assert (response.status_code, response.json()) == (401, UNAUTHORIZED)


def _no_store_responses(
    runtime_settings: DatabaseSettings, fetch_log: FetchLog, issuer: LocalIssuer
) -> list[tuple[str, int, str | None]]:
    seen: list[tuple[str, int, str | None]] = []

    def record(case: str, response: Any) -> None:
        seen.append((case, response.status_code, response.headers.get("cache-control")))

    with TestClient(create_app(_environ(runtime_settings), jwks_fetcher=fetch_log.factory)) as c:
        record("ok", c.get("/v1/me", headers=_bearer(issuer.token())))
        record("no credentials", c.get("/v1/me"))
        record("invalid token", c.get("/v1/me", headers=_bearer("garbage")))
    unconfigured = _environ(runtime_settings, AUTH_ISSUER="")
    with TestClient(create_app(unconfigured, jwks_fetcher=fetch_log.factory)) as c:
        record("auth not configured", c.get("/v1/me", headers=_bearer(issuer.token())))
    database_down = _environ(
        runtime_settings, DATABASE_URL="postgresql://app_runtime:pw@127.0.0.1:1/app_test"
    )
    with TestClient(create_app(database_down, jwks_fetcher=fetch_log.factory)) as c:
        record("database down", c.get("/v1/me", headers=_bearer(issuer.token())))
    return seen


def test_every_me_response_forbids_caching(
    runtime_settings: DatabaseSettings, fetch_log: FetchLog, issuer: LocalIssuer
) -> None:
    # H1: user-specific and error responses must never be stored by any cache.
    assert _no_store_responses(runtime_settings, fetch_log, issuer) == [
        ("ok", 200, "no-store"),
        ("no credentials", 401, "no-store"),
        ("invalid token", 401, "no-store"),
        ("auth not configured", 503, "no-store"),
        ("database down", 503, "no-store"),
    ]
