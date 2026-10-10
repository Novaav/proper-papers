"""Tenant isolation on workspaces, exercised as app_runtime (never the owner)."""

from collections.abc import Callable, Iterable
from uuid import UUID

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from api.database import create_session_factory, tenant_transaction
from tests.conftest import SeededTenant, sqlstate

Seed = Callable[..., SeededTenant]
INSUFFICIENT_PRIVILEGE = "42501"


def _visible(session: Session, *workspace_ids: UUID) -> set[UUID]:
    rows: Iterable[UUID] = session.execute(
        text("SELECT id FROM workspaces WHERE id = ANY(:ids)"), {"ids": list(workspace_ids)}
    ).scalars()
    return set(rows)


def _expect_denied(session: Session, sql: str, **params: object) -> str:
    with pytest.raises(DBAPIError) as error, session.begin_nested():
        session.execute(text(sql), params)
    assert sqlstate(error.value) == INSUFFICIENT_PRIVILEGE
    return str(error.value.orig)


def _owner_workspace_name(owner_engine: Engine, seeded: SeededTenant) -> str | None:
    with owner_engine.begin() as connection:
        return connection.execute(
            text("SELECT name FROM workspaces WHERE id = :w"), {"w": seeded.workspace_id}
        ).scalar()


def test_select_returns_only_the_current_tenants_workspaces(
    runtime_engine: Engine, seed_tenant: Seed
) -> None:
    a, b = seed_tenant(), seed_tenant()

    with tenant_transaction(create_session_factory(runtime_engine), a.ctx) as session:
        assert _visible(session, a.workspace_id, b.workspace_id) == {a.workspace_id}
        assert (
            session.execute(
                text("SELECT count(*) FROM workspaces WHERE tenant_id <> :t"),
                {"t": a.ctx.tenant_id},
            ).scalar_one()
            == 0
        )


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE workspaces SET name = 'renamed' WHERE id = :w",
        "DELETE FROM workspaces WHERE id = :w",
    ],
)
@pytest.mark.parametrize("target", ["own", "other"])
def test_runtime_cannot_update_or_delete_any_workspace(
    runtime_engine: Engine, owner_engine: Engine, seed_tenant: Seed, sql: str, target: str
) -> None:
    # UPDATE and DELETE are not granted until endpoints need them (decision D-A6).
    a, b = seed_tenant(), seed_tenant()
    victim = a if target == "own" else b

    with tenant_transaction(create_session_factory(runtime_engine), a.ctx) as session:
        message = _expect_denied(session, sql, w=victim.workspace_id)

    assert "permission denied for table workspaces" in message
    assert _owner_workspace_name(owner_engine, victim) == "Workspace"


def test_insert_for_another_tenant_is_rejected_by_with_check(
    runtime_engine: Engine, seed_tenant: Seed
) -> None:
    a, b = seed_tenant(), seed_tenant()

    with tenant_transaction(create_session_factory(runtime_engine), a.ctx) as session:
        message = _expect_denied(
            session,
            "INSERT INTO workspaces (tenant_id, name) VALUES (:t, 'w')",
            t=b.ctx.tenant_id,
        )
        session.execute(
            text("INSERT INTO workspaces (tenant_id, name) VALUES (:t, 'mine')"),
            {"t": a.ctx.tenant_id},
        )

    assert "row-level security policy" in message


def test_workspace_cannot_be_moved_to_another_tenant(
    runtime_engine: Engine, seed_tenant: Seed
) -> None:
    a, b = seed_tenant(), seed_tenant()

    with tenant_transaction(create_session_factory(runtime_engine), a.ctx) as session:
        _expect_denied(
            session,
            "UPDATE workspaces SET tenant_id = :b WHERE id = :w",
            b=b.ctx.tenant_id,
            w=a.workspace_id,
        )


