"""Users and memberships under row-level security, as app_runtime on app_test.

The API will set the transaction-local settings app.auth_issuer / app.auth_subject
(to provision and look up a user) and app.user_id (to read memberships). These
tests set them directly, exactly as that code will.
"""

import secrets
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from uuid import UUID

import psycopg
import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from api.database import DatabaseSettings, create_runtime_engine
from tests.conftest import sqlstate

ISSUER = "https://idp.test/"
INSUFFICIENT_PRIVILEGE, CHECK, FOREIGN_KEY, UNIQUE, RESTRICT = (
    "42501",
    "23514",
    "23503",
    "23505",
    "23001",
)

PROVISION = text(
    "INSERT INTO users (issuer, subject) VALUES (:issuer, :subject) "
    "ON CONFLICT (issuer, subject) DO NOTHING RETURNING id"
)
LOOKUP = text("SELECT id FROM users WHERE issuer = :issuer AND subject = :subject")
# Measured on PostgreSQL 18: INSERT ... ON CONFLICT (with or without RETURNING)
# also checks the proposed row against the SELECT policies, so those forms are
# guarded by two policies. A plain INSERT is guarded only by the INSERT policy's
# WITH CHECK, so rejection tests cover all three forms to prove each policy.
PROVISION_FORMS = pytest.mark.parametrize(
    "statement",
    [
        PROVISION.text,
        "INSERT INTO users (issuer, subject) VALUES (:issuer, :subject) "
        "ON CONFLICT (issuer, subject) DO NOTHING",
        "INSERT INTO users (issuer, subject) VALUES (:issuer, :subject)",
    ],
    ids=["on-conflict-returning", "on-conflict", "plain"],
)


@dataclass(frozen=True)
class Identity:
    issuer: str
    subject: str

    @property
    def params(self) -> dict[str, str]:
        return {"issuer": self.issuer, "subject": self.subject}


@pytest.fixture
def new_identity() -> Callable[[], Identity]:
    return lambda: Identity(ISSUER, f"subject-{secrets.token_hex(8)}")


def _set_identity(connection: Connection, identity: Identity) -> None:
    connection.execute(
        text(
            "SELECT set_config('app.auth_issuer', :issuer, true), "
            "set_config('app.auth_subject', :subject, true)"
        ),
        identity.params,
    )


def _set_user(connection: Connection, user_id: UUID) -> None:
    connection.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(user_id)})


def _expect(connection: Connection, code: str, sql: str, **params: object) -> str:
    with pytest.raises(DBAPIError) as error, connection.begin_nested():
        connection.execute(text(sql), params)
    assert sqlstate(error.value) == code
    assert isinstance(error.value.orig, psycopg.Error)
    return str(error.value.orig.diag.message_primary)


def _seed_user(owner_engine: Engine, identity: Identity) -> UUID:
    with owner_engine.begin() as connection:
        user_id: UUID = connection.execute(
            text("INSERT INTO users (issuer, subject) VALUES (:issuer, :subject) RETURNING id"),
            identity.params,
        ).scalar_one()
    return user_id


def _seed_tenant(owner_engine: Engine) -> UUID:
    with owner_engine.begin() as connection:
        tenant_id: UUID = connection.execute(
            text("INSERT INTO tenants (name) VALUES ('Tenant') RETURNING id")
        ).scalar_one()
    return tenant_id


def _seed_membership(owner_engine: Engine, tenant_id: UUID, user_id: UUID, role: str) -> None:
    with owner_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO memberships (tenant_id, user_id, role) VALUES (:t, :u, :r)"),
            {"t": tenant_id, "u": user_id, "r": role},
        )


def _count_identity(owner_engine: Engine, identity: Identity) -> int:
    with owner_engine.connect() as connection:
        count: int = connection.execute(
            text("SELECT count(*) FROM users WHERE issuer = :issuer AND subject = :subject"),
            identity.params,
        ).scalar_one()
    return count


# --- Provisioning with INSERT ... ON CONFLICT under row-level security ---------------


def test_provisioning_creates_one_user_and_is_idempotent(
    runtime_engine: Engine, owner_engine: Engine, new_identity: Callable[[], Identity]
) -> None:
    identity = new_identity()

    with runtime_engine.begin() as connection:
        _set_identity(connection, identity)
        created = connection.execute(PROVISION, identity.params).scalar_one_or_none()
    with runtime_engine.begin() as connection:
        _set_identity(connection, identity)
        repeated = connection.execute(PROVISION, identity.params).scalar_one_or_none()
        found: UUID = connection.execute(LOOKUP, identity.params).scalar_one()

    assert isinstance(created, UUID) and created.version == 7
    assert repeated is None  # DO NOTHING on conflict: no row returned
    assert found == created
    assert _count_identity(owner_engine, identity) == 1


