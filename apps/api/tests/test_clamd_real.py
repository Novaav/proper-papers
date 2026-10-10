"""The upload path against the real, pinned ClamAV engine (the mandatory CI gate).

CI starts two clamd containers from a digest-pinned image with the
configurations in tests/clamav/ and sets REAL_CLAMD_ADDRESS (production
limits, plus the test-only marker signature), REAL_CLAMD_TIGHT_ADDRESS (1 MiB
limits, to exceed them with small synthetic files) and REQUIRE_REAL_CLAMD=1.
Under GitHub Actions, or with REQUIRE_REAL_CLAMD=1, a missing engine fails
these tests instead of skipping them, so a pull request cannot pass on the
fake scanner alone even if the CI variable were removed. Elsewhere they are
skipped unless the addresses are set.

The pinned image's signature database is fixed at its build time, so these
tests accept any signature age: they verify verdicts. Freshness is enforced
by the scanner's default (72 hours) and covered with the fake scanner.
"""

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from api.database import DatabaseSettings
from api.main import create_app
from api.scanning import ClamdScanner, ScannerUnavailable, Verdict
from tests import synthetic_pdfs
from tests.conftest import SeededTenant
from tests.test_document_endpoints import _counts, upload
from tests.test_workspace_endpoints import Api, _environ, _fetcher, _subject, issuer  # noqa: F401


def real_engine_required(environ: Mapping[str, str]) -> bool:
    """The real engine is mandatory in CI (GitHub Actions) or when explicitly required."""
    return environ.get("GITHUB_ACTIONS") == "true" or environ.get("REQUIRE_REAL_CLAMD") == "1"


def skip_real_engine_tests(environ: Mapping[str, str]) -> bool:
    """Skip only when not required and no real engine is configured."""
    configured = bool(environ.get("REAL_CLAMD_ADDRESS")) and bool(
        environ.get("REAL_CLAMD_TIGHT_ADDRESS")
    )
    return not real_engine_required(environ) and not configured


ADDRESS = os.environ.get("REAL_CLAMD_ADDRESS", "")
TIGHT_ADDRESS = os.environ.get("REAL_CLAMD_TIGHT_ADDRESS", "")

pytestmark = pytest.mark.skipif(
    skip_real_engine_tests(os.environ),
    reason="real ClamAV not configured (REAL_CLAMD_ADDRESS, REAL_CLAMD_TIGHT_ADDRESS)",
)

ANY_AGE = timedelta(days=3650)


def _scanner(address: str) -> ClamdScanner:
    assert address, "the real ClamAV engine is required here but its address is not set"
    return ClamdScanner(address, max_signature_age=ANY_AGE, warn_signature_age=ANY_AGE)


@pytest.fixture
def real() -> ClamdScanner:
    return _scanner(ADDRESS)


@pytest.fixture
def tight() -> ClamdScanner:
    return _scanner(TIGHT_ADDRESS)


@contextmanager
def _app_api(
    scanner: ClamdScanner,
    runtime_settings: DatabaseSettings,
    issuer: Any,  # noqa: F811
    owner_engine: Engine,
) -> Iterator[Api]:
    app = create_app(_environ(runtime_settings), jwks_fetcher=_fetcher(issuer), scanner=scanner)
    with TestClient(app) as client:
        yield Api(client, issuer, owner_engine)


def _upload_as_member(api: Api, tenant: SeededTenant, content: bytes) -> Any:
    subject = _subject()
    api.grant(subject, tenant.ctx.tenant_id)
    return upload(api, tenant.ctx.tenant_id, tenant.workspace_id, subject, content=content)


# --- engine level ---------------------------------------------------------------------------------


def test_the_engine_reports_its_version(real: ClamdScanner) -> None:
    result = real.scan(synthetic_pdfs.clean_pdf())
    assert result.verdict is Verdict.CLEAN
    assert result.signatures.engine == "ClamAV"
    assert result.signatures.engine_version.startswith("1.4.")


def test_clean_synthetic_pdfs_are_clean(real: ClamdScanner) -> None:
    for content in (synthetic_pdfs.clean_pdf(), synthetic_pdfs.padded_pdf(2 * 1024 * 1024)):
        assert real.scan(content).verdict is Verdict.CLEAN


def test_the_eicar_test_file_is_infected(real: ClamdScanner) -> None:
    result = real.scan(synthetic_pdfs.eicar())
    assert result.verdict is Verdict.INFECTED
    assert result.signature_name is not None and "eicar" in result.signature_name.lower()


def test_the_test_signature_marks_a_pdf_infected(real: ClamdScanner) -> None:
    result = real.scan(synthetic_pdfs.marker_pdf())
    assert result.verdict is Verdict.INFECTED
    assert result.signature_name is not None
    assert result.signature_name.startswith("ProductionAiFlagship.Test.Marker")


def test_a_file_beyond_the_size_limits_is_incomplete_never_clean(tight: ClamdScanner) -> None:
    result = tight.scan(synthetic_pdfs.padded_pdf(2 * 1024 * 1024))
    assert result.verdict is Verdict.INCOMPLETE, result
    assert result.signature_name is not None
    assert result.signature_name.startswith("Heuristics.Limits.Exceeded")


def test_content_expanding_beyond_the_limits_is_incomplete_never_clean(
    tight: ClamdScanner,
) -> None:
    # About 4 KiB compressed, 8 MiB once decompressed by the scanner.
    result = tight.scan(synthetic_pdfs.expanding_pdf(8 * 1024 * 1024))
    assert result.verdict is Verdict.INCOMPLETE, result


