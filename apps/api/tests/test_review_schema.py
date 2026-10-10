"""Migration 0009 below the API: row-level security, privileges and triggers.

These statements are what a compromised or buggy API could send; the database
must refuse them on its own. Each case uses exactly one role's privileges.
"""

import hashlib
import json
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from api.database import DatabaseSettings, tenant_transaction
from api.review import approve, record_decision
from tests.conftest import SeededTenant, insert_user, sqlstate
from tests.processing_support import Seeded, block, start
from tests.review_support import Reviewable, new_pipeline, reviewable_factory

INSUFFICIENT_PRIVILEGE = "42501"
CHECK_VIOLATION = "23514"
UNIQUE_VIOLATION = "23505"
RECORD_CHANGED = "RV001"
Seed = Callable[..., SeededTenant]


@pytest.fixture
def reviewable(
    seed_document: Callable[..., Seeded],
    sessions: sessionmaker[Session],
    worker_sessions: sessionmaker[Session],
) -> Callable[..., Reviewable]:
    return reviewable_factory(seed_document, sessions, worker_sessions)


@pytest.fixture
def operator_engine(operator_settings: DatabaseSettings) -> Iterator[Engine]:
    engine = create_engine(operator_settings.url, poolclass=NullPool)
    yield engine
    engine.dispose()


def _decide(session: Session, target: Reviewable, **overrides: Any) -> None:
    values = {
        "tenant": target.tenant.ctx.tenant_id,
        "run": target.run_id,
        "seq": 1,
        "key": "effective_date",
        "resolution": "candidate",
        "ordinal": 1,
        "user": target.tenant.ctx.user_id,
    } | overrides
    session.execute(
        text(
            "INSERT INTO review_decisions (tenant_id, run_id, seq, field_key, resolution, "
            "candidate_ordinal, decided_by) "
            "VALUES (:tenant, :run, :seq, :key, :resolution, :ordinal, :user)"
        ),
        values,
    )


def _refused(
    sessions: sessionmaker[Session], tenant: SeededTenant, write: Callable[[Session], object]
) -> str | None:
    with pytest.raises(DBAPIError) as raised, tenant_transaction(sessions, tenant.ctx) as session:
        write(session)
    return sqlstate(raised.value)


def _approve_v1(sessions: sessionmaker[Session], target: Reviewable) -> dict[str, Any]:
    """A real approval through the repository; returns its stored record."""
    ctx = target.tenant.ctx
    ids = {"tenant_id": ctx.tenant_id, "user_id": ctx.user_id, "run_id": target.run_id}
    with tenant_transaction(sessions, ctx) as session:
        record_decision(
            session,
            target.tenant.workspace_id,
            target.seeded.document.id,
            **ids,
            expected_seq=0,
            field_key="effective_date",
            resolution="candidate",
            candidate_ordinal=1,
        )
    with tenant_transaction(sessions, ctx) as session:
        approve(
            session,
            target.tenant.workspace_id,
            target.seeded.document.id,
            **ids,
            expected_seq=1,
            expected_version=0,
        )
        body: str = session.execute(
            text("SELECT record_json FROM approved_records WHERE run_id = :run"),
            {"run": target.run_id},
        ).scalar_one()
    record: dict[str, Any] = json.loads(body)
    return record


def _insert_approval(
    session: Session,
    target: Reviewable,
    record: dict[str, Any],
    *,
    version: int = 2,
    decisions_through: int = 1,
    run_id: UUID | None = None,
    tamper: Callable[[dict[str, Any]], None] | None = None,
    digest: bytes | None = None,
) -> None:
    """Insert `record` as an approval row, made consistent with that row unless tampered."""
    record = json.loads(json.dumps(record))
    approved_at: datetime = session.execute(text("SELECT now()")).scalar_one()
    record["approval"].update(
        version=version,
        decisions_through=decisions_through,
        approved_at=approved_at.isoformat(),
        approved_by=str(target.tenant.ctx.user_id),
    )
    record["processing"]["run_id"] = str(run_id or target.run_id)
    record["document"].update(
        id=str(target.seeded.document.id),
        version_id=str(target.seeded.document.version_id),
        sha256=target.seeded.document.sha256.hex(),
    )
    if tamper is not None:
        tamper(record)
    body = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    session.execute(
        text(
            "INSERT INTO approved_records (tenant_id, document_id, version, run_id, "
            "document_version_id, decisions_through, record_json, record_sha256, approved_by) "
            "VALUES (:tenant, :document, :version, :run, :doc_version, :through, :body, "
            ":digest, :user)"
        ),
        {
            "tenant": target.tenant.ctx.tenant_id,
            "document": target.seeded.document.id,
            "version": version,
            "run": run_id or target.run_id,
            "doc_version": target.seeded.document.version_id,
            "through": decisions_through,
            "body": body,
            "digest": digest or hashlib.sha256(body.encode()).digest(),
            "user": target.tenant.ctx.user_id,
        },
    )


