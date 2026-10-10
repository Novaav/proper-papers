"""Tenant row-level security, exercised as app_runtime (never the owner)."""

from collections.abc import Callable, Iterable
from uuid import UUID

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from api.database import create_session_factory, tenant_transaction
from tests.conftest import SeededTenant, insert_user, sqlstate

Seed = Callable[..., SeededTenant]


def _visible_tenants(session: Session, *tenant_ids: UUID) -> set[UUID]:
    rows: Iterable[object] = session.execute(
        text("SELECT id FROM tenants WHERE id = ANY(:ids)"), {"ids": list(tenant_ids)}
    ).scalars()
    return {UUID(str(row)) for row in rows}


def test_runtime_sees_only_its_own_tenant(runtime_engine: Engine, seed_tenant: Seed) -> None:
    a, b = seed_tenant(), seed_tenant()

    with tenant_transaction(create_session_factory(runtime_engine), a.ctx) as session:
        assert _visible_tenants(session, a.ctx.tenant_id, b.ctx.tenant_id) == {a.ctx.tenant_id}
        assert session.execute(text("SELECT count(*) FROM tenants")).scalar_one() == 1
        cross = session.execute(
            text("SELECT name FROM tenants WHERE id = :b"), {"b": b.ctx.tenant_id}
        ).all()
        assert cross == []


def test_without_tenant_context_no_tenant_is_visible(
    runtime_engine: Engine, seed_tenant: Seed
) -> None:
    a = seed_tenant()
    sessions = create_session_factory(runtime_engine)

    with sessions() as session, session.begin():
        assert _visible_tenants(session, a.ctx.tenant_id) == set()
        assert session.execute(text("SELECT count(*) FROM tenants")).scalar_one() == 0


def test_malformed_tenant_setting_fails_closed(runtime_engine: Engine, seed_tenant: Seed) -> None:
    seed_tenant()
    sessions = create_session_factory(runtime_engine)

    with sessions() as session, session.begin():
        session.execute(text("SELECT set_config('app.tenant_id', 'not-a-uuid', true)"))
        with pytest.raises(DBAPIError) as error:
            session.execute(text("SELECT count(*) FROM tenants"))

    assert sqlstate(error.value) == "22P02"


def test_tenant_visibility_ends_with_the_transaction(
    runtime_engine: Engine, seed_tenant: Seed
) -> None:
    a = seed_tenant()
    sessions = create_session_factory(runtime_engine)
    pid = text("SELECT pg_backend_pid()")

    with tenant_transaction(sessions, a.ctx) as session:
        assert _visible_tenants(session, a.ctx.tenant_id) == {a.ctx.tenant_id}
        committed_pid: int = session.execute(pid).scalar_one()
    with sessions() as session, session.begin():
        assert session.execute(pid).scalar_one() == committed_pid
        assert _visible_tenants(session, a.ctx.tenant_id) == set()

    with pytest.raises(RuntimeError), tenant_transaction(sessions, a.ctx) as session:
        rolled_back_pid: int = session.execute(pid).scalar_one()
        raise RuntimeError("abort")
    with sessions() as session, session.begin():
        assert session.execute(pid).scalar_one() == rolled_back_pid
        assert _visible_tenants(session, a.ctx.tenant_id) == set()


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO tenants (name) VALUES ('t')",
        "UPDATE tenants SET name = 'x'",
        "DELETE FROM tenants",
        "ALTER TABLE tenants DISABLE ROW LEVEL SECURITY",
        "ALTER TABLE tenants NO FORCE ROW LEVEL SECURITY",
        "DROP POLICY tenant_member_read ON tenants",
        "ALTER POLICY tenant_member_read ON tenants USING (true)",
        "CREATE POLICY read_all ON tenants FOR SELECT USING (true)",
    ],
)
def test_runtime_cannot_write_tenants_or_change_their_policies(
    runtime_engine: Engine, seed_tenant: Seed, sql: str
) -> None:
    a = seed_tenant()

    with tenant_transaction(create_session_factory(runtime_engine), a.ctx) as session:
        with pytest.raises(DBAPIError) as error, session.begin_nested():
            session.execute(text(sql))
        assert sqlstate(error.value) == "42501"


