"""Migration 0008 access control: app_runtime, app_worker and app_operator on the new tables.

Runs are claimed as the owner here (the worker's own claiming is tested in
test_worker.py), so each case exercises exactly one role's privileges and
row-level security.
"""

import hashlib
import uuid
from collections.abc import Callable, Iterator
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from api.database import DatabaseSettings, tenant_transaction
from api.models import FIELD_KEYS
from tests.conftest import SeededTenant, sqlstate
from tests.processing_support import Seeded, as_worker, block, start

INSUFFICIENT_PRIVILEGE = "42501"
NEW_TABLES = (
    "processing_runs",
    "document_pages",
    "extracted_fields",
    "field_candidates",
    "candidate_evidence",
)


@pytest.fixture
def operator_engine(operator_settings: DatabaseSettings) -> Iterator[Engine]:
    engine = create_engine(operator_settings.url, poolclass=NullPool)
    yield engine
    engine.dispose()


def _claim(owner_engine: Engine, run_id: UUID, lease: UUID | None = None) -> UUID:
    lease = lease or uuid.uuid7()
    with owner_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE processing_runs SET status = 'running', attempts = attempts + 1, "
                "lease_owner = :l, lease_expires_at = now() + interval '5 minutes' WHERE id = :id"
            ),
            {"l": lease, "id": run_id},
        )
    return lease


def _write_results(connection: Connection, seeded: Seeded, run_id: UUID) -> None:
    """A minimal complete result: one page, seven fields, one candidate with evidence."""
    params = {"run": run_id, "tenant": seeded.tenant_id}
    connection.execute(
        text(
            "INSERT INTO document_pages (run_id, tenant_id, page_number, text, text_sha256, "
            "off_page_chars, invisible_chars, undefined_chars, control_chars_removed) "
            "VALUES (:run, :tenant, 1, 'Provider: A Ltd', :digest, 0, 0, 0, 0)"
        ),
        params | {"digest": hashlib.sha256(b"Provider: A Ltd").digest()},
    )
    for key in FIELD_KEYS:
        found = key == "provider_name"
        connection.execute(
            text(
                "INSERT INTO extracted_fields (run_id, tenant_id, field_key, status, reason) "
                "VALUES (:run, :tenant, :key, :status, :reason)"
            ),
            params
            | {
                "key": key,
                "status": "found" if found else "unknown",
                "reason": None if found else "not_found",
            },
        )
    candidate = uuid.uuid7()
    connection.execute(
        text(
            "INSERT INTO field_candidates (id, run_id, tenant_id, field_key, ordinal, value, raw) "
            "VALUES (:id, :run, :tenant, 'provider_name', 0, '\"A Ltd\"', 'A Ltd')"
        ),
        params | {"id": candidate},
    )
    connection.execute(
        text(
            "INSERT INTO candidate_evidence (candidate_id, ordinal, run_id, tenant_id, "
            "page_number, char_start, char_end, quote) "
            "VALUES (:c, 0, :run, :tenant, 1, 0, 15, 'Provider: A Ltd')"
        ),
        params | {"c": candidate},
    )


@pytest.fixture
def completed(
    seed_document: Callable[..., Seeded], sessions: sessionmaker[Session], owner_engine: Engine
) -> tuple[Seeded, UUID]:
    seeded = seed_document()
    run_id = start(sessions, seeded)
    _claim(owner_engine, run_id)
    with owner_engine.begin() as connection:
        _write_results(connection, seeded, run_id)
        connection.execute(
            text(
                "UPDATE processing_runs SET status = 'needs_review', finished_at = now() "
                "WHERE id = :id"
            ),
            {"id": run_id},
        )
    return seeded, run_id


def _run_column(table: str) -> str:
    return "id" if table == "processing_runs" else "run_id"


def _counts(session: Session, run_id: UUID) -> dict[str, int]:
    return {
        table: session.execute(
            text(f"SELECT count(*) FROM {table} WHERE {_run_column(table)} = :r"),
            {"r": run_id},
        ).scalar_one()
        for table in NEW_TABLES
    }