# --- The reviewer role --------------------------------------------------------------------------


def test_memberships_accept_exactly_owner_reviewer_and_member(
    owner_engine: Engine, seed_tenant: Seed
) -> None:
    tenant = seed_tenant()
    with owner_engine.begin() as connection:
        for role in ("reviewer", "member"):
            user = insert_user(connection)
            connection.execute(
                text("INSERT INTO memberships (tenant_id, user_id, role) VALUES (:t, :u, :r)"),
                {"t": tenant.ctx.tenant_id, "u": user, "r": role},
            )
    with pytest.raises(DBAPIError) as raised, owner_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO memberships (tenant_id, user_id, role) VALUES (:t, :u, 'admin')"),
            {"t": tenant.ctx.tenant_id, "u": insert_user(connection)},
        )
    assert sqlstate(raised.value) == CHECK_VIOLATION


def test_members_cannot_write_reviews_even_without_the_api(
    reviewable: Callable[..., Reviewable],
    seed_tenant: Seed,
    sessions: sessionmaker[Session],
) -> None:
    member = seed_tenant("member")
    target = reviewable(tenant=member)
    assert _refused(sessions, member, lambda s: _decide(s, target)) == INSUFFICIENT_PRIVILEGE

    owner = reviewable()
    record = _approve_v1(sessions, owner)
    # Same tenant, member role: the approval insert is refused as well.
    member_target = reviewable(tenant=member)
    assert (
        _refused(
            sessions,
            member,
            lambda s: _insert_approval(s, member_target, record, version=1, decisions_through=0),
        )
        == INSUFFICIENT_PRIVILEGE
    )


def test_reviewers_write_only_as_themselves(
    reviewable: Callable[..., Reviewable],
    seed_tenant: Seed,
    sessions: sessionmaker[Session],
    owner_engine: Engine,
) -> None:
    reviewer = seed_tenant("reviewer")
    target = reviewable(tenant=reviewer)
    with owner_engine.begin() as connection:
        someone_else = insert_user(connection)
    assert (
        _refused(sessions, reviewer, lambda s: _decide(s, target, user=someone_else))
        == INSUFFICIENT_PRIVILEGE
    )
    with tenant_transaction(sessions, reviewer.ctx) as session:
        _decide(session, target)


def test_other_tenants_neither_see_nor_write_reviews(
    reviewable: Callable[..., Reviewable], seed_tenant: Seed, sessions: sessionmaker[Session]
) -> None:
    target = reviewable()
    with tenant_transaction(sessions, target.tenant.ctx) as session:
        _decide(session, target)
    other = seed_tenant("reviewer")
    with tenant_transaction(sessions, other.ctx) as session:
        assert session.execute(text("SELECT count(*) FROM review_decisions")).scalar_one() == 0
    # Naming the other tenant is refused (the trigger cannot see the run, and row-level
    # security would refuse the row) ...
    assert _refused(sessions, other, lambda s: _decide(s, target, seq=2)) in (
        CHECK_VIOLATION,
        INSUFFICIENT_PRIVILEGE,
    )
    # ... and naming their own tenant with the foreign run finds no such run.
    assert _refused(
        sessions, other, lambda s: _decide(s, target, seq=2, tenant=other.ctx.tenant_id)
    ) in (CHECK_VIOLATION, "23503")


