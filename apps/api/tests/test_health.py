from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from api.database import DatabaseSettings
from api.main import app, create_app
from api.scanning import ClamdScanner
from tests.fake_clamd import FakeClamd, fresh_version
from tests.test_workspace_endpoints import _environ

client = TestClient(app)


def test_health_live_reports_ok() -> None:
    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"status": "ok"}


def test_health_live_rejects_non_get_methods() -> None:
    response = client.post("/health/live")

    assert response.status_code == 405


def test_health_live_does_not_depend_on_database_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for value in (None, "not a url", "postgresql://app:pw@127.0.0.1:1/app"):
        if value is None:
            monkeypatch.delenv("DATABASE_URL", raising=False)
        else:
            monkeypatch.setenv("DATABASE_URL", value)

        assert client.get("/health/live").status_code == 200


# --- /health/scanner -----------------------------------------------------------------------------


def _scanner_health(scanner: ClamdScanner | None) -> tuple[int, dict[str, object], str | None]:
    app = create_app({}, scanner=scanner)
    with TestClient(app) as scanner_client:
        response = scanner_client.get("/health/scanner")
    return response.status_code, response.json(), response.headers.get("cache-control")


def test_scanner_health_is_ok_only_for_an_answering_fresh_scanner(fake_clamd: FakeClamd) -> None:
    assert _scanner_health(ClamdScanner(fake_clamd.address)) == (200, {"status": "ok"}, "no-store")


@pytest.mark.parametrize(
    ("ping", "version"),
    [
        (None, fresh_version()),
        (b"PONG", fresh_version(age=timedelta(hours=73))),
        (b"PONG", b"COMMAND UNAVAILABLE"),
    ],
    ids=["no-answer", "stale-signatures", "unknown-signatures"],
)
def test_scanner_health_fails_closed(
    fake_clamd: FakeClamd, ping: bytes | None, version: bytes
) -> None:
    fake_clamd.ping_reply = ping
    fake_clamd.version_reply = version
    status, body, cache = _scanner_health(ClamdScanner(fake_clamd.address))
    # Only the status: no version, address or signature date.
    assert (status, body, cache) == (503, {"status": "unavailable"}, "no-store")


def test_scanner_health_without_a_configured_scanner_is_503() -> None:
    assert _scanner_health(None) == (503, {"status": "unavailable"}, "no-store")


def test_readiness_does_not_depend_on_the_scanner(
    runtime_settings: DatabaseSettings, fake_clamd: FakeClamd
) -> None:
    # A scanner outage stops uploads, not the API: reads and downloads go on.
    fake_clamd.ping_reply = None
    app = create_app(_environ(runtime_settings))
    with TestClient(app) as scanner_client:
        ready = scanner_client.get("/health/ready")
        scanner = scanner_client.get("/health/scanner")
    assert (ready.status_code, ready.json()) == (200, {"status": "ready"})
    assert (scanner.status_code, scanner.json()) == (503, {"status": "unavailable"})
