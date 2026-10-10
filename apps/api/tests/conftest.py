import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from api.database import (
    DatabaseSettings,
    TenantContext,
    TenantRole,
    create_runtime_engine,
    create_session_factory,
    tenant_transaction,
)
from api.documents import create_document
from api.worker import create_worker_engine
from tests import fake_clamd as fake_clamd_module
from tests.fake_clamd import FakeClamd
from tests.processing_support import CLEAN, PDF, Seeded

# Database tests are mandatory: without a database they fail, never skip. They run
# only against the disposable test database, never the development database.
SETUP_HINT = (
    "locally, run `make api-db-up` and use `make api-check`, which recreates the "
    "disposable test database; in CI, the workflow provisions it"
)
TEST_DATABASE = "app_test"
TEST_DATABASE_MARKER = "disposable-test-database"


def _required_url(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.fail(f"{name} is not set; {SETUP_HINT}", pytrace=False)
    return value


def _assert_disposable(settings: DatabaseSettings, role: str) -> None:
    """Fail unless the URL points at the database marked as disposable test data."""
    engine = create_engine(settings.url, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            name, marker = connection.execute(
                text(
                    "SELECT current_database(), shobj_description(oid, 'pg_database') "
                    "FROM pg_database WHERE datname = current_database()"
                )
            ).one()
    except OperationalError as error:
        pytest.fail(f"cannot connect to the test database as {role} ({error.orig}); {SETUP_HINT}")
    finally:
        engine.dispose()
    if (name, marker) != (TEST_DATABASE, TEST_DATABASE_MARKER):
        pytest.fail(
            f"refusing to run database tests: {role} URL points at database {name!r} "
            f"(marker {marker!r}), not the disposable {TEST_DATABASE!r}",
            pytrace=False,
        )


@pytest.fixture(scope="session")
def test_database() -> tuple[DatabaseSettings, DatabaseSettings]:
    """(owner, runtime) settings for the positively identified disposable test database."""
    owner = DatabaseSettings.from_env({"DATABASE_URL": _required_url("TEST_DATABASE_URL_OWNER")})
    runtime = DatabaseSettings.from_env(
        {"DATABASE_URL": _required_url("TEST_DATABASE_URL_RUNTIME")}
    )
    _assert_disposable(owner, "owner")
    _assert_disposable(runtime, "runtime")
    return owner, runtime


@pytest.fixture(scope="session")
def admin_engine() -> Iterator[Engine]:
    """Superuser on the disposable test database, only for creating temporary roles."""
    admin = DatabaseSettings.from_env({"DATABASE_URL": _required_url("TEST_DATABASE_URL_ADMIN")})
    _assert_disposable(admin, "admin")
    engine = create_engine(admin.url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def operator_settings() -> DatabaseSettings:
    operator = DatabaseSettings.from_env(
        {"DATABASE_URL": _required_url("TEST_DATABASE_URL_OPERATOR")}
    )
    _assert_disposable(operator, "operator")
    return operator


@pytest.fixture(scope="session")
def worker_settings() -> DatabaseSettings:
    worker = DatabaseSettings.from_env({"DATABASE_URL": _required_url("TEST_DATABASE_URL_WORKER")})
    _assert_disposable(worker, "worker")
    return worker


@pytest.fixture(scope="session")
def runtime_settings(test_database: tuple[DatabaseSettings, DatabaseSettings]) -> DatabaseSettings:
    return test_database[1]


@pytest.fixture(scope="session")
def owner_settings(test_database: tuple[DatabaseSettings, DatabaseSettings]) -> DatabaseSettings:
    return test_database[0]


@pytest.fixture
def runtime_engine(runtime_settings: DatabaseSettings) -> Iterator[Engine]:
    """A single-connection pool, so consecutive transactions reuse one connection."""
    engine = create_runtime_engine(runtime_settings, pool_size=1, max_overflow=0)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except OperationalError as error:
        pytest.fail(f"cannot connect to the test database ({error.orig}); {SETUP_HINT}")
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def owner_engine(owner_settings: DatabaseSettings) -> Iterator[Engine]:
    """Schema owner, for test setup only. Application code never uses this role."""
    engine = create_engine(owner_settings.url, poolclass=NullPool)
    yield engine
    engine.dispose()


@dataclass(frozen=True)
class SeededTenant:
    ctx: TenantContext
    workspace_id: UUID


def insert_user(connection: Connection) -> UUID:
    """Insert a user with a unique identity, as the schema owner."""
    user_id: UUID = connection.execute(
        text("INSERT INTO users (issuer, subject) VALUES (:i, :s) RETURNING id"),
        {"i": "https://issuer.test/", "s": f"seed|{uuid4()}"},
    ).scalar_one()
    return user_id


def insert_membership(connection: Connection, tenant_id: UUID, user_id: UUID, role: str) -> None:
    connection.execute(
        text("INSERT INTO memberships (tenant_id, user_id, role) VALUES (:t, :u, :r)"),
        {"t": tenant_id, "u": user_id, "r": role},
    )


@pytest.fixture
def seed_tenant(owner_engine: Engine) -> Callable[..., SeededTenant]:
    """Create a tenant, a member user and one workspace, committed, as the schema owner.

    The returned context is what authorization would build for that member.
    """

    def seed(role: TenantRole = "owner") -> SeededTenant:
        with owner_engine.begin() as connection:
            tenant_id: UUID = connection.execute(
                text("INSERT INTO tenants (name) VALUES ('Tenant') RETURNING id")
            ).scalar_one()
            user_id = insert_user(connection)
            insert_membership(connection, tenant_id, user_id, role)
            workspace_id: UUID = connection.execute(
                text(
                    "INSERT INTO workspaces (tenant_id, name) VALUES (:t, 'Workspace') RETURNING id"
                ),
                {"t": tenant_id},
            ).scalar_one()
        ctx = TenantContext(user_id=user_id, tenant_id=tenant_id, role=role)
        return SeededTenant(ctx=ctx, workspace_id=workspace_id)

    return seed


def sqlstate(error: DBAPIError) -> str | None:
    return getattr(error.orig, "sqlstate", None)


@pytest.fixture(autouse=True)
def fake_clamd() -> Iterator[FakeClamd]:
    """The session's fake clamd (the endpoint tests' CLAMD_ADDRESS), reset to "clean"."""
    fake = fake_clamd_module.shared()
    fake.reset()
    yield fake


# ---- DI-1 processing (tests/processing_support.py has the helpers)


@pytest.fixture
def sessions(runtime_engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(runtime_engine)


@pytest.fixture
def worker_engine(worker_settings: DatabaseSettings) -> Iterator[Engine]:
    engine = create_worker_engine(worker_settings)
    yield engine
    engine.dispose()


@pytest.fixture
def worker_sessions(worker_engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(worker_engine)


@pytest.fixture
def seed_document(
    seed_tenant: Callable[..., SeededTenant], sessions: sessionmaker[Session]
) -> Callable[..., Seeded]:
    def seed(content: bytes = PDF, tenant: SeededTenant | None = None) -> Seeded:
        tenant = tenant or seed_tenant()
        with tenant_transaction(sessions, tenant.ctx) as session:
            record = create_document(session, tenant.workspace_id, "contract.pdf", content, CLEAN)
        return Seeded(tenant, record)

    return seed