@pytest.mark.parametrize("column", ["id", "decided_at"])
def test_decision_ids_and_times_are_set_by_the_database(
    reviewable: Callable[..., Reviewable], sessions: sessionmaker[Session], column: str
) -> None:
    target = reviewable()

    def insert(session: Session) -> None:
        value = "uuidv7()" if column == "id" else "now() - interval '1 day'"
        session.execute(
            text(
                f"INSERT INTO review_decisions (tenant_id, run_id, seq, field_key, resolution, "
                f"decided_by, {column}) VALUES (:t, :r, 1, 'end_date', 'unknown', :u, {value})"
            ),
            {"t": target.tenant.ctx.tenant_id, "r": target.run_id, "u": target.tenant.ctx.user_id},
        )

    assert _refused(sessions, target.tenant, insert) == INSUFFICIENT_PRIVILEGE


# --- Append-only --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE review_decisions SET resolution = 'unknown', candidate_ordinal = NULL",
        "DELETE FROM review_decisions",
        "UPDATE approved_records SET decisions_through = 0",
        "DELETE FROM approved_records",
    ],
)
def test_the_runtime_cannot_change_or_remove_reviews(
    reviewable: Callable[..., Reviewable], sessions: sessionmaker[Session], statement: str
) -> None:
    target = reviewable()
    _approve_v1(sessions, target)
    assert _refused(sessions, target.tenant, lambda s: s.execute(text(statement))) == (
        INSUFFICIENT_PRIVILEGE
    )


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE review_decisions SET resolution = 'unknown', candidate_ordinal = NULL "
        "WHERE run_id = :run",
        "DELETE FROM review_decisions WHERE run_id = :run",
        "UPDATE approved_records SET record_json = record_json WHERE run_id = :run",
        "DELETE FROM approved_records WHERE run_id = :run",
        "TRUNCATE review_decisions CASCADE",
        "TRUNCATE approved_records",
    ],
)
def test_not_even_the_schema_owner_can_change_or_remove_reviews(
    reviewable: Callable[..., Reviewable],
    sessions: sessionmaker[Session],
    owner_engine: Engine,
    statement: str,
) -> None:
    target = reviewable()
    _approve_v1(sessions, target)
    with pytest.raises(DBAPIError) as raised, owner_engine.begin() as connection:
        connection.execute(text(statement), {"run": target.run_id})
    assert sqlstate(raised.value) == CHECK_VIOLATION


def test_the_worker_and_the_operator_have_no_access(
    worker_engine: Engine, operator_engine: Engine
) -> None:
    for engine in (worker_engine, operator_engine):
        for table in ("review_decisions", "approved_records"):
            with pytest.raises(DBAPIError) as raised, engine.connect() as connection:
                connection.execute(text(f"SELECT count(*) FROM {table}"))
            assert sqlstate(raised.value) == INSUFFICIENT_PRIVILEGE


# --- Decision rules -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"seq": 2}, UNIQUE_VIOLATION),  # not previous + 1
        ({"key": "end_date", "ordinal": 0}, CHECK_VIOLATION),  # not found: no candidate
        ({"key": "contract_value", "ordinal": 0}, CHECK_VIOLATION),  # candidate without a value
        ({"key": "effective_date", "ordinal": 7}, CHECK_VIOLATION),  # no such candidate
        ({"resolution": "unknown"}, CHECK_VIOLATION),  # unknown with an ordinal
        ({"resolution": "candidate", "ordinal": None}, CHECK_VIOLATION),
        ({"key": "notes"}, CHECK_VIOLATION),
    ],
)
def test_decisions_follow_the_field_and_the_sequence(
    reviewable: Callable[..., Reviewable],
    sessions: sessionmaker[Session],
    overrides: dict[str, Any],
    expected: str,
) -> None:
    target = reviewable()
    assert _refused(sessions, target.tenant, lambda s: _decide(s, target, **overrides)) == expected


def test_only_runs_that_completed_or_need_review_can_be_reviewed(
    seed_document: Callable[..., Seeded], sessions: sessionmaker[Session]
) -> None:
    seeded = seed_document()
    queued = Reviewable(seeded, start(sessions, seeded, new_pipeline()))

    def decide(session: Session) -> None:
        _decide(session, queued, key="end_date", resolution="unknown", ordinal=None)

    assert _refused(sessions, seeded.tenant, decide) == CHECK_VIOLATION


# --- Approval rules -----------------------------------------------------------------------------


def _tamper_field(key: str, **changes: Any) -> Callable[[dict[str, Any]], None]:
    def tamper(record: dict[str, Any]) -> None:
        record["fields"][key].update(changes)

    return tamper