# ---------------------------------------------------------------- app_runtime


def test_members_read_their_runs_and_results(
    completed: tuple[Seeded, UUID], sessions: sessionmaker[Session]
) -> None:
    seeded, run_id = completed
    with tenant_transaction(sessions, seeded.tenant.ctx) as session:
        assert _counts(session, run_id) == {
            "processing_runs": 1,
            "document_pages": 1,
            "extracted_fields": 7,
            "field_candidates": 1,
            "candidate_evidence": 1,
        }


def test_other_tenants_see_nothing(
    completed: tuple[Seeded, UUID],
    sessions: sessionmaker[Session],
    seed_tenant: Callable[..., SeededTenant],
) -> None:
    other = seed_tenant()
    with tenant_transaction(sessions, other.ctx) as session:
        assert set(_counts(session, completed[1]).values()) == {0}


def test_derived_text_is_hidden_once_the_version_is_not_clean(
    completed: tuple[Seeded, UUID], sessions: sessionmaker[Session], owner_engine: Engine
) -> None:
    seeded, run_id = completed
    block(owner_engine, seeded)
    with tenant_transaction(sessions, seeded.tenant.ctx) as session:
        counts = _counts(session, run_id)
    # Run state and field statuses stay visible; pages, candidates and quotes do not.
    assert counts == {
        "processing_runs": 1,
        "document_pages": 0,
        "extracted_fields": 7,
        "field_candidates": 0,
        "candidate_evidence": 0,
    }


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE processing_runs SET not_before = now() WHERE id = :r",
        "DELETE FROM processing_runs WHERE id = :r",
        "UPDATE document_pages SET text = 'x' WHERE run_id = :r",
        "DELETE FROM document_pages WHERE run_id = :r",
        "UPDATE extracted_fields SET status = 'found' WHERE run_id = :r",
        "DELETE FROM field_candidates WHERE run_id = :r",
        "UPDATE candidate_evidence SET quote = 'x' WHERE run_id = :r",
        "DELETE FROM candidate_evidence WHERE run_id = :r",
        "INSERT INTO extracted_fields (run_id, tenant_id, field_key, status, reason) "
        "SELECT id, tenant_id, 'end_date', 'found', NULL FROM processing_runs WHERE id = :r",
    ],
)
def test_members_cannot_change_runs_or_results(
    completed: tuple[Seeded, UUID], sessions: sessionmaker[Session], statement: str
) -> None:
    seeded, run_id = completed
    with (
        pytest.raises(DBAPIError) as error,
        tenant_transaction(sessions, seeded.tenant.ctx) as session,
    ):
        session.execute(text(statement), {"r": run_id})
    assert sqlstate(error.value) == INSUFFICIENT_PRIVILEGE


def _insert_run(session: Session, seeded: Seeded, **overrides: Any) -> None:
    values = {
        "tenant": seeded.tenant_id,
        "version": seeded.document.version_id,
        "pipeline": f"p-{uuid.uuid4()}",
        "user": seeded.tenant.ctx.user_id,
    } | overrides
    columns = "tenant_id, version_id, pipeline_version, created_by"
    placeholders = ":tenant, :version, :pipeline, :user"
    if "status" in values:
        columns += ", status"
        placeholders += ", :status"
    session.execute(
        text(f"INSERT INTO processing_runs ({columns}) VALUES ({placeholders})"), values
    )


def test_members_create_runs_only_queued_as_themselves_for_clean_versions(
    seed_document: Callable[..., Seeded],
    sessions: sessionmaker[Session],
    seed_tenant: Callable[..., SeededTenant],
    owner_engine: Engine,
) -> None:
    seeded = seed_document()
    with tenant_transaction(sessions, seeded.tenant.ctx) as session:
        _insert_run(session, seeded)  # allowed
    other_user = seed_tenant().ctx.user_id
    for overrides in ({"status": "running"}, {"user": other_user}):
        with (
            pytest.raises(DBAPIError) as error,
            tenant_transaction(sessions, seeded.tenant.ctx) as session,
        ):
            _insert_run(session, seeded, **overrides)
        assert sqlstate(error.value) == INSUFFICIENT_PRIVILEGE, overrides
    block(owner_engine, seeded)
    with (
        pytest.raises(DBAPIError) as error,
        tenant_transaction(sessions, seeded.tenant.ctx) as session,
    ):
        _insert_run(session, seeded)
    assert sqlstate(error.value) == INSUFFICIENT_PRIVILEGE