@PROVISION_FORMS
def test_provisioning_another_identity_fails_the_same_way_whether_or_not_it_exists(
    runtime_engine: Engine,
    owner_engine: Engine,
    new_identity: Callable[[], Identity],
    statement: str,
) -> None:
    mine, existing, missing = new_identity(), new_identity(), new_identity()
    _seed_user(owner_engine, existing)

    with runtime_engine.begin() as connection:
        _set_identity(connection, mine)
        existing_error = _expect(
            connection,
            INSUFFICIENT_PRIVILEGE,
            statement,
            **existing.params,
        )
        missing_error = _expect(
            connection,
            INSUFFICIENT_PRIVILEGE,
            statement,
            **missing.params,
        )

    # WITH CHECK is applied to the proposed row before conflict handling, so
    # DO NOTHING never silently skips another identity and reveals nothing.
    assert (
        existing_error
        == missing_error
        == ('new row violates row-level security policy for table "users"')
    )
    assert _count_identity(owner_engine, missing) == 0


@PROVISION_FORMS
@pytest.mark.parametrize("context", ["none", "empty", "issuer_only"])
def test_provisioning_without_a_complete_identity_context_is_rejected(
    runtime_engine: Engine, new_identity: Callable[[], Identity], context: str, statement: str
) -> None:
    identity = new_identity()

    with runtime_engine.begin() as connection:
        if context == "empty":
            _set_identity(connection, Identity("", ""))
        elif context == "issuer_only":
            connection.execute(
                text("SELECT set_config('app.auth_issuer', :i, true)"), {"i": identity.issuer}
            )
        _expect(
            connection,
            INSUFFICIENT_PRIVILEGE,
            statement,
            **identity.params,
        )
        assert connection.execute(text("SELECT count(*) FROM users")).scalar_one() == 0


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO users (issuer, subject) VALUES (:issuer, :subject) "
        "ON CONFLICT (issuer, subject) DO UPDATE SET subject = excluded.subject",
        "INSERT INTO users (id, issuer, subject) VALUES (uuidv7(), :issuer, :subject)",
        "UPDATE users SET subject = 'renamed' WHERE issuer = :issuer AND subject = :subject",
        "DELETE FROM users WHERE issuer = :issuer AND subject = :subject",
        "TRUNCATE users",
    ],
)
def test_runtime_cannot_update_delete_or_choose_user_ids(
    runtime_engine: Engine, new_identity: Callable[[], Identity], sql: str
) -> None:
    identity = new_identity()

    with runtime_engine.begin() as connection:
        _set_identity(connection, identity)
        connection.execute(PROVISION, identity.params)
        _expect(connection, INSUFFICIENT_PRIVILEGE, sql, **identity.params)


def test_user_rows_are_visible_only_to_their_own_identity_or_id(
    runtime_engine: Engine, owner_engine: Engine, new_identity: Callable[[], Identity]
) -> None:
    a, b = new_identity(), new_identity()
    a_id, b_id = _seed_user(owner_engine, a), _seed_user(owner_engine, b)

    with runtime_engine.begin() as connection:
        assert connection.execute(text("SELECT count(*) FROM users")).scalar_one() == 0
    with runtime_engine.begin() as connection:
        _set_identity(connection, a)
        visible: set[UUID] = set(connection.execute(text("SELECT id FROM users")).scalars())
        assert visible == {a_id}
        assert connection.execute(LOOKUP, b.params).all() == []
    with runtime_engine.begin() as connection:
        _set_user(connection, a_id)
        assert set(connection.execute(text("SELECT id FROM users")).scalars()) == {a_id}
        assert (
            connection.execute(
                text("SELECT count(*) FROM users WHERE id = :b"), {"b": b_id}
            ).scalar_one()
            == 0
        )


