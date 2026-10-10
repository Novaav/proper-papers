"""Document repository: validation, integrity and atomicity (app_runtime on app_test)."""

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from api import documents
from api.database import create_session_factory, tenant_transaction
from api.documents import (
    CleanScan,
    DocumentTooLarge,
    EncryptedPdf,
    InvalidDocumentName,
    NotAPdf,
    TenantContextMissing,
    WorkspaceNotFound,
    check_pdf_content,
    create_document,
    get_document,
    list_documents,
    normalize_document_name,
    read_document_content,
)
from api.models import DOCUMENT_MAX_BYTES
from tests.conftest import SeededTenant

Seed = Callable[..., SeededTenant]
PDF = b"%PDF-1.7\n% synthetic test document\n%%EOF\n"
CLEAN = CleanScan(
    engine="ClamAV",
    engine_version="1.4.6",
    signature_version="27777",
    signature_date=datetime(2026, 9, 30, tzinfo=UTC),
)


@pytest.fixture
def sessions(runtime_engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(runtime_engine)


# --- validation (no database) ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "stored"),
    [
        ("report.pdf", "report.pdf"),
        ("  padded.pdf  ", "padded.pdf"),
        ("../../etc/passwd.pdf", "passwd.pdf"),
        ("C:\\Users\\me\\contract.pdf", "contract.pdf"),
        ("e\u0301te\u0301.pdf", "\u00e9t\u00e9.pdf"),  # NFC
        ("Årsredovisning 2025.pdf", "Årsredovisning 2025.pdf"),
        ("x" * 255, "x" * 255),
    ],
)
def test_names_are_normalised_display_text(raw: str, stored: str) -> None:
    assert normalize_document_name(raw) == stored


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "folder/",
        "..",
        ".",
        "x" * 256,
        "a\x00b.pdf",
        "a\nb.pdf",
        "a\tb.pdf",
        "a\x7fb.pdf",
        "invoice\u202efdp.exe",  # right-to-left override
        "a\u200bb.pdf",  # zero-width space
        "a\u2066b.pdf",  # isolate
    ],
)
def test_bad_names_are_rejected(raw: str) -> None:
    with pytest.raises(InvalidDocumentName):
        normalize_document_name(raw)


@pytest.mark.parametrize(
    ("content", "error"),
    [
        (b"", NotAPdf),
        (b"%PDF", NotAPdf),
        (b"<html>%PDF-1.7", NotAPdf),
        (b"\xef\xbb\xbf%PDF-1.7", NotAPdf),
        (b"PK\x03\x04", NotAPdf),
        (b"%PDF-" + b"x" * DOCUMENT_MAX_BYTES, DocumentTooLarge),
    ],
)
def test_content_must_be_a_bounded_pdf(content: bytes, error: type[Exception]) -> None:
    with pytest.raises(error):
        check_pdf_content(content)


def test_pdf_limits_are_inclusive() -> None:
    check_pdf_content(b"%PDF-")
    check_pdf_content(b"%PDF-" + b"x" * (DOCUMENT_MAX_BYTES - 5))


# --- persistence --------------------------------------------------------------------------------


def test_stored_bytes_hash_and_size_match_the_upload(
    sessions: sessionmaker[Session], seed_tenant: Seed
) -> None:
    a = seed_tenant("member")
    with tenant_transaction(sessions, a.ctx) as session:
        created = create_document(session, a.workspace_id, "contract.pdf", PDF, CLEAN)

    assert created.sha256 == hashlib.sha256(PDF).digest()
    assert (created.size_bytes, created.version_number, created.media_type) == (
        len(PDF),
        1,
        "application/pdf",
    )
    assert created.created_by == a.ctx.user_id
    with tenant_transaction(sessions, a.ctx) as session:
        found = read_document_content(session, a.workspace_id, created.id)
        assert get_document(session, a.workspace_id, created.id) == created
        assert list_documents(session, a.workspace_id) == [created]
    assert found is not None
    record, content = found
    assert (record, content) == (created, PDF)
    assert content is not None and hashlib.sha256(content).digest() == record.sha256


def test_documents_are_listed_newest_first_per_workspace(
    sessions: sessionmaker[Session], owner_engine: Engine, seed_tenant: Seed
) -> None:
    a = seed_tenant()
    with owner_engine.begin() as connection:
        other_workspace: UUID = connection.execute(
            text("INSERT INTO workspaces (tenant_id, name) VALUES (:t, 'Other') RETURNING id"),
            {"t": a.ctx.tenant_id},
        ).scalar_one()
    with tenant_transaction(sessions, a.ctx) as session:
        first = create_document(session, a.workspace_id, "first.pdf", PDF, CLEAN)
    with tenant_transaction(sessions, a.ctx) as session:
        second = create_document(session, a.workspace_id, "second.pdf", PDF, CLEAN)
        elsewhere = create_document(session, other_workspace, "elsewhere.pdf", PDF, CLEAN)

    with tenant_transaction(sessions, a.ctx) as session:
        assert [d.id for d in list_documents(session, a.workspace_id)] == [second.id, first.id]
        assert [d.id for d in list_documents(session, other_workspace)] == [elsewhere.id]
        # A document is found only under its own workspace.
        assert get_document(session, other_workspace, first.id) is None
        assert read_document_content(session, other_workspace, first.id) is None


