"""Workspace access requires membership (decision D-A6), exercised as app_runtime.

The runtime sees and creates a workspace only when the transaction's tenant is
the workspace's tenant AND the transaction's user is a member of that tenant.
Contexts here are built directly (tests only); in the API only authorization
builds them, after checking the membership.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from api.database import TenantContext, create_session_factory, tenant_transaction
from api.workspaces import create_workspace, list_workspaces
from tests.conftest import SeededTenant, insert_membership, insert_user, sqlstate

Seed = Callable[..., SeededTenant]
INSUFFICIENT_PRIVILEGE = "42501"
INSERTS = [
    "INSERT INTO workspaces (tenant_id, name) VALUES (:t, 'w')",
    "INSERT INTO workspaces (tenant_id, name) VALUES (:t, 'w') RETURNING id",
]


@pytest.fixture
def sessions(runtime_engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(runtime_engine)


@contextmanager
def _raw(sessions: sessionmaker[Session], **settings: str) -> Iterator[Session]:
    """A runtime transaction with exactly the given app.* settings (no TenantContext)."""
    with sessions() as session, session.begin():
        for name, value in settings.items():
            session.execute(
                text("SELECT set_config(:n, :v, true)"), {"n": f"app.{name}", "v": value}
            )
        yield session


def _visible(session: Session) -> set[UUID]:
    return set(session.execute(text("SELECT id FROM workspaces")).scalars())


def _expect_insert_denied(session: Session, tenant_id: UUID) -> None:
    for sql in INSERTS:
        with pytest.raises(DBAPIError) as error, session.begin_nested():
            session.execute(text(sql), {"t": tenant_id})
        assert sqlstate(error.value) == INSUFFICIENT_PRIVILEGE
        assert "row-level security policy" in str(error.value.orig)


def _new_user(owner_engine: Engine, *memberships: tuple[UUID, str]) -> UUID:
    with owner_engine.begin() as connection:
        user_id = insert_user(connection)
        for tenant_id, role in memberships:
            insert_membership(connection, tenant_id, user_id, role)
    return user_id


def _owner_count(owner_engine: Engine, tenant_id: UUID) -> int:
    with owner_engine.connect() as connection:
        count: int = connection.execute(
            text("SELECT count(*) FROM workspaces WHERE tenant_id = :t"), {"t": tenant_id}
        ).scalar_one()
    return count


@pytest.mark.parametrize("role", ["owner", "member"])
def test_owners_and_members_read_and_create_in_their_tenant(
    sessions: sessionmaker[Session], owner_engine: Engine, seed_tenant: Seed, role: str
) -> None:
    a = seed_tenant(role)

    with tenant_transaction(sessions, a.ctx) as session:
        assert _visible(session) == {a.workspace_id}
        for sql in INSERTS:
            session.execute(text(sql), {"t": a.ctx.tenant_id})
        create_workspace(session, "via repository")

    assert _owner_count(owner_engine, a.ctx.tenant_id) == 4


def test_tenant_without_user_sees_nothing_and_cannot_insert(
    sessions: sessionmaker[Session], owner_engine: Engine, seed_tenant: Seed
) -> None:
    a = seed_tenant()

    with _raw(sessions, tenant_id=str(a.ctx.tenant_id)) as session:
        assert _visible(session) == set()
        _expect_insert_denied(session, a.ctx.tenant_id)
    with _raw(sessions, tenant_id=str(a.ctx.tenant_id), user_id="") as session:
        assert _visible(session) == set()
        _expect_insert_denied(session, a.ctx.tenant_id)

    assert _owner_count(owner_engine, a.ctx.tenant_id) == 1


def test_user_without_membership_sees_nothing_and_cannot_insert(
    sessions: sessionmaker[Session], owner_engine: Engine, seed_tenant: Seed
) -> None:
    a = seed_tenant()
    outsider = _new_user(owner_engine)
    unknown_user = uuid4()

    for user_id in (outsider, unknown_user):
        ctx = TenantContext(user_id=user_id, tenant_id=a.ctx.tenant_id, role="owner")
        with tenant_transaction(sessions, ctx) as session:
            assert _visible(session) == set()
            _expect_insert_denied(session, a.ctx.tenant_id)

    assert _owner_count(owner_engine, a.ctx.tenant_id) == 1


def test_member_of_one_tenant_gets_nothing_with_another_tenants_context(
    sessions: sessionmaker[Session], owner_engine: Engine, seed_tenant: Seed
) -> None:
    # A context naming a tenant the user is not a member of (an authorization
    # bug) must not expose that tenant, nor the user's own tenant.
    a, b = seed_tenant(), seed_tenant()
    mismatched = TenantContext(user_id=a.ctx.user_id, tenant_id=b.ctx.tenant_id, role="owner")

    with tenant_transaction(sessions, mismatched) as session:
        assert _visible(session) == set()
        _expect_insert_denied(session, b.ctx.tenant_id)
        _expect_insert_denied(session, a.ctx.tenant_id)

    assert (
        _owner_count(owner_engine, a.ctx.tenant_id),
        _owner_count(owner_engine, b.ctx.tenant_id),
    ) == (1, 1)


def test_member_of_two_tenants_sees_only_the_context_tenant(
    sessions: sessionmaker[Session], owner_engine: Engine, seed_tenant: Seed
) -> None:
    a, b = seed_tenant(), seed_tenant()
    both = _new_user(owner_engine, (a.ctx.tenant_id, "member"), (b.ctx.tenant_id, "owner"))

    in_a = TenantContext(user_id=both, tenant_id=a.ctx.tenant_id, role="member")
    in_b = TenantContext(user_id=both, tenant_id=b.ctx.tenant_id, role="owner")
    with tenant_transaction(sessions, in_a) as session:
        assert _visible(session) == {a.workspace_id}
        _expect_insert_denied(session, b.ctx.tenant_id)
    with tenant_transaction(sessions, in_b) as session:
        assert _visible(session) == {b.workspace_id}
        _expect_insert_denied(session, a.ctx.tenant_id)


def test_other_members_of_the_tenant_do_not_grant_access(
    sessions: sessionmaker[Session], owner_engine: Engine, seed_tenant: Seed
) -> None:
    # The tenant has members, but not this user: the membership check is per user.
    a = seed_tenant()
    _new_user(owner_engine, (a.ctx.tenant_id, "member"))
    stranger = _new_user(owner_engine)

    ctx = TenantContext(user_id=stranger, tenant_id=a.ctx.tenant_id, role="member")
    with tenant_transaction(sessions, ctx) as session:
        assert _visible(session) == set()
        _expect_insert_denied(session, a.ctx.tenant_id)


def test_revoked_membership_takes_effect_on_the_reused_pooled_connection(
    sessions: sessionmaker[Session], owner_engine: Engine, seed_tenant: Seed
) -> None:
    a = seed_tenant("member")
    pid = text("SELECT pg_backend_pid()")

    with tenant_transaction(sessions, a.ctx) as session:
        before_pid: int = session.execute(pid).scalar_one()
        assert [w.id for w in list_workspaces(session)] == [a.workspace_id]

    with owner_engine.begin() as connection:
        connection.execute(
            text("DELETE FROM memberships WHERE tenant_id = :t AND user_id = :u"),
            {"t": a.ctx.tenant_id, "u": a.ctx.user_id},
        )

    # The same (now stale) context on the same connection: nothing.
    with tenant_transaction(sessions, a.ctx) as session:
        assert session.execute(pid).scalar_one() == before_pid
        assert list_workspaces(session) == []
        _expect_insert_denied(session, a.ctx.tenant_id)
    assert _owner_count(owner_engine, a.ctx.tenant_id) == 1


def test_membership_revoked_during_a_transaction_blocks_later_statements(
    sessions: sessionmaker[Session], owner_engine: Engine, seed_tenant: Seed
) -> None:
    # READ COMMITTED: each statement sees memberships committed before it began.
    a = seed_tenant("member")

    with tenant_transaction(sessions, a.ctx) as session:
        assert _visible(session) == {a.workspace_id}
        with owner_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM memberships WHERE user_id = :u"), {"u": a.ctx.user_id}
            )
        assert _visible(session) == set()
        _expect_insert_denied(session, a.ctx.tenant_id)


def test_malformed_user_setting_fails_instead_of_matching(
    sessions: sessionmaker[Session], seed_tenant: Seed
) -> None:
    a = seed_tenant()

    with (
        pytest.raises(DBAPIError) as error,
        _raw(sessions, tenant_id=str(a.ctx.tenant_id), user_id="not-a-uuid") as session,
    ):
        session.execute(text("SELECT count(*) FROM workspaces")).scalar_one()

    assert sqlstate(error.value) == "22P02"


def test_membership_policy_composes_with_joins_without_recursion(
    sessions: sessionmaker[Session], seed_tenant: Seed
) -> None:
    # The workspace policy reads memberships, whose own policy never reads
    # workspaces, so there is no policy recursion; joins stay filtered.
    a, b = seed_tenant(), seed_tenant()

    with tenant_transaction(sessions, a.ctx) as session:
        rows = session.execute(
            text(
                "SELECT w.id, m.role FROM workspaces w "
                "JOIN memberships m ON m.tenant_id = w.tenant_id "
                "JOIN tenants t ON t.id = w.tenant_id"
            )
        ).all()
    assert rows == [(a.workspace_id, "owner")]
    assert b.workspace_id not in {row[0] for row in rows}


def test_schema_owner_administers_all_workspaces_without_context(
    owner_engine: Engine, seed_tenant: Seed
) -> None:
    a, b = seed_tenant(), seed_tenant()

    with owner_engine.begin() as connection:
        visible: set[UUID] = set(
            connection.execute(
                text("SELECT id FROM workspaces WHERE id = ANY(:ids)"),
                {"ids": [a.workspace_id, b.workspace_id]},
            ).scalars()
        )
        connection.execute(
            text("INSERT INTO workspaces (tenant_id, name) VALUES (:t, 'admin')"),
            {"t": b.ctx.tenant_id},
        )

    assert visible == {a.workspace_id, b.workspace_id}
    assert _owner_count(owner_engine, b.ctx.tenant_id) == 2


def test_role_is_not_a_database_privilege(
    sessions: sessionmaker[Session], seed_tenant: Seed
) -> None:
    # The context's role is informational for A3: claiming "owner" does not
    # widen what a member can see.
    a, _b = seed_tenant("member"), seed_tenant()
    claimed = TenantContext(user_id=a.ctx.user_id, tenant_id=a.ctx.tenant_id, role="owner")

    with tenant_transaction(sessions, claimed) as session:
        assert _visible(session) == {a.workspace_id}