def test_a_password_protected_pdf_is_flagged_encrypted_by_the_engine(real: ClamdScanner) -> None:
    # The engine cannot decrypt it, so it must not pass as clean; even the test
    # marker inside it stays hidden.
    for line in (b"Synthetic encrypted document", synthetic_pdfs.MARKER):
        result = real.scan(synthetic_pdfs.encrypted_pdf(user_password=b"secret", line=line))
        assert result.verdict is Verdict.ENCRYPTED, result


def test_the_engine_does_not_inspect_permissions_only_encrypted_pdfs(real: ClamdScanner) -> None:
    # Characterisation of a known engine gap (ClamAV 1.4.6), not a desired
    # outcome: an encrypted PDF with an empty user password (it opens without a
    # prompt; verified with a PDF reader) is neither flagged by AlertEncrypted
    # nor inspected: the test signature inside its encrypted content is not
    # found and the verdict is clean. The API therefore must not rely on the
    # engine for encrypted PDFs: the /Encrypt byte check rejects them first
    # (see the API-level cases below). If this starts failing because the
    # engine flags or inspects such files, update the README and deployment
    # documentation accordingly.
    for line in (b"Synthetic encrypted document", synthetic_pdfs.MARKER):
        result = real.scan(synthetic_pdfs.encrypted_pdf(line=line))
        assert result.verdict is Verdict.CLEAN, result


def test_a_stream_beyond_the_engines_limit_is_unavailable(real: ClamdScanner) -> None:
    # StreamMaxLength is 21M; the engine refuses the stream: no verdict, fail closed.
    with pytest.raises(ScannerUnavailable):
        real.scan(synthetic_pdfs.padded_pdf(22 * 1024 * 1024))


# --- through the API ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("which", "content", "expected"),
    [
        ("real", synthetic_pdfs.clean_pdf(), (201, None)),
        ("real", synthetic_pdfs.marker_pdf(), (422, "malware_detected")),
        ("tight", synthetic_pdfs.padded_pdf(2 * 1024 * 1024), (422, "scan_incomplete")),
        ("tight", synthetic_pdfs.expanding_pdf(8 * 1024 * 1024), (422, "scan_incomplete")),
        ("real", synthetic_pdfs.encrypted_pdf(), (422, "encrypted_pdf")),
        ("real", synthetic_pdfs.encrypted_pdf(user_password=b"secret"), (422, "encrypted_pdf")),
    ],
    ids=[
        "clean",
        "infected",
        "incomplete-size",
        "incomplete-expanded",
        "encrypted-permissions-only",
        "encrypted-password",
    ],
)
def test_real_verdicts_decide_what_the_api_stores(
    runtime_settings: DatabaseSettings,
    issuer: Any,  # noqa: F811
    owner_engine: Engine,
    seed_tenant: Any,
    which: str,
    content: bytes,
    expected: tuple[int, str | None],
) -> None:
    scanner = _scanner(ADDRESS if which == "real" else TIGHT_ADDRESS)
    tenant = seed_tenant()
    with _app_api(scanner, runtime_settings, issuer, owner_engine) as api:
        response = _upload_as_member(api, tenant, content)
    status, detail = expected
    assert response.status_code == status, response.text
    if detail is None:
        assert response.json()["version"]["scan_status"] == "clean"
        assert _counts(owner_engine, tenant.ctx.tenant_id) == (1, 1, 1, 1)
        with owner_engine.connect() as connection:
            engine, version = connection.execute(
                text("SELECT engine, engine_version FROM document_scans WHERE tenant_id = :t"),
                {"t": tenant.ctx.tenant_id},
            ).one()
        assert engine == "ClamAV" and version.startswith("1.4.")
    else:
        assert response.json() == {"detail": detail}
        assert _counts(owner_engine, tenant.ctx.tenant_id) == (0, 0, 0, 0)


def test_an_unreachable_engine_is_503_and_nothing_is_stored(
    runtime_settings: DatabaseSettings,
    issuer: Any,  # noqa: F811
    owner_engine: Engine,
    seed_tenant: Any,
) -> None:
    # The real address with a port nothing listens on (the engine is down).
    host = ADDRESS.removeprefix("tcp://").rpartition(":")[0]
    scanner = _scanner(f"tcp://{host}:9")
    tenant = seed_tenant()
    with _app_api(scanner, runtime_settings, issuer, owner_engine) as api:
        response = _upload_as_member(api, tenant, synthetic_pdfs.clean_pdf())
    assert (response.status_code, response.json()) == (503, {"detail": "scan_unavailable"})
    assert _counts(owner_engine, tenant.ctx.tenant_id) == (0, 0, 0, 0)


def test_scanner_health_with_the_real_engine(
    runtime_settings: DatabaseSettings,
    issuer: Any,  # noqa: F811
    owner_engine: Engine,
) -> None:
    # The same client and rules as uploads (any signature age here: fixed image database).
    host = ADDRESS.removeprefix("tcp://").rpartition(":")[0]
    for address, expected in ((ADDRESS, (200, "ok")), (f"tcp://{host}:9", (503, "unavailable"))):
        with _app_api(_scanner(address), runtime_settings, issuer, owner_engine) as api:
            response = api.client.get("/health/scanner")
        assert (response.status_code, response.json()) == (expected[0], {"status": expected[1]})