def test_other_tenants_documents_are_invisible(
    sessions: sessionmaker[Session], seed_tenant: Seed
) -> None:
    a, b = seed_tenant(), seed_tenant()
    with tenant_transaction(sessions, a.ctx) as session:
        created = create_document(session, a.workspace_id, "a.pdf", PDF, CLEAN)

    with tenant_transaction(sessions, b.ctx) as session:
        assert get_document(session, a.workspace_id, created.id) is None
        assert read_document_content(session, a.workspace_id, created.id) is None
        assert get_document(session, b.workspace_id, created.id) is None
        with pytest.raises(WorkspaceNotFound):
            list_documents(session, a.workspace_id)
        with pytest.raises(WorkspaceNotFound):
            create_document(session, a.workspace_id, "intrusion.pdf", PDF, CLEAN)


def test_unknown_workspace_and_missing_context(
    sessions: sessionmaker[Session], seed_tenant: Seed
) -> None:
    a = seed_tenant()
    with tenant_transaction(sessions, a.ctx) as session:
        with pytest.raises(WorkspaceNotFound):
            create_document(session, uuid4(), "x.pdf", PDF, CLEAN)
        assert get_document(session, a.workspace_id, uuid4()) is None
    with sessions() as session, session.begin():
        with pytest.raises(TenantContextMissing):
            create_document(session, a.workspace_id, "x.pdf", PDF, CLEAN)
        with pytest.raises(TenantContextMissing):
            list_documents(session, a.workspace_id)


def test_invalid_input_is_rejected_before_any_statement(
    sessions: sessionmaker[Session], seed_tenant: Seed
) -> None:
    a = seed_tenant()
    with tenant_transaction(sessions, a.ctx) as session:
        with pytest.raises(InvalidDocumentName):
            create_document(session, a.workspace_id, "a\x00.pdf", PDF, CLEAN)
        with pytest.raises(NotAPdf):
            create_document(session, a.workspace_id, "x.pdf", b"not a pdf", CLEAN)
        with pytest.raises(TypeError):
            create_document(session, str(a.workspace_id), "x.pdf", PDF, CLEAN)  # type: ignore[arg-type]
        assert list_documents(session, a.workspace_id) == []


def test_a_failure_after_the_first_insert_leaves_nothing(
    sessions: sessionmaker[Session],
    owner_engine: Engine,
    seed_tenant: Seed,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Bypass the Python size check so the database rejects the version row (CHECK
    # size_bytes) after the document row was inserted: the transaction rolls back.
    a = seed_tenant()
    monkeypatch.setattr(documents, "check_pdf_content", lambda _content: None)
    oversized = b"%PDF-" + b"x" * DOCUMENT_MAX_BYTES

    with pytest.raises(IntegrityError), tenant_transaction(sessions, a.ctx) as session:
        create_document(session, a.workspace_id, "too-big.pdf", oversized, CLEAN)

    with owner_engine.connect() as connection:
        counts = connection.execute(
            text(
                "SELECT (SELECT count(*) FROM documents WHERE tenant_id = :t), "
                "(SELECT count(*) FROM document_versions WHERE tenant_id = :t), "
                "(SELECT count(*) FROM document_contents WHERE tenant_id = :t), "
                "(SELECT count(*) FROM document_scans WHERE tenant_id = :t)"
            ),
            {"t": a.ctx.tenant_id},
        ).one()
    assert tuple(counts) == (0, 0, 0, 0)


# --- malware scans (migration 0007) -------------------------------------------------------------


def _scans(owner_engine: Engine, version_id: UUID) -> list[tuple[object, ...]]:
    with owner_engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT scan_number, verdict, engine, engine_version, signature_version, "
                "signature_date, created_by FROM document_scans WHERE version_id = :v "
                "ORDER BY scan_number"
            ),
            {"v": version_id},
        )
        return [tuple(row) for row in rows]


def test_a_document_is_stored_with_its_clean_scan(
    sessions: sessionmaker[Session], owner_engine: Engine, seed_tenant: Seed
) -> None:
    a = seed_tenant("member")
    with tenant_transaction(sessions, a.ctx) as session:
        created = create_document(session, a.workspace_id, "a.pdf", PDF, CLEAN)
    assert created.scan_status == "clean"
    assert _scans(owner_engine, created.version_id) == [
        (1, "clean", "ClamAV", "1.4.6", "27777", CLEAN.signature_date, a.ctx.user_id)
    ]


def test_create_requires_a_clean_scan_and_rejects_encrypted_content(
    sessions: sessionmaker[Session], seed_tenant: Seed
) -> None:
    a = seed_tenant()
    with tenant_transaction(sessions, a.ctx) as session:
        with pytest.raises(TypeError):
            create_document(session, a.workspace_id, "x.pdf", PDF, None)  # type: ignore[arg-type]
        with pytest.raises(EncryptedPdf):
            create_document(
                session, a.workspace_id, "x.pdf", PDF + b"trailer << /Encrypt 5 0 R >>", CLEAN
            )
        assert list_documents(session, a.workspace_id) == []


def test_a_later_non_clean_verdict_blocks_the_bytes(
    sessions: sessionmaker[Session], owner_engine: Engine, seed_tenant: Seed
) -> None:
    # A future rescan appends a verdict; the latest one decides.
    a = seed_tenant()
    with tenant_transaction(sessions, a.ctx) as session:
        created = create_document(session, a.workspace_id, "a.pdf", PDF, CLEAN)
    with owner_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO document_scans (tenant_id, version_id, scan_number, verdict, engine, "
                "engine_version, signature_version, signature_date, created_by) "
                "VALUES (:t, :v, 2, 'infected', 'ClamAV', '1.4.6', '27778', now(), :u)"
            ),
            {"t": a.ctx.tenant_id, "v": created.version_id, "u": a.ctx.user_id},
        )
    with tenant_transaction(sessions, a.ctx) as session:
        [listed] = list_documents(session, a.workspace_id)
        found = read_document_content(session, a.workspace_id, created.id)
    assert listed.scan_status == "blocked"
    assert found is not None and found[1] is None
