"""app_operator's privileges (decision D-A13), exercised directly in SQL on app_test."""

from collections.abc import Callable, Iterable, Iterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from api.database import DatabaseSettings
from tests.conftest import SeededTenant, insert_user, sqlstate

Seed = Callable[..., SeededTenant]
INSUFFICIENT_PRIVILEGE = "42501"


@pytest.fixture
def operator(operator_settings: DatabaseSettings) -> Iterator[Connection]:
    """An app_operator connection inside a transaction that is rolled back."""
    engine = create_engine(operator_settings.url, poolclass=NullPool)
    with engine.connect() as connection:
        connection.begin()
        yield connection
        connection.rollback()
    engine.dispose()


def _denied(connection: Connection, sql: str, **params: object) -> None:
    with pytest.raises(DBAPIError) as error, connection.begin_nested():
        connection.execute(text(sql), params)
    assert sqlstate(error.value) == INSUFFICIENT_PRIVILEGE, sql


def _audit(connection: Connection, tenant_id: UUID, user_id: UUID) -> None:
    connection.execute(
        text(
            "INSERT INTO operator_audit (action, tenant_id, user_id, new_role, request_id, reason) "
            "VALUES ('tenant_created', :t, :u, 'owner', :r, 'test')"
        ),
        {"t": tenant_id, "u": user_id, "r": f"req-{uuid4()}"},
    )


def test_operator_role_has_no_elevated_attributes(owner_engine: Engine) -> None:
    with owner_engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, rolreplication, "
                "rolcanlogin FROM pg_roles WHERE rolname = 'app_operator'"
            )
        ).one()
        owns: Iterable[int] = connection.execute(
            text(
                "SELECT count(*) FROM pg_class WHERE relowner = 'app_operator'::regrole "
                "UNION ALL "
                "SELECT count(*) FROM pg_namespace WHERE nspowner = 'app_operator'::regrole"
            )
        ).scalars()
        memberships: int = connection.execute(
            text(
                "SELECT count(*) FROM pg_auth_members "
                "WHERE member = 'app_operator'::regrole OR roleid = 'app_operator'::regrole"
            )
        ).scalar_one()
    assert tuple(row) == (False, False, False, False, False, True)
    assert list(owns) == [0, 0]
    assert memberships == 0


def test_exact_operator_privileges(owner_engine: Engine) -> None:
    tables = (
        "tenants",
        "workspaces",
        "users",
        "memberships",
        "operator_audit",
        "documents",
        "document_versions",
        "document_contents",
        "alembic_version",
    )
    privileges = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
    with owner_engine.connect() as connection:
        granted = {
            (table, privilege)
            for table in tables
            for privilege in privileges
            if connection.execute(
                text("SELECT has_table_privilege('app_operator', :t, :p)"),
                {"t": f"app.{table}", "p": privilege},
            ).scalar_one()
        }
        columns = set(
            connection.execute(
                text(
                    "SELECT table_name, column_name, privilege_type "
                    "FROM information_schema.column_privileges "
                    "WHERE grantee = 'app_operator' AND table_schema = 'app' "
                    "AND privilege_type IN ('INSERT', 'UPDATE')"
                )
            ).all()
        )
        schema_create: bool = connection.execute(
            text("SELECT has_schema_privilege('app_operator', 'app', 'CREATE')")
        ).scalar_one()

    assert granted == {
        ("tenants", "SELECT"),
        ("users", "SELECT"),
        ("memberships", "SELECT"),
        ("memberships", "DELETE"),
        ("operator_audit", "SELECT"),
    }
    assert columns == {
        ("tenants", "name", "INSERT"),
        ("memberships", "tenant_id", "INSERT"),
        ("memberships", "user_id", "INSERT"),
        ("memberships", "role", "INSERT"),
        ("memberships", "role", "UPDATE"),
        *(
            ("operator_audit", column, "INSERT")
            for column in (
                "action",
                "tenant_id",
                "user_id",
                "old_role",
                "new_role",
                "request_id",
                "reason",
            )
        ),
    }
    assert schema_create is False


def test_operator_can_do_its_job(operator: Connection, owner_engine: Engine) -> None:
    with owner_engine.begin() as owner:
        user_id = insert_user(owner)
    tenant_id: UUID = operator.execute(
        text("INSERT INTO tenants (name) VALUES ('Acme') RETURNING id")
    ).scalar_one()
    operator.execute(
        text("INSERT INTO memberships (tenant_id, user_id, role) VALUES (:t, :u, 'member')"),
        {"t": tenant_id, "u": user_id},
    )
    operator.execute(
        text("UPDATE memberships SET role = 'owner' WHERE tenant_id = :t"), {"t": tenant_id}
    )
    _audit(operator, tenant_id, user_id)
    row = operator.execute(
        text("SELECT db_role FROM operator_audit WHERE tenant_id = :t"), {"t": tenant_id}
    ).one()
    operator.execute(text("DELETE FROM memberships WHERE tenant_id = :t"), {"t": tenant_id})

    assert row.db_role == "app_operator"