def test_identity_context_does_not_survive_commit_or_rollback(
    runtime_engine: Engine, owner_engine: Engine, new_identity: Callable[[], Identity]
) -> None:
    identity = new_identity()
    user_id = _seed_user(owner_engine, identity)
    pid = text("SELECT pg_backend_pid()")

    with runtime_engine.begin() as connection:
        _set_identity(connection, identity)
        _set_user(connection, user_id)
        first_pid: int = connection.execute(pid).scalar_one()
        assert connection.execute(text("SELECT count(*) FROM users")).scalar_one() == 1
    with runtime_engine.begin() as connection:
        assert connection.execute(pid).scalar_one() == first_pid
        assert connection.execute(text("SELECT count(*) FROM users")).scalar_one() == 0

    with pytest.raises(RuntimeError), runtime_engine.begin() as connection:
        _set_identity(connection, identity)
        raise RuntimeError("abort")
    with runtime_engine.begin() as connection:
        assert connection.execute(pid).scalar_one() == first_pid
        assert connection.execute(text("SELECT count(*) FROM users")).scalar_one() == 0


# --- Concurrency -----------------------------------------------------------------------


@pytest.fixture
def two_runtime_connections(runtime_settings: DatabaseSettings) -> Iterator[Engine]:
    engine = create_runtime_engine(runtime_settings, pool_size=2, max_overflow=0)
    yield engine
    engine.dispose()


def _wait_until_blocked(admin_engine: Engine, backend_pid: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with admin_engine.connect() as admin:
            waiting = admin.execute(
                text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :p"),
                {"p": backend_pid},
            ).scalar_one_or_none()
        if waiting == "Lock":
            return
        time.sleep(0.02)
    pytest.fail("second provisioning transaction never waited on the first")


@pytest.mark.parametrize("first_outcome", ["commit", "rollback"])
def test_concurrent_provisioning_creates_exactly_one_user(
    two_runtime_connections: Engine,
    admin_engine: Engine,
    owner_engine: Engine,
    new_identity: Callable[[], Identity],
    first_outcome: str,
) -> None:
    identity = new_identity()
    result: dict[str, object] = {}

    first = two_runtime_connections.connect()
    first_tx = first.begin()
    _set_identity(first, identity)
    first_id: UUID = first.execute(PROVISION, identity.params).scalar_one()

    second = two_runtime_connections.connect()
    second_pid: int = second.execute(text("SELECT pg_backend_pid()")).scalar_one()
    second.rollback()

    def provision_second() -> None:
        with second.begin():
            _set_identity(second, identity)
            result["returned"] = second.execute(PROVISION, identity.params).scalar_one_or_none()
            result["found"] = second.execute(LOOKUP, identity.params).scalar_one()

    worker = threading.Thread(target=provision_second)
    worker.start()
    try:
        _wait_until_blocked(admin_engine, second_pid)  # blocked on the uncommitted row
        assert worker.is_alive()
        if first_outcome == "commit":
            first_tx.commit()
        else:
            first_tx.rollback()
        worker.join(timeout=10)
        assert not worker.is_alive()
    finally:
        first.close()
        worker.join(timeout=10)
        second.close()

    assert _count_identity(owner_engine, identity) == 1
    if first_outcome == "commit":
        assert result == {"returned": None, "found": first_id}
    else:
        assert isinstance(result["returned"], UUID)
        assert result["returned"] != first_id
        assert result["found"] == result["returned"]


# --- Memberships -----------------------------------------------------------------------


def test_memberships_are_visible_only_for_the_current_user(
    runtime_engine: Engine, owner_engine: Engine, new_identity: Callable[[], Identity]
) -> None:
    a_identity = new_identity()
    a, b = _seed_user(owner_engine, a_identity), _seed_user(owner_engine, new_identity())
    t1, t2 = _seed_tenant(owner_engine), _seed_tenant(owner_engine)
    _seed_membership(owner_engine, t1, a, "owner")
    _seed_membership(owner_engine, t2, a, "member")
    _seed_membership(owner_engine, t1, b, "member")
    listing = text("SELECT tenant_id, user_id, role FROM memberships")

    with runtime_engine.begin() as connection:
        _set_user(connection, a)
        assert {tuple(row) for row in connection.execute(listing)} == {
            (t1, a, "owner"),
            (t2, a, "member"),
        }
    with runtime_engine.begin() as connection:
        _set_user(connection, b)
        assert {tuple(row) for row in connection.execute(listing)} == {(t1, b, "member")}
    with runtime_engine.begin() as connection:
        assert connection.execute(listing).all() == []
    with runtime_engine.begin() as connection:
        _set_identity(connection, a_identity)  # identity alone does not reveal memberships
        assert connection.execute(listing).all() == []


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO memberships (tenant_id, user_id, role) VALUES (:t, :u, 'owner')",
        "UPDATE memberships SET role = 'owner' WHERE user_id = :u",
        "DELETE FROM memberships WHERE user_id = :u",
        "TRUNCATE memberships",
    ],
)
def test_runtime_cannot_write_memberships(
    runtime_engine: Engine, owner_engine: Engine, new_identity: Callable[[], Identity], sql: str
) -> None:
    user_id, tenant_id = _seed_user(owner_engine, new_identity()), _seed_tenant(owner_engine)
    _seed_membership(owner_engine, tenant_id, user_id, "member")

    with runtime_engine.begin() as connection:
        _set_user(connection, user_id)
        _expect(connection, INSUFFICIENT_PRIVILEGE, sql, t=tenant_id, u=user_id)


