"""The live scan check script, run against the real API app (test database, fake scanner)."""

import hashlib
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from api.database import DatabaseSettings
from api.main import create_app
from scripts import live_scan_check
from scripts.live_scan_check import CheckFailed, Reply, run
from tests import synthetic_pdfs
from tests.conftest import SeededTenant
from tests.fake_clamd import FakeClamd
from tests.test_document_endpoints import _counts
from tests.test_workspace_endpoints import Api, _environ, _fetcher, _subject, issuer  # noqa: F401


def _legacy_document(owner_engine: Engine, tenant: SeededTenant, user_id: Any) -> None:
    """A document from before scanning (no scan row; the trigger suspended by the owner)."""
    with owner_engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE document_versions DISABLE TRIGGER document_versions_require_clean_scan"
            )
        )
        document: Any = connection.execute(
            text(
                "INSERT INTO documents (tenant_id, workspace_id, display_name, created_by) "
                "VALUES (:t, :w, 'legacy.pdf', :u) RETURNING id"
            ),
            {"t": tenant.ctx.tenant_id, "w": tenant.workspace_id, "u": user_id},
        ).scalar_one()
        pdf = synthetic_pdfs.clean_pdf()
        version: Any = connection.execute(
            text(
                "INSERT INTO document_versions (tenant_id, document_id, version_number, "
                "media_type, size_bytes, sha256, created_by) "
                "VALUES (:t, :d, 1, 'application/pdf', :n, :h, :u) RETURNING id"
            ),
            {
                "t": tenant.ctx.tenant_id,
                "d": document,
                "n": len(pdf),
                "h": hashlib.sha256(pdf).digest(),
                "u": user_id,
            },
        ).scalar_one()
        connection.execute(
            text("INSERT INTO document_contents VALUES (:v, :t, :c)"),
            {"v": version, "t": tenant.ctx.tenant_id, "c": pdf},
        )
        connection.execute(
            text(
                "ALTER TABLE document_versions ENABLE TRIGGER document_versions_require_clean_scan"
            )
        )


def _run(
    runtime_settings: DatabaseSettings,
    issuer: Any,  # noqa: F811
    owner_engine: Engine,
    seed_tenant: Any,
) -> tuple[list[str], list[str], SeededTenant, SeededTenant, Any]:
    tenant, foreign = seed_tenant(), seed_tenant()
    app = create_app(_environ(runtime_settings), jwks_fetcher=_fetcher(issuer))
    methods: list[str] = []
    lines: list[str] = []
    with TestClient(app) as client:
        api = Api(client, issuer, owner_engine)
        subject = _subject()
        user_id = api.grant(subject, tenant.ctx.tenant_id)
        _legacy_document(owner_engine, tenant, user_id)
        settings = {
            "LIVE_ACCESS_TOKEN": issuer.token(sub=subject),
            "LIVE_TENANT_ID": str(tenant.ctx.tenant_id),
            "LIVE_WORKSPACE_ID": str(tenant.workspace_id),
            "LIVE_FOREIGN_TENANT_ID": str(foreign.ctx.tenant_id),
            "LIVE_FOREIGN_WORKSPACE_ID": str(foreign.workspace_id),
        }

        def send(method: str, path: str, headers: dict[str, str], body: bytes | None) -> Reply:
            methods.append(method)
            response = client.request(method, path, headers=headers, content=body)
            return Reply(response.status_code, response.content)

        run(settings, send, out=lines.append)
    return lines, methods, tenant, foreign, settings


def test_the_live_check_passes_against_the_real_api(
    runtime_settings: DatabaseSettings,
    issuer: Any,  # noqa: F811
    owner_engine: Engine,
    seed_tenant: Any,
    fake_clamd: FakeClamd,
) -> None:
    fake_clamd.infected_marker = synthetic_pdfs.MARKER
    lines, methods, tenant, foreign, settings = _run(
        runtime_settings, issuer, owner_engine, seed_tenant
    )
    assert lines[-1] == "live scan check: all checks passed"
    assert [line for line in lines if "[PASS] 5." in line]  # the legacy document was checked
    assert set(methods) == {"GET", "POST"}  # never anything that could delete or change
    # Exactly the one clean upload was added (plus its scan); the legacy document stays.
    assert _counts(owner_engine, tenant.ctx.tenant_id) == (2, 2, 2, 1)
    assert _counts(owner_engine, foreign.ctx.tenant_id) == (0, 0, 0, 0)
    assert not any(settings["LIVE_ACCESS_TOKEN"] in line for line in lines)


def test_the_live_check_fails_fast_when_the_scanner_is_down(
    runtime_settings: DatabaseSettings,
    issuer: Any,  # noqa: F811
    owner_engine: Engine,
    seed_tenant: Any,
    fake_clamd: FakeClamd,
) -> None:
    fake_clamd.ping_reply = None
    with pytest.raises(CheckFailed, match=r"1\. /health/scanner is 200"):
        _run(runtime_settings, issuer, owner_engine, seed_tenant)


def test_the_live_check_refuses_to_run_without_its_settings(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert live_scan_check.main({}) == 2
    assert "missing settings" in capsys.readouterr().err


def test_the_live_check_catches_a_scanner_that_misses_the_marker(
    runtime_settings: DatabaseSettings,
    issuer: Any,  # noqa: F811
    owner_engine: Engine,
    seed_tenant: Any,
    fake_clamd: FakeClamd,
) -> None:
    # The fake answers "clean" for everything: the marker PDF gets stored, and
    # the check must fail there rather than pass.
    with pytest.raises(CheckFailed, match=r"3\. the test-signature PDF is refused"):
        _run(runtime_settings, issuer, owner_engine, seed_tenant)