@pytest.mark.parametrize(
    ("tamper", "expected"),
    [
        (_tamper_field("provider_name", evidence=[]), CHECK_VIOLATION),
        (_tamper_field("provider_name", value=None), CHECK_VIOLATION),
        (_tamper_field("end_date", value="2027-01-01"), CHECK_VIOLATION),
        (_tamper_field("end_date", reason=None), CHECK_VIOLATION),
        (_tamper_field("effective_date", status="conflict"), CHECK_VIOLATION),
        (lambda r: r["fields"].pop("notice_period_days"), CHECK_VIOLATION),
        (lambda r: r["fields"].update(notes=r["fields"]["end_date"]), CHECK_VIOLATION),
        (lambda r: r.update(schema="approved_record.v2"), CHECK_VIOLATION),
        (
            lambda r: r["document"].update(id="00000000-0000-7000-8000-000000000001"),
            CHECK_VIOLATION,
        ),
        (lambda r: r["approval"].update(approved_at="2020-01-01T00:00:00Z"), CHECK_VIOLATION),
        (lambda r: r["approval"].update(version=9), CHECK_VIOLATION),
    ],
)
def test_an_approved_record_must_describe_its_row_and_ground_every_value(
    reviewable: Callable[..., Reviewable],
    sessions: sessionmaker[Session],
    tamper: Callable[[dict[str, Any]], None],
    expected: str,
) -> None:
    target = reviewable()
    record = _approve_v1(sessions, target)
    assert (
        _refused(
            sessions, target.tenant, lambda s: _insert_approval(s, target, record, tamper=tamper)
        )
        == expected
    )


def test_a_consistent_next_version_is_accepted(
    reviewable: Callable[..., Reviewable], sessions: sessionmaker[Session]
) -> None:
    target = reviewable()
    record = _approve_v1(sessions, target)
    with tenant_transaction(sessions, target.tenant.ctx) as session:
        _insert_approval(session, target, record)
        assert (
            session.execute(
                text("SELECT max(version) FROM approved_records WHERE run_id = :run"),
                {"run": target.run_id},
            ).scalar_one()
            == 2
        )


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"version": 3}, UNIQUE_VIOLATION),
        ({"version": 1}, UNIQUE_VIOLATION),
        ({"decisions_through": 0}, RECORD_CHANGED),
        ({"digest": b"\x00" * 32}, CHECK_VIOLATION),
    ],
)
def test_approvals_follow_the_versions_the_decisions_and_the_hash(
    reviewable: Callable[..., Reviewable],
    sessions: sessionmaker[Session],
    changes: dict[str, Any],
    expected: str,
) -> None:
    target = reviewable()
    record = _approve_v1(sessions, target)
    assert (
        _refused(sessions, target.tenant, lambda s: _insert_approval(s, target, record, **changes))
        == expected
    )


def test_an_approval_after_a_newer_decision_is_refused(
    reviewable: Callable[..., Reviewable], sessions: sessionmaker[Session]
) -> None:
    # What happens when a decision commits between the API's check and its insert.
    target = reviewable()
    record = _approve_v1(sessions, target)
    with tenant_transaction(sessions, target.tenant.ctx) as session:
        _decide(session, target, seq=2, key="provider_name", ordinal=0)
    assert (
        _refused(sessions, target.tenant, lambda s: _insert_approval(s, target, record))
        == RECORD_CHANGED
    )


def test_an_approval_names_a_run_of_this_document(
    reviewable: Callable[..., Reviewable], sessions: sessionmaker[Session]
) -> None:
    target = reviewable()
    record = _approve_v1(sessions, target)
    other = reviewable(tenant=target.tenant)
    assert (
        _refused(
            sessions,
            target.tenant,
            lambda s: _insert_approval(s, target, record, run_id=other.run_id, decisions_through=0),
        )
        == CHECK_VIOLATION
    )


def test_approved_records_are_hidden_once_the_version_is_blocked(
    reviewable: Callable[..., Reviewable], sessions: sessionmaker[Session], owner_engine: Engine
) -> None:
    target = reviewable()
    _approve_v1(sessions, target)
    block(owner_engine, target.seeded)
    with tenant_transaction(sessions, target.tenant.ctx) as session:
        assert session.execute(text("SELECT count(*) FROM approved_records")).scalar_one() == 0
        # Decisions carry no document text and stay visible.
        assert session.execute(text("SELECT count(*) FROM review_decisions")).scalar_one() == 1