def test_owner_administers_all_tenants(owner_engine: Engine, seed_tenant: Seed) -> None:
    a, b = seed_tenant(), seed_tenant()

    with owner_engine.connect() as connection:
        visible: Iterable[UUID] = connection.execute(
            text("SELECT id FROM tenants WHERE id = ANY(:ids)"),
            {"ids": [a.ctx.tenant_id, b.ctx.tenant_id]},
        ).scalars()
        assert set(visible) == {a.ctx.tenant_id, b.ctx.tenant_id}


def test_workspace_rls_is_unchanged_by_tenant_rls(
    runtime_engine: Engine, seed_tenant: Seed
) -> None:
    a, b = seed_tenant(), seed_tenant()

    with tenant_transaction(create_session_factory(runtime_engine), a.ctx) as session:
        workspaces: Iterable[UUID] = session.execute(
            text("SELECT id FROM workspaces WHERE id = ANY(:ids)"),
            {"ids": [a.workspace_id, b.workspace_id]},
        ).scalars()
        assert set(workspaces) == {a.workspace_id}
        # The foreign key check to tenants is not subject to row-level security.
        session.execute(
            text("INSERT INTO workspaces (tenant_id, name) VALUES (:t, 'another')"),
            {"t": a.ctx.tenant_id},
        )


# --- Membership (decision D-A14): the tenant row is readable only by its members ----------


def _raw_visible(
    sessions: sessionmaker[Session], tenant_ids: list[UUID], **settings: str
) -> set[UUID]:
    with sessions() as session, session.begin():
        for name, value in settings.items():
            session.execute(
                text("SELECT set_config(:n, :v, true)"), {"n": f"app.{name}", "v": value}
            )
        return _visible_tenants(session, *tenant_ids)


def test_tenant_setting_without_a_member_user_shows_nothing(
    runtime_engine: Engine, owner_engine: Engine, seed_tenant: Seed
) -> None:
    a, b = seed_tenant(), seed_tenant()
    sessions = create_session_factory(runtime_engine)
    with owner_engine.begin() as connection:
        outsider = insert_user(connection)
    ids = [a.ctx.tenant_id, b.ctx.tenant_id]
    tenant = str(a.ctx.tenant_id)

    assert _raw_visible(sessions, ids, tenant_id=tenant) == set()
    assert _raw_visible(sessions, ids, tenant_id=tenant, user_id=str(outsider)) == set()
    # A member of tenant b naming tenant a sees neither.
    assert _raw_visible(sessions, ids, tenant_id=tenant, user_id=str(b.ctx.user_id)) == set()
    assert _raw_visible(sessions, ids, tenant_id=tenant, user_id=str(a.ctx.user_id)) == {
        a.ctx.tenant_id
    }


def test_revoked_member_no_longer_sees_the_tenant(
    runtime_engine: Engine, owner_engine: Engine, seed_tenant: Seed
) -> None:
    a = seed_tenant("member")
    sessions = create_session_factory(runtime_engine)
    with tenant_transaction(sessions, a.ctx) as session:
        assert _visible_tenants(session, a.ctx.tenant_id) == {a.ctx.tenant_id}

    with owner_engine.begin() as connection:
        connection.execute(text("DELETE FROM memberships WHERE user_id = :u"), {"u": a.ctx.user_id})

    with tenant_transaction(sessions, a.ctx) as session:
        assert _visible_tenants(session, a.ctx.tenant_id) == set()


def test_malformed_user_setting_fails_closed_on_tenants(
    runtime_engine: Engine, seed_tenant: Seed
) -> None:
    a = seed_tenant()
    sessions = create_session_factory(runtime_engine)

    with sessions() as session, session.begin():
        session.execute(
            text(
                "SELECT set_config('app.tenant_id', :t, true), set_config('app.user_id', 'x', true)"
            ),
            {"t": str(a.ctx.tenant_id)},
        )
        with pytest.raises(DBAPIError) as error:
            session.execute(text("SELECT count(*) FROM tenants")).scalar_one()

    assert sqlstate(error.value) == "22P02"