# ---------------------------------------------------------------- app_worker


def _worker_read(
    worker_engine: Engine, sql: str, params: dict[str, Any] | None = None, **gucs: UUID | None
) -> list[Any]:
    with worker_engine.begin() as connection:
        as_worker(connection, **gucs)
        return list(connection.execute(text(sql), params or {}))


def _content(worker_engine: Engine, seeded: Seeded, **gucs: UUID | None) -> list[Any]:
    return _worker_read(
        worker_engine,
        "SELECT c.content, v.sha256, s.verdict FROM document_contents c "
        "JOIN document_versions v ON v.id = c.version_id "
        "JOIN document_scans s ON s.version_id = c.version_id WHERE c.version_id = :v",
        {"v": seeded.document.version_id},
        **gucs,
    )


def test_the_worker_sees_the_queue_of_every_tenant_but_no_document_data(
    seed_document: Callable[..., Seeded], sessions: sessionmaker[Session], worker_engine: Engine
) -> None:
    runs = [start(sessions, seed_document()) for _ in range(2)]
    queued = _worker_read(
        worker_engine, "SELECT id FROM processing_runs WHERE id = ANY(:ids)", {"ids": runs}
    )
    assert len(queued) == 2
    for table in ("document_contents", "document_versions", "document_scans"):
        assert _worker_read(worker_engine, f"SELECT count(*) FROM {table}")[0][0] == 0


def test_the_worker_reads_only_its_claimed_clean_version_under_its_lease(
    seed_document: Callable[..., Seeded],
    sessions: sessionmaker[Session],
    owner_engine: Engine,
    worker_engine: Engine,
) -> None:
    seeded, other = seed_document(), seed_document()
    run_id, other_run = start(sessions, seeded), start(sessions, other)
    lease = _claim(owner_engine, run_id)
    _claim(owner_engine, other_run)
    mine = {"worker": lease, "run": run_id, "tenant": seeded.tenant_id}
    assert len(_content(worker_engine, seeded, **mine)) == 1
    # Another version (another tenant's), another lease, another run, a wrong tenant: nothing.
    assert _content(worker_engine, other, **mine) == []
    assert _content(worker_engine, seeded, **(mine | {"worker": uuid.uuid7()})) == []
    assert _content(worker_engine, seeded, **(mine | {"run": other_run})) == []
    assert _content(worker_engine, seeded, **(mine | {"tenant": other.tenant_id})) == []
    # A version whose latest verdict is no longer clean: no bytes.
    block(owner_engine, seeded)
    assert _worker_read(
        worker_engine,
        "SELECT count(*) FROM document_contents WHERE version_id = :v",
        {"v": seeded.document.version_id},
        **mine,
    ) == [(0,)]


