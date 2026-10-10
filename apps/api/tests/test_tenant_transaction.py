import ast
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Session

from api.database import (
    DatabaseRoleError,
    DatabaseSettings,
    TenantContext,
    TenantRole,
    create_runtime_engine,
    create_session_factory,
    tenant_transaction,
)

CURRENT = text(
    "SELECT pg_backend_pid(), current_setting('app.tenant_id', true), "
    "current_setting('app.user_id', true)"
)


def _pid_and_tenant(session: Session) -> tuple[int, str | None]:
    pid, tenant, user = session.execute(CURRENT).one()
    # The user and tenant settings are always set and cleared together.
    assert (user in (None, "")) == (tenant in (None, ""))
    return pid, tenant


def _no_tenant(value: str | None) -> bool:
    # Once set locally, the setting reads back as '' after the transaction ends.
    return value in (None, "")


def _ctx(tenant_id: UUID | None = None) -> TenantContext:
    return TenantContext(user_id=uuid4(), tenant_id=tenant_id or uuid4(), role="member")


@pytest.mark.parametrize("field", ["user_id", "tenant_id"])
def test_tenant_context_requires_non_nil_uuids(field: str) -> None:
    valid = {"user_id": uuid4(), "tenant_id": uuid4(), "role": "member"}
    for bad in ("7d3b3c1e-0000-4000-8000-000000000001", None, 1):
        with pytest.raises(TypeError, match=field):
            TenantContext(**{**valid, field: bad})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="nil UUID"):
        TenantContext(**{**valid, field: UUID(int=0)})  # type: ignore[arg-type]


@pytest.mark.parametrize("role", ["admin", "Owner", "", None, "owner "])
def test_tenant_context_requires_a_known_role(role: object) -> None:
    with pytest.raises(ValueError, match="role"):
        TenantContext(user_id=uuid4(), tenant_id=uuid4(), role=role)  # type: ignore[arg-type]


@pytest.mark.parametrize("role", ["owner", "member"])
def test_tenant_context_accepts_both_roles(role: TenantRole) -> None:
    assert TenantContext(user_id=uuid4(), tenant_id=uuid4(), role=role).role == role


def test_tenant_transaction_requires_a_tenant_context(runtime_engine: Engine) -> None:
    sessions = create_session_factory(runtime_engine)
    with (
        pytest.raises(TypeError, match="requires a TenantContext"),
        tenant_transaction(sessions, uuid4()),  # type: ignore[arg-type]
    ):
        pass


def test_user_and_tenant_are_set_inside_the_transaction(runtime_engine: Engine) -> None:
    ctx = _ctx()

    with tenant_transaction(create_session_factory(runtime_engine), ctx) as session:
        _, tenant = _pid_and_tenant(session)
        user = session.execute(text("SELECT current_setting('app.user_id', true)")).scalar()

    assert (user, tenant) == (str(ctx.user_id), str(ctx.tenant_id))


def test_context_is_not_retained_by_the_next_transaction_on_the_same_connection(
    runtime_engine: Engine,
) -> None:
    sessions = create_session_factory(runtime_engine)
    with tenant_transaction(sessions, _ctx()) as session:
        first_pid, _ = _pid_and_tenant(session)

    with sessions() as session, session.begin():
        next_pid, tenant = _pid_and_tenant(session)

    assert next_pid == first_pid, "test requires the pooled connection to be reused"
    assert _no_tenant(tenant)


def test_context_is_cleared_and_error_propagates_when_the_transaction_fails(
    runtime_engine: Engine,
) -> None:
    sessions = create_session_factory(runtime_engine)
    with (
        pytest.raises(RuntimeError, match="boom"),
        tenant_transaction(sessions, _ctx()) as session,
    ):
        first_pid, _ = _pid_and_tenant(session)
        raise RuntimeError("boom")

    with sessions() as session, session.begin():
        next_pid, tenant = _pid_and_tenant(session)

    assert next_pid == first_pid
    assert _no_tenant(tenant)
    assert runtime_engine.pool.checkedout() == 0  # type: ignore[attr-defined]


def test_consecutive_tenants_on_one_connection_see_only_their_own_context(
    runtime_engine: Engine,
) -> None:
    sessions = create_session_factory(runtime_engine)
    tenant_a, tenant_b = _ctx(), _ctx()

    with tenant_transaction(sessions, tenant_a) as session:
        pid_a, seen_a = _pid_and_tenant(session)
    with tenant_transaction(sessions, tenant_b) as session:
        pid_b, seen_b = _pid_and_tenant(session)

    assert pid_a == pid_b
    assert (seen_a, seen_b) == (str(tenant_a.tenant_id), str(tenant_b.tenant_id))


def test_work_cannot_continue_after_committing_inside_the_tenant_block(
    runtime_engine: Engine,
) -> None:
    sessions = create_session_factory(runtime_engine)
    with (
        pytest.raises(InvalidRequestError, match="closed transaction"),
        tenant_transaction(sessions, _ctx()) as session,
    ):
        session.commit()
        session.execute(text("SELECT 1"))


def test_runtime_engine_refuses_a_schema_owner_role(owner_settings: DatabaseSettings) -> None:
    engine = create_runtime_engine(owner_settings)
    try:
        with pytest.raises(DatabaseRoleError, match="must not"):
            engine.connect()
    finally:
        engine.dispose()


def test_runtime_engine_accepts_the_runtime_role(runtime_engine: Engine) -> None:
    with runtime_engine.connect() as connection:
        assert connection.execute(text("SELECT current_user")).scalar_one() == "app_runtime"


def test_tenant_context_is_constructed_only_by_authorization() -> None:
    # Only api.authorization builds a TenantContext, after checking the
    # membership; this prevents deriving one from request data elsewhere.
    source_root = Path(__file__).parents[1] / "src" / "api"
    constructions = [
        path.name
        for path in source_root.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "TenantContext")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "TenantContext")
        )
    ]

    assert constructions == ["authorization.py"]