def test_without_tenant_context_nothing_is_visible_and_writes_fail(
    runtime_engine: Engine, seed_tenant: Seed
) -> None:
    a = seed_tenant()
    sessions = create_session_factory(runtime_engine)

    with sessions() as session, session.begin():
        assert _visible(session, a.workspace_id) == set()
        assert session.execute(text("SELECT count(*) FROM workspaces")).scalar_one() == 0
        message = _expect_denied(
            session,
            "INSERT INTO workspaces (tenant_id, name) VALUES (:t, 'w')",
            t=a.ctx.tenant_id,
        )

    assert "row-level security policy" in message


def test_malformed_tenant_setting_fails_instead_of_matching(
    runtime_engine: Engine, seed_tenant: Seed
) -> None:
    seed_tenant()
    sessions = create_session_factory(runtime_engine)

    with sessions() as session, session.begin():
        session.execute(text("SELECT set_config('app.tenant_id', 'not-a-uuid', true)"))
        with pytest.raises(DBAPIError) as error:
            session.execute(text("SELECT count(*) FROM workspaces"))

    assert sqlstate(error.value) == "22P02"  # invalid_text_representation


def test_tenant_context_does_not_survive_commit_or_rollback(
    runtime_engine: Engine, seed_tenant: Seed
) -> None:
    a = seed_tenant()
    sessions = create_session_factory(runtime_engine)
    pid = text("SELECT pg_backend_pid()")

    with tenant_transaction(sessions, a.ctx) as session:
        assert _visible(session, a.workspace_id) == {a.workspace_id}
        committed_pid: int = session.execute(pid).scalar_one()
    with sessions() as session, session.begin():
        assert session.execute(pid).scalar_one() == committed_pid
        assert _visible(session, a.workspace_id) == set()

    with pytest.raises(RuntimeError), tenant_transaction(sessions, a.ctx) as session:
        assert _visible(session, a.workspace_id) == {a.workspace_id}
        rolled_back_pid: int = session.execute(pid).scalar_one()
        raise RuntimeError("abort")
    with sessions() as session, session.begin():
        assert session.execute(pid).scalar_one() == rolled_back_pid
        assert _visible(session, a.workspace_id) == set()


@pytest.mark.parametrize(
    "ddl",
    [
        "ALTER TABLE workspaces DISABLE ROW LEVEL SECURITY",
        "ALTER TABLE workspaces NO FORCE ROW LEVEL SECURITY",
        "DROP POLICY workspace_member_access ON workspaces",
        "DROP POLICY workspaces_admin ON workspaces",
        "ALTER POLICY workspace_member_access ON workspaces USING (true)",
        "CREATE POLICY allow_all ON workspaces USING (true)",
        "ALTER TABLE workspaces ADD COLUMN extra text",
        "DROP TABLE workspaces",
        "CREATE TABLE app.extra (id int)",
        "SET ROLE app_owner",
    ],
)
def test_runtime_role_cannot_change_rls_or_schema(runtime_engine: Engine, ddl: str) -> None:
    sessions = create_session_factory(runtime_engine)

    with sessions() as session, session.begin():
        _expect_denied(session, ddl)


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO tenants (name) VALUES ('t')",
        "UPDATE tenants SET name = 'x'",
        "DELETE FROM tenants",
        "SELECT * FROM alembic_version",
    ],
)
def test_runtime_role_can_only_read_tenants(runtime_engine: Engine, sql: str) -> None:
    sessions = create_session_factory(runtime_engine)

    with sessions() as session, session.begin():
        session.execute(text("SELECT id, name, created_at FROM tenants LIMIT 1"))
        _expect_denied(session, sql)


def test_runtime_role_cannot_grant_access_to_others(
    runtime_engine: Engine, owner_engine: Engine
) -> None:
    # PostgreSQL answers a non-owner GRANT with a warning, not an error; what
    # matters is that nothing is granted.
    sessions = create_session_factory(runtime_engine)
    with sessions() as session, session.begin():
        session.execute(text("GRANT SELECT, INSERT ON workspaces TO PUBLIC"))

    with owner_engine.connect() as connection:
        granted: bool = connection.execute(
            text(
                "SELECT has_table_privilege('public', 'app.workspaces', 'SELECT') "
                "OR has_table_privilege('public', 'app.workspaces', 'INSERT')"
            )
        ).scalar_one()
    assert granted is False
