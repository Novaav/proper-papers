"""Migration 0008: run state machine, completeness at commit, constraints (as the owner).

The owner bypasses row-level security (admin policies) but not the triggers or
constraints, so these tests isolate the database rules themselves.
"""

import hashlib
import uuid
from collections.abc import Callable
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from api.models import FIELD_KEYS
from tests.conftest import sqlstate
from tests.processing_support import Seeded, run_row, start

CHECK_VIOLATION = "23514"


@pytest.fixture
def run(
    seed_document: Callable[..., Seeded], sessions: sessionmaker[Session]
) -> tuple[Seeded, UUID]:
    seeded = seed_document()
    return seeded, start(sessions, seeded)


def _update(owner_engine: Engine, run_id: UUID, assignments: str, **values: Any) -> None:
    with owner_engine.begin() as connection:
        connection.execute(
            text(f"UPDATE processing_runs SET {assignments} WHERE id = :id"),
            {"id": run_id, **values},
        )


def _claim(
    owner_engine: Engine, run_id: UUID, *, expires: str = "now() + interval '5 minutes'"
) -> UUID:
    lease = uuid.uuid7()
    _update(
        owner_engine,
        run_id,
        "status = 'running', attempts = attempts + 1, lease_owner = :lease, "
        f"lease_expires_at = {expires}",
        lease=lease,
    )
    return lease


def _refused(owner_engine: Engine, run_id: UUID, assignments: str, **values: Any) -> str | None:
    with pytest.raises(DBAPIError) as error:
        _update(owner_engine, run_id, assignments, **values)
    return sqlstate(error.value)


def _results(
    connection: Connection, seeded: Seeded, run_id: UUID, fields: int = 7, pages: int = 1
) -> None:
    for number in range(1, pages + 1):
        connection.execute(
            text(
                "INSERT INTO document_pages (run_id, tenant_id, page_number, text, text_sha256, "
                "off_page_chars, invisible_chars, undefined_chars, control_chars_removed) "
                "VALUES (:run, :tenant, :n, 'Provider: A Ltd', :digest, 0, 0, 0, 0)"
            ),
            {
                "run": run_id,
                "tenant": seeded.tenant_id,
                "n": number,
                "digest": hashlib.sha256(b"x").digest(),
            },
        )
    for key in FIELD_KEYS[:fields]:
        connection.execute(
            text(
                "INSERT INTO extracted_fields (run_id, tenant_id, field_key, status, reason) "
                "VALUES (:run, :tenant, :key, 'unknown', 'not_found')"
            ),
            {"run": run_id, "tenant": seeded.tenant_id, "key": key},
        )


def test_a_new_run_is_queued_with_no_attempts(
    run: tuple[Seeded, UUID], owner_engine: Engine
) -> None:
    row = run_row(owner_engine, run[1])
    assert (row["status"], row["attempts"], row["lease_owner"], row["finished_at"]) == (
        "queued",
        0,
        None,
        None,
    )


def test_legal_path_queued_running_retry_running_failed(
    run: tuple[Seeded, UUID], owner_engine: Engine
) -> None:
    _, run_id = run
    _claim(owner_engine, run_id)
    _update(
        owner_engine,
        run_id,
        "status = 'queued', lease_owner = NULL, lease_expires_at = NULL, not_before = now()",
    )
    _claim(owner_engine, run_id)
    _update(
        owner_engine,
        run_id,
        "status = 'failed', failure_code = 'parse_failed', finished_at = now()",
    )
    assert run_row(owner_engine, run_id)["attempts"] == 2


def test_expired_lease_can_be_taken_over_but_a_live_one_cannot(
    seed_document: Callable[..., Seeded], sessions: sessionmaker[Session], owner_engine: Engine
) -> None:
    live = start(sessions, seed_document())
    _claim(owner_engine, live)
    takeover = "attempts = attempts + 1, lease_owner = :l"
    assert _refused(owner_engine, live, takeover, l=uuid.uuid7()) == CHECK_VIOLATION
    # A lease cannot be shortened or renewed either: only a new attempt changes it.
    assert _refused(owner_engine, live, "lease_expires_at = now()") == CHECK_VIOLATION

    expired = start(sessions, seed_document())
    _claim(owner_engine, expired, expires="now() - interval '1 second'")
    _update(owner_engine, expired, takeover, l=uuid.uuid7())
    assert run_row(owner_engine, expired)["attempts"] == 2


@pytest.mark.parametrize(
    "assignments",
    [
        "status = 'completed', finished_at = now()",  # queued cannot finish
        # Running without counting an attempt, or counting two.
        "status = 'running', lease_owner = gen_random_uuid(), lease_expires_at = now()",
        "status = 'running', attempts = 2, lease_owner = gen_random_uuid(), "
        "lease_expires_at = now()",
        "pipeline_version = 'other'",
        "version_id = gen_random_uuid()",
        "created_at = now() - interval '1 day'",
    ],
)
def test_illegal_changes_of_a_queued_run(
    run: tuple[Seeded, UUID], owner_engine: Engine, assignments: str
) -> None:
    assert _refused(owner_engine, run[1], assignments) in (CHECK_VIOLATION, "23503")


