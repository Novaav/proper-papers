"""Workspace repository against real PostgreSQL 18, as app_runtime on app_test."""

import inspect
from collections.abc import Callable, Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from api import workspaces
from api.database import TenantContext, create_session_factory, tenant_transaction
from api.workspaces import (
    InvalidWorkspaceName,
    TenantContextMissing,
    WorkspaceRecord,
    create_workspace,
    get_workspace,
    list_workspaces,
)
from tests.conftest import SeededTenant, insert_membership, insert_user, sqlstate

Seed = Callable[..., SeededTenant]


@pytest.fixture
def sessions(runtime_engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(runtime_engine)


@pytest.fixture
def new_tenant(owner_engine: Engine) -> Callable[[], TenantContext]:
    """A tenant without workspaces and a member's context, created by the schema owner."""

    def create() -> TenantContext:
        with owner_engine.begin() as connection:
            tenant_id: UUID = connection.execute(
                text("INSERT INTO tenants (name) VALUES ('Tenant') RETURNING id")
            ).scalar_one()
            user_id = insert_user(connection)
            insert_membership(connection, tenant_id, user_id, "member")
        return TenantContext(user_id=user_id, tenant_id=tenant_id, role="member")

    return create


@pytest.fixture
def statements(runtime_engine: Engine) -> Iterator[list[str]]:
    """SQL statements sent through the runtime engine while the test runs."""
    sent: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        sent.append(statement)

    event.listen(runtime_engine, "before_cursor_execute", record)
    yield sent
    event.remove(runtime_engine, "before_cursor_execute", record)


def test_create_list_and_get_within_one_tenant(
    sessions: sessionmaker[Session], new_tenant: Callable[[], TenantContext]
) -> None:
    tenant = new_tenant()

    with tenant_transaction(sessions, tenant) as session:
        assert list_workspaces(session) == []
        first = create_workspace(session, "Contracts")
        second = create_workspace(session, "  Policies  ")

    assert (first.tenant_id, second.tenant_id) == (tenant.tenant_id, tenant.tenant_id)
    assert second.name == "  Policies  "  # stored as given; validation only trims spaces
    with tenant_transaction(sessions, tenant) as session:
        assert list_workspaces(session) == [first, second]
        assert get_workspace(session, first.id) == first
        assert isinstance(get_workspace(session, second.id), WorkspaceRecord)


def test_list_order_is_deterministic(
    sessions: sessionmaker[Session], new_tenant: Callable[[], TenantContext]
) -> None:
    tenant = new_tenant()
    with tenant_transaction(sessions, tenant) as session:
        created = [create_workspace(session, f"w{i}") for i in range(5)]
    with tenant_transaction(sessions, tenant) as session:
        later = create_workspace(session, "later")

    with tenant_transaction(sessions, tenant) as session:
        listed = list_workspaces(session)

    # Same transaction means the same created_at; ties are broken by id.
    assert listed == [*sorted(created, key=lambda w: w.id), later]
    with tenant_transaction(sessions, tenant) as session:
        assert list_workspaces(session) == listed


def test_other_tenants_workspaces_are_never_listed_or_returned(
    sessions: sessionmaker[Session], seed_tenant: Seed
) -> None:
    a, b = seed_tenant(), seed_tenant()
    with tenant_transaction(sessions, a.ctx) as session:
        a_created = create_workspace(session, "A only")

    with tenant_transaction(sessions, b.ctx) as session:
        listed_ids = {w.id for w in list_workspaces(session)}
        assert listed_ids == {b.workspace_id}
        assert get_workspace(session, a.workspace_id) is None
        assert get_workspace(session, a_created.id) is None


def test_cross_tenant_and_missing_ids_are_indistinguishable(
    sessions: sessionmaker[Session], seed_tenant: Seed, statements: list[str]
) -> None:
    a, b = seed_tenant(), seed_tenant()

    with tenant_transaction(sessions, a.ctx) as session:
        statements.clear()
        other_tenants = get_workspace(session, b.workspace_id)
        other_statements = list(statements)
        statements.clear()
        nonexistent = get_workspace(session, uuid4())
        nonexistent_statements = list(statements)

    assert other_tenants is None
    assert nonexistent is None
    assert other_statements == nonexistent_statements


def test_behaviour_is_identical_across_pooled_connection_reuse(
    sessions: sessionmaker[Session], seed_tenant: Seed
) -> None:
    a, b = seed_tenant(), seed_tenant()
    pids: set[int] = set()

    for _ in range(5):
        for own, other in ((a, b), (b, a)):
            with tenant_transaction(sessions, own.ctx) as session:
                pids.add(session.execute(text("SELECT pg_backend_pid()")).scalar_one())
                assert [w.id for w in list_workspaces(session)] == [own.workspace_id]
                assert get_workspace(session, own.workspace_id) is not None
                assert get_workspace(session, other.workspace_id) is None

    assert len(pids) == 1, "the single pooled connection must be reused throughout"


def test_repository_fails_closed_without_tenant_context(
    sessions: sessionmaker[Session], seed_tenant: Seed, owner_engine: Engine
) -> None:
    a = seed_tenant()

    with sessions() as session, session.begin():
        with pytest.raises(TenantContextMissing):
            create_workspace(session, "orphan")
        with pytest.raises(TenantContextMissing):
            list_workspaces(session)
        with pytest.raises(TenantContextMissing):
            get_workspace(session, a.workspace_id)

    with owner_engine.connect() as connection:
        connection.execute(text("SELECT set_config('app.tenant_id', :t, false)"), {"t": ""})
        orphans: int = connection.execute(
            text("SELECT count(*) FROM workspaces WHERE name = 'orphan'")
        ).scalar_one()
    assert orphans == 0


@pytest.mark.parametrize("missing", ["user_id", "tenant_id"])
def test_repository_fails_closed_when_user_or_tenant_is_missing(
    sessions: sessionmaker[Session], seed_tenant: Seed, missing: str
) -> None:
    a = seed_tenant()
    present = {"user_id": str(a.ctx.user_id), "tenant_id": str(a.ctx.tenant_id)}
    present.pop(missing)

    with sessions() as session, session.begin():
        for name, value in present.items():
            session.execute(
                text("SELECT set_config(:n, :v, true)"), {"n": f"app.{name}", "v": value}
            )
        with pytest.raises(TenantContextMissing):
            list_workspaces(session)
        with pytest.raises(TenantContextMissing):
            get_workspace(session, a.workspace_id)
        with pytest.raises(TenantContextMissing):
            create_workspace(session, "orphan")


def test_malformed_tenant_setting_fails_instead_of_matching(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session, session.begin():
        session.execute(text("SELECT set_config('app.tenant_id', 'not-a-uuid', true)"))
        with pytest.raises(DBAPIError) as error:
            list_workspaces(session)

    assert sqlstate(error.value) == "22P02"


@pytest.mark.parametrize("name", ["", " ", "     ", "x" * 201, " " + "x" * 201 + " ", "a\x00b"])
def test_invalid_names_are_rejected_before_reaching_the_database(
    sessions: sessionmaker[Session],
    new_tenant: Callable[[], TenantContext],
    statements: list[str],
    name: str,
) -> None:
    with tenant_transaction(sessions, new_tenant()) as session:
        statements.clear()
        with pytest.raises(InvalidWorkspaceName):
            create_workspace(session, name)
        assert statements == []
        assert list_workspaces(session) == []


def test_non_string_names_and_ids_are_rejected(
    sessions: sessionmaker[Session], new_tenant: Callable[[], TenantContext]
) -> None:
    with tenant_transaction(sessions, new_tenant()) as session:
        with pytest.raises(TypeError):
            create_workspace(session, 123)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            get_workspace(session, str(uuid4()))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "name",
    [
        "x",
        "x" * 200,
        "  padded  ",
        "\t",
        chr(0xA0),
        "\U0001f600" * 200,
        "\U0001f600" * 201,
        ("e" + chr(0x301)) * 100,
    ],
)
def test_name_validation_matches_the_database_constraint(
    sessions: sessionmaker[Session],
    new_tenant: Callable[[], TenantContext],
    name: str,
) -> None:
    with tenant_transaction(sessions, new_tenant()) as session:
        database_accepts: bool = session.execute(
            text("SELECT length(btrim(:n)) BETWEEN 1 AND 200"), {"n": name}
        ).scalar_one()
        try:
            create_workspace(session, name)
            repository_accepts = True
        except InvalidWorkspaceName:
            repository_accepts = False

    assert repository_accepts == database_accepts


def test_rollback_removes_a_created_workspace(
    sessions: sessionmaker[Session], new_tenant: Callable[[], TenantContext]
) -> None:
    tenant = new_tenant()

    with pytest.raises(RuntimeError), tenant_transaction(sessions, tenant) as session:
        created = create_workspace(session, "rolled back")
        assert get_workspace(session, created.id) == created
        raise RuntimeError("abort")

    with tenant_transaction(sessions, tenant) as session:
        assert get_workspace(session, created.id) is None
        assert list_workspaces(session) == []


def test_repository_neither_commits_nor_opens_transactions(
    sessions: sessionmaker[Session], seed_tenant: Seed
) -> None:
    a = seed_tenant()
    lifecycle: list[str] = []

    with tenant_transaction(sessions, a.ctx) as session:
        transaction = session.get_transaction()
        connection = session.connection()
        for name in ("after_begin", "after_commit", "after_rollback", "after_soft_rollback"):
            event.listen(session, name, lambda *_args, n=name: lifecycle.append(n))

        created = create_workspace(session, "no commit")
        list_workspaces(session)
        get_workspace(session, created.id)

        assert lifecycle == []
        assert session.get_transaction() is transaction
        assert session.connection() is connection
        assert connection.in_transaction()
        assert not session.in_nested_transaction()


def test_repository_functions_take_no_tenant_parameter() -> None:
    for function in (
        workspaces.create_workspace,
        workspaces.list_workspaces,
        workspaces.get_workspace,
    ):
        parameters = inspect.signature(function).parameters
        assert not any("tenant" in parameter for parameter in parameters), function.__name__


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE workspaces SET tenant_id = :other WHERE id = :w",
        "SET LOCAL row_security = off; SELECT count(*) FROM workspaces",
    ],
)
def test_runtime_cannot_retarget_or_unfilter_created_workspaces(
    sessions: sessionmaker[Session], seed_tenant: Seed, sql: str
) -> None:
    a, b = seed_tenant(), seed_tenant()

    with tenant_transaction(sessions, a.ctx) as session:
        created = create_workspace(session, "pinned")
        with pytest.raises(DBAPIError) as error, session.begin_nested():
            for statement in sql.split("; "):
                session.execute(text(statement), {"other": b.ctx.tenant_id, "w": created.id})

    assert sqlstate(error.value) == "42501"
