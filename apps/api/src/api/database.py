"""Database configuration, runtime engine and tenant-scoped transactions.

Importing this module reads no configuration and opens no connections.
"""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import Session, sessionmaker

# psycopg 3 is the only installed driver; a bare "postgresql" URL means psycopg.
_SUPPORTED_DRIVERS = {"postgresql", "postgresql+psycopg"}


class DatabaseConfigError(Exception):
    """DATABASE_URL is missing or unusable. Messages never contain the URL."""


class DatabaseRoleError(Exception):
    """The runtime connection uses a role that could bypass row-level security."""


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    url: URL

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> DatabaseSettings:
        raw = env.get("DATABASE_URL", "").strip()
        if not raw:
            raise DatabaseConfigError("DATABASE_URL is not set")
        try:
            url = make_url(raw)
        except ArgumentError:
            raise DatabaseConfigError("DATABASE_URL is not a valid database URL") from None
        if url.drivername not in _SUPPORTED_DRIVERS:
            raise DatabaseConfigError(
                f"DATABASE_URL driver {url.drivername!r} is not supported; "
                "use postgresql:// or postgresql+psycopg://"
            )
        missing = [part for part in ("host", "database", "username") if not getattr(url, part)]
        if missing:
            raise DatabaseConfigError(f"DATABASE_URL is missing: {', '.join(missing)}")
        return cls(url=url.set(drivername="postgresql+psycopg"))

    def __repr__(self) -> str:
        return f"DatabaseSettings(url={self.url.render_as_string(hide_password=True)!r})"


# True when the connected role, or any role it is a direct or indirect member
# of, is a superuser, can bypass RLS, or owns a database or schema (as the
# migration role does). Membership matters: members inherit policies granted
# to the role and can SET ROLE to it.
_PRIVILEGED_ROLE_SQL = """
SELECT EXISTS (
    SELECT 1
    FROM pg_roles AS r
    WHERE pg_has_role(current_user, r.oid, 'MEMBER')
      AND (r.rolsuper
           OR r.rolbypassrls
           OR EXISTS (SELECT 1 FROM pg_database WHERE datdba = r.oid)
           OR EXISTS (SELECT 1 FROM pg_namespace WHERE nspowner = r.oid))
)
"""


def create_runtime_engine(settings: DatabaseSettings, **engine_options: Any) -> Engine:
    """Create the API's engine. Connections open lazily, on first use.

    Every new connection is checked so that the API never runs as a role that
    row-level security would not apply to.
    """
    engine = create_engine(settings.url, pool_pre_ping=True, **engine_options)

    @event.listens_for(engine, "connect")
    def _reject_privileged_role(dbapi_connection: Any, _: Any) -> None:
        with dbapi_connection.cursor() as cursor:
            cursor.execute(_PRIVILEGED_ROLE_SQL)
            (privileged,) = cursor.fetchone()
        dbapi_connection.rollback()
        if privileged:
            raise DatabaseRoleError(
                "runtime database role must not be a superuser, have BYPASSRLS, "
                "or own the database or a schema"
            )

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine)


TenantRole = Literal["owner", "reviewer", "member"]


@dataclass(frozen=True, slots=True)
class TenantContext:
    """A verified user acting in a tenant they are a member of.

    Only api.authorization creates one, after checking the membership; never
    build it from request headers, paths or bodies. PostgreSQL re-checks the
    membership on every workspace statement (row-level security).
    """

    user_id: UUID
    tenant_id: UUID
    role: TenantRole

    def __post_init__(self) -> None:
        for name in ("user_id", "tenant_id"):
            value = getattr(self, name)
            if not isinstance(value, UUID):
                raise TypeError(f"{name} must be a uuid.UUID")
            if value.int == 0:
                raise ValueError(f"{name} must not be the nil UUID")
        if self.role not in ("owner", "reviewer", "member"):
            raise ValueError("role must be 'owner', 'reviewer' or 'member'")

    @property
    def can_review(self) -> bool:
        """Owners and reviewers may record review decisions and approve (R1)."""
        return self.role in ("owner", "reviewer")


@contextmanager
def tenant_transaction(sessions: sessionmaker[Session], ctx: TenantContext) -> Iterator[Session]:
    """Run one transaction as `ctx`'s user in `ctx`'s tenant.

    `app.user_id` and `app.tenant_id` are set with set_config(..., is_local =>
    true), so they end with the transaction. The transaction commits when the
    block exits normally and rolls back on any exception; the session is then
    closed and its connection returned to the pool.
    """
    if not isinstance(ctx, TenantContext):
        raise TypeError("tenant_transaction requires a TenantContext")
    with sessions() as session, session.begin():
        session.execute(
            text(
                "SELECT set_config('app.user_id', :user_id, true), "
                "set_config('app.tenant_id', :tenant_id, true)"
            ),
            {"user_id": str(ctx.user_id), "tenant_id": str(ctx.tenant_id)},
        )
        yield session