@pytest.mark.parametrize(
    "sql",
    [
        # Tenant content is out of reach.
        "SELECT count(*) FROM workspaces",
        "INSERT INTO workspaces (tenant_id, name) VALUES (:t, 'w')",
        # Tenants: create by name only; no update, delete or chosen id.
        "INSERT INTO tenants (id, name) VALUES (gen_random_uuid(), 'x')",
        "UPDATE tenants SET name = 'x' WHERE id = :t",
        "DELETE FROM tenants WHERE id = :t",
        # Users are read-only.
        "INSERT INTO users (issuer, subject) VALUES ('https://x/', 'y')",
        "UPDATE users SET subject = 'y' WHERE id = :u",
        "DELETE FROM users WHERE id = :u",
        # Memberships cannot be moved between tenants or users.
        "UPDATE memberships SET tenant_id = :t WHERE user_id = :u",
        "UPDATE memberships SET user_id = :u WHERE tenant_id = :t",
        # The audit is append-only, and its server-set columns cannot be supplied.
        "UPDATE operator_audit SET reason = 'x'",
        "DELETE FROM operator_audit",
        "INSERT INTO operator_audit (db_role, action, tenant_id, user_id, new_role, request_id, "
        "reason) VALUES ('someone', 'tenant_created', :t, :u, 'owner', 'r-db-role', 'x')",
        "INSERT INTO operator_audit (occurred_at, action, tenant_id, user_id, new_role, "
        "request_id, reason) VALUES (now() - interval '1 year', 'tenant_created', :t, :u, "
        "'owner', 'r-time', 'x')",
        # No DDL, bulk deletion or role switching.
        "TRUNCATE operator_audit",
        "TRUNCATE memberships",
        "CREATE TABLE app.extra (id int)",
        "ALTER TABLE operator_audit DISABLE ROW LEVEL SECURITY",
        "DROP POLICY operator_audit_append ON operator_audit",
        "CREATE POLICY anything ON operator_audit FOR UPDATE TO app_operator USING (true)",
        "SET ROLE app_owner",
        "SET ROLE app_runtime",
        "SELECT * FROM alembic_version",
    ],
)
def test_operator_is_denied_everything_else(
    operator: Connection, seed_tenant: Seed, sql: str
) -> None:
    seeded = seed_tenant()
    _denied(operator, sql, t=seeded.ctx.tenant_id, u=seeded.ctx.user_id)


def test_audit_rows_cannot_claim_another_database_role(
    operator: Connection, seed_tenant: Seed
) -> None:
    # db_role is not insertable (above); the policy also requires it to be the
    # session user, which the default always is.
    seeded = seed_tenant()
    _audit(operator, seeded.ctx.tenant_id, seeded.ctx.user_id)
    roles: set[str] = set(
        operator.execute(text("SELECT DISTINCT db_role FROM operator_audit")).scalars()
    )
    assert roles == {"app_operator"}


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) FROM operator_audit",
        "INSERT INTO operator_audit (action, tenant_id, user_id, new_role, request_id, reason) "
        "VALUES ('tenant_created', gen_random_uuid(), gen_random_uuid(), 'owner', 'r', 'x')",
        "INSERT INTO tenants (name) VALUES ('t')",
        "INSERT INTO memberships (tenant_id, user_id, role) "
        "VALUES (gen_random_uuid(), gen_random_uuid(), 'owner')",
    ],
)
def test_runtime_cannot_touch_the_audit_or_administer(runtime_engine: Engine, sql: str) -> None:
    with runtime_engine.connect() as connection, connection.begin():
        _denied(connection, sql)


def test_owner_administers_the_audit(owner_engine: Engine, seed_tenant: Seed) -> None:
    seeded = seed_tenant()
    with owner_engine.connect() as connection:
        connection.begin()
        _audit(connection, seeded.ctx.tenant_id, seeded.ctx.user_id)
        db_role: str = connection.execute(
            text("SELECT db_role FROM operator_audit WHERE tenant_id = :t"),
            {"t": seeded.ctx.tenant_id},
        ).scalar_one()
        connection.rollback()
    assert db_role == "app_owner"