def test_attempts_are_capped_at_three(run: tuple[Seeded, UUID], owner_engine: Engine) -> None:
    _, run_id = run
    for _ in range(3):
        _claim(owner_engine, run_id, expires="now() - interval '1 second'")
        if run_row(owner_engine, run_id)["attempts"] < 3:
            _update(
                owner_engine,
                run_id,
                "status = 'queued', lease_owner = NULL, lease_expires_at = NULL",
            )
    assert run_row(owner_engine, run_id)["attempts"] == 3
    # No retry from the last attempt, and no fourth attempt (takeover of the expired lease).
    assert (
        _refused(
            owner_engine, run_id, "status = 'queued', lease_owner = NULL, lease_expires_at = NULL"
        )
        == CHECK_VIOLATION
    )
    assert _refused(owner_engine, run_id, "attempts = attempts + 1") == CHECK_VIOLATION


@pytest.mark.parametrize("final", ["failed", "completed", "needs_review"])
def test_final_states_never_change(
    run: tuple[Seeded, UUID], owner_engine: Engine, final: str
) -> None:
    seeded, run_id = run
    _claim(owner_engine, run_id)
    with owner_engine.begin() as connection:
        if final != "failed":
            _results(connection, seeded, run_id)
        connection.execute(
            text(
                "UPDATE processing_runs SET status = :s, finished_at = now(), "
                "failure_code = CASE WHEN :s = 'failed' THEN 'parse_failed' END WHERE id = :id"
            ),
            {"s": final, "id": run_id},
        )
    for assignments in (
        "status = 'queued', lease_owner = NULL, lease_expires_at = NULL, finished_at = NULL, "
        "failure_code = NULL",
        "not_before = now()",
        "parser_versions = '{}'",
    ):
        assert _refused(owner_engine, run_id, assignments) == CHECK_VIOLATION


@pytest.mark.parametrize(("fields", "pages"), [(6, 1), (0, 1), (7, 0)])
def test_a_finished_run_needs_its_pages_and_seven_fields_at_commit(
    run: tuple[Seeded, UUID], owner_engine: Engine, fields: int, pages: int
) -> None:
    seeded, run_id = run
    _claim(owner_engine, run_id)
    with pytest.raises(DBAPIError) as error, owner_engine.begin() as connection:
        _results(connection, seeded, run_id, fields=fields, pages=pages)
        connection.execute(
            text(
                "UPDATE processing_runs SET status = 'completed', finished_at = now() "
                "WHERE id = :id"
            ),
            {"id": run_id},
        )
    assert sqlstate(error.value) == CHECK_VIOLATION
    assert run_row(owner_engine, run_id)["status"] == "running"


def test_a_complete_run_commits(run: tuple[Seeded, UUID], owner_engine: Engine) -> None:
    seeded, run_id = run
    _claim(owner_engine, run_id)
    with owner_engine.begin() as connection:
        _results(connection, seeded, run_id)
        connection.execute(
            text(
                "UPDATE processing_runs SET status = 'needs_review', finished_at = now() "
                "WHERE id = :id"
            ),
            {"id": run_id},
        )
    assert run_row(owner_engine, run_id)["status"] == "needs_review"


@pytest.mark.parametrize(
    "assignments",
    [
        "status = 'failed', finished_at = now()",  # failed needs a code
        "status = 'failed', failure_code = 'made_up', finished_at = now()",
        "status = 'failed', failure_code = 'parse_failed'",  # final needs finished_at
    ],
)
def test_state_constraints(
    run: tuple[Seeded, UUID], owner_engine: Engine, assignments: str
) -> None:
    _claim(owner_engine, run[1])
    assert _refused(owner_engine, run[1], assignments) == CHECK_VIOLATION


def test_one_run_per_version_and_pipeline(run: tuple[Seeded, UUID], owner_engine: Engine) -> None:
    seeded, run_id = run
    row = run_row(owner_engine, run_id)
    with pytest.raises(IntegrityError), owner_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO processing_runs (tenant_id, version_id, pipeline_version, created_by) "
                "VALUES (:t, :v, :p, :u)"
            ),
            {
                "t": seeded.tenant_id,
                "v": row["version_id"],
                "p": row["pipeline_version"],
                "u": row["created_by"],
            },
        )


@pytest.mark.parametrize(
    ("column", "value"),
    [("char_start", -1), ("char_end", 0), ("quote", "wrong length")],
)
def test_evidence_ranges_must_match_their_quotes(
    run: tuple[Seeded, UUID], owner_engine: Engine, column: str, value: object
) -> None:
    seeded, run_id = run
    evidence = {"char_start": 0, "char_end": 15, "quote": "Provider: A Ltd"} | {column: value}
    with pytest.raises(IntegrityError), owner_engine.begin() as connection:
        _results(connection, seeded, run_id)
        candidate = uuid.uuid7()
        connection.execute(
            text(
                "INSERT INTO field_candidates "
                "(id, run_id, tenant_id, field_key, ordinal, value, raw) "
                "VALUES (:id, :run, :tenant, 'provider_name', 0, '\"A Ltd\"', 'A Ltd')"
            ),
            {"id": candidate, "run": run_id, "tenant": seeded.tenant_id},
        )
        connection.execute(
            text(
                "INSERT INTO candidate_evidence "
                "(candidate_id, ordinal, run_id, tenant_id, page_number, "
                "char_start, char_end, quote) VALUES (:c, 0, :run, :tenant, 1, :s, :e, :q)"
            ),
            {
                "c": candidate,
                "run": run_id,
                "tenant": seeded.tenant_id,
                "s": evidence["char_start"],
                "e": evidence["char_end"],
                "q": evidence["quote"],
            },
        )