# --- Constraints (schema owner, rolled back) ---------------------------------------------


@pytest.fixture
def owner(owner_engine: Engine) -> Iterator[Connection]:
    with owner_engine.connect() as connection:
        connection.begin()
        yield connection
        connection.rollback()


@pytest.mark.parametrize(
    ("issuer", "subject", "ok"),
    [
        ("i", "s", True),
        ("i" * 512, "s" * 255, True),
        ("", "s", False),
        ("i", "", False),
        ("i" * 513, "s", False),
        ("i", "s" * 256, False),
    ],
)
def test_identity_length_limits(owner: Connection, issuer: str, subject: str, ok: bool) -> None:
    sql = "INSERT INTO users (issuer, subject) VALUES (:i, :s)"
    if ok:
        owner.execute(text(sql), {"i": issuer, "s": subject})
    else:
        _expect(owner, CHECK, sql, i=issuer, s=subject)


def test_identity_is_unique_per_issuer_and_subject(owner: Connection) -> None:
    sql = "INSERT INTO users (issuer, subject) VALUES (:i, :s)"
    owner.execute(text(sql), {"i": "https://a/", "s": "same"})
    owner.execute(text(sql), {"i": "https://b/", "s": "same"})  # same subject, other issuer
    _expect(owner, UNIQUE, sql, i="https://a/", s="same")


def test_membership_constraints(
    owner: Connection, owner_engine: Engine, new_identity: Callable[[], Identity]
) -> None:
    user_id, tenant_id = _seed_user(owner_engine, new_identity()), _seed_tenant(owner_engine)
    insert = "INSERT INTO memberships (tenant_id, user_id, role) VALUES (:t, :u, :r)"

    _expect(owner, CHECK, insert, t=tenant_id, u=user_id, r="admin")
    _expect(owner, FOREIGN_KEY, insert, t=tenant_id, u=UUID(int=1), r="member")
    _expect(owner, FOREIGN_KEY, insert, t=UUID(int=1), u=user_id, r="member")
    owner.execute(text(insert), {"t": tenant_id, "u": user_id, "r": "member"})
    _expect(owner, UNIQUE, insert, t=tenant_id, u=user_id, r="owner")
    _expect(owner, RESTRICT, "DELETE FROM users WHERE id = :u", u=user_id)
    _expect(owner, RESTRICT, "DELETE FROM tenants WHERE id = :t", t=tenant_id)


# --- Policy recursion ------------------------------------------------------------------


def test_every_policy_evaluates_without_recursion(
    runtime_engine: Engine, owner_engine: Engine, new_identity: Callable[[], Identity]
) -> None:
    identity = new_identity()
    user_id, tenant_id = _seed_user(owner_engine, identity), _seed_tenant(owner_engine)
    _seed_membership(owner_engine, tenant_id, user_id, "owner")
    queries = [
        "SELECT count(*) FROM tenants",
        "SELECT count(*) FROM workspaces",
        "SELECT count(*) FROM users",
        "SELECT count(*) FROM memberships",
        "SELECT count(*) FROM memberships m JOIN users u ON u.id = m.user_id "
        "LEFT JOIN tenants t ON t.id = m.tenant_id LEFT JOIN workspaces w ON w.tenant_id = t.id",
    ]

    for engine in (runtime_engine, owner_engine):
        with engine.begin() as connection:
            _set_identity(connection, identity)
            _set_user(connection, user_id)
            connection.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
            )
            for query in queries:
                connection.execute(text(query)).scalar_one()  # 42P17 would raise here