def test_the_worker_cannot_read_a_queued_runs_version(
    seed_document: Callable[..., Seeded], sessions: sessionmaker[Session], worker_engine: Engine
) -> None:
    seeded = seed_document()
    run_id = start(sessions, seeded)
    assert (
        _content(worker_engine, seeded, worker=uuid.uuid7(), run=run_id, tenant=seeded.tenant_id)
        == []
    )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM users",
        "SELECT * FROM memberships",
        "SELECT * FROM tenants",
        "SELECT * FROM workspaces",
        "SELECT * FROM documents",
        "SELECT * FROM operator_audit",
        "SELECT created_by FROM processing_runs",
        "SELECT parser_versions FROM processing_runs",
        "SELECT * FROM field_candidates",
        "SELECT * FROM candidate_evidence",
        "SELECT text FROM document_pages",
        "SELECT reason FROM extracted_fields",
        "SELECT media_type FROM document_versions",
        "SELECT engine FROM document_scans",
        "DELETE FROM processing_runs",
        "DELETE FROM document_pages",
        "UPDATE processing_runs SET pipeline_version = 'x'",
        "UPDATE processing_runs SET created_by = gen_random_uuid()",
        "UPDATE document_pages SET text = 'x'",
        "INSERT INTO processing_runs (tenant_id, version_id, pipeline_version, created_by) "
        "VALUES (gen_random_uuid(), gen_random_uuid(), 'x', gen_random_uuid())",
    ],
)
def test_the_worker_has_nothing_else(worker_engine: Engine, sql: str) -> None:
    with pytest.raises(DBAPIError) as error:
        _worker_read(worker_engine, sql)
    assert sqlstate(error.value) == INSUFFICIENT_PRIVILEGE


def test_the_worker_writes_results_only_for_its_running_claimed_run(
    seed_document: Callable[..., Seeded],
    sessions: sessionmaker[Session],
    owner_engine: Engine,
    worker_engine: Engine,
) -> None:
    seeded, other = seed_document(), seed_document()
    run_id, other_run = start(sessions, seeded), start(sessions, other)
    lease = _claim(owner_engine, run_id)
    mine = {"worker": lease, "run": run_id, "tenant": seeded.tenant_id}
    # For another run (even with its own ids in the rows), or with another lease: refused.
    for gucs, target, target_run in (
        (mine, other, other_run),
        (mine | {"worker": uuid.uuid7()}, seeded, run_id),
    ):
        with pytest.raises(DBAPIError) as error, worker_engine.begin() as connection:
            as_worker(connection, **gucs)
            _write_results(connection, target, target_run)
        assert sqlstate(error.value) == INSUFFICIENT_PRIVILEGE
    # Its own: allowed, and the run can finish.
    with worker_engine.begin() as connection:
        as_worker(connection, **mine)
        _write_results(connection, seeded, run_id)
        connection.execute(
            text(
                "UPDATE processing_runs SET status = 'completed', finished_at = now() "
                "WHERE id = :id"
            ),
            {"id": run_id},
        )
    # Nothing more once it is final.
    with pytest.raises(DBAPIError) as error, worker_engine.begin() as connection:
        as_worker(connection, **mine)
        connection.execute(
            text(
                "INSERT INTO extracted_fields (run_id, tenant_id, field_key, status, reason) "
                "VALUES (:r, :t, 'end_date', 'unknown', 'not_found')"
            ),
            {"r": run_id, "t": seeded.tenant_id},
        )
    assert sqlstate(error.value) == INSUFFICIENT_PRIVILEGE


def test_the_worker_cannot_change_a_run_leased_by_another_worker(
    seed_document: Callable[..., Seeded],
    sessions: sessionmaker[Session],
    owner_engine: Engine,
    worker_engine: Engine,
) -> None:
    seeded = seed_document()
    run_id = start(sessions, seeded)
    _claim(owner_engine, run_id)
    with worker_engine.begin() as connection:
        as_worker(connection, worker=uuid.uuid7(), run=run_id, tenant=seeded.tenant_id)
        changed = connection.execute(
            text(
                "UPDATE processing_runs SET status = 'failed', failure_code = 'parse_failed', "
                "finished_at = now() WHERE id = :id"
            ),
            {"id": run_id},
        ).rowcount
    assert changed == 0


# ---------------------------------------------------------------- app_operator


@pytest.mark.parametrize("table", NEW_TABLES)
def test_the_operator_has_no_access(operator_engine: Engine, table: str) -> None:
    with pytest.raises(DBAPIError) as error, operator_engine.begin() as connection:
        connection.execute(text(f"SELECT count(*) FROM {table}"))
    assert sqlstate(error.value) == INSUFFICIENT_PRIVILEGE
