"""Constraints, checked as the schema owner inside transactions that are rolled back."""

from collections.abc import Callable, Iterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from tests.conftest import SeededTenant, sqlstate

NOT_NULL, FOREIGN_KEY, RESTRICT, CHECK = "23502", "23503", "23001", "23514"


@pytest.fixture
def owner(owner_engine: Engine) -> Iterator[Connection]:
    with owner_engine.connect() as connection:
        connection.begin()
        yield connection
        connection.rollback()


def _expect_violation(connection: Connection, code: str, sql: str, **params: object) -> None:
    with pytest.raises(DBAPIError) as error, connection.begin_nested():
        connection.execute(text(sql), params)
    assert sqlstate(error.value) == code


@pytest.mark.parametrize("name", ["", "   ", "x" * 201, " " + "x" * 201])
def test_tenant_name_length_is_checked(owner: Connection, name: str) -> None:
    _expect_violation(owner, CHECK, "INSERT INTO tenants (name) VALUES (:n)", n=name)


@pytest.mark.parametrize("name", ["x", "x" * 200, "  padded  "])
def test_tenant_names_within_limits_are_accepted(owner: Connection, name: str) -> None:
    owner.execute(text("INSERT INTO tenants (name) VALUES (:n)"), {"n": name})


def test_tenant_name_is_required(owner: Connection) -> None:
    _expect_violation(owner, NOT_NULL, "INSERT INTO tenants (name) VALUES (NULL)")


def test_generated_ids_are_uuid_version_7(owner: Connection) -> None:
    tenant_id: UUID = owner.execute(
        text("INSERT INTO tenants (name) VALUES ('t') RETURNING id")
    ).scalar_one()

    assert isinstance(tenant_id, UUID)
    assert tenant_id.version == 7


@pytest.mark.parametrize("name", ["", "   ", "x" * 201])
def test_workspace_name_length_is_checked(owner: Connection, name: str) -> None:
    tenant_id: UUID = owner.execute(
        text("INSERT INTO tenants (name) VALUES ('t') RETURNING id")
    ).scalar_one()

    _expect_violation(
        owner,
        CHECK,
        "INSERT INTO workspaces (tenant_id, name) VALUES (:t, :n)",
        t=tenant_id,
        n=name,
    )


def test_workspace_tenant_is_required(owner: Connection) -> None:
    # The owner's administration policy admits any row, so NOT NULL is what
    # fails. (For the runtime, the membership policy rejects a NULL tenant first.)
    _expect_violation(
        owner, NOT_NULL, "INSERT INTO workspaces (tenant_id, name) VALUES (NULL, 'w')"
    )
    not_null: bool = owner.execute(
        text(
            "SELECT attnotnull FROM pg_attribute "
            "WHERE attrelid = 'app.workspaces'::regclass AND attname = 'tenant_id'"
        )
    ).scalar_one()
    assert not_null is True


def test_workspace_must_reference_an_existing_tenant(owner: Connection) -> None:
    unknown = uuid4()  # the owner's administration policy admits the row; the key fails

    _expect_violation(
        owner, FOREIGN_KEY, "INSERT INTO workspaces (tenant_id, name) VALUES (:t, 'w')", t=unknown
    )


def test_tenant_with_workspaces_cannot_be_deleted(
    owner: Connection, seed_tenant: Callable[[], SeededTenant]
) -> None:
    seeded = seed_tenant()

    _expect_violation(owner, RESTRICT, "DELETE FROM tenants WHERE id = :t", t=seeded.ctx.tenant_id)


def test_composite_key_lets_child_tables_pin_the_workspace_tenant(owner: Connection) -> None:
    a: UUID = owner.execute(
        text("INSERT INTO tenants (name) VALUES ('a') RETURNING id")
    ).scalar_one()
    b: UUID = owner.execute(
        text("INSERT INTO tenants (name) VALUES ('b') RETURNING id")
    ).scalar_one()
    workspace_a: UUID = owner.execute(
        text("INSERT INTO workspaces (tenant_id, name) VALUES (:t, 'w') RETURNING id"), {"t": a}
    ).scalar_one()
    # A future child table (created only inside this rolled-back transaction).
    owner.execute(
        text(
            "CREATE TABLE child (tenant_id uuid NOT NULL, workspace_id uuid NOT NULL, "
            "FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces (tenant_id, id))"
        )
    )

    owner.execute(text("INSERT INTO child VALUES (:t, :w)"), {"t": a, "w": workspace_a})
    _expect_violation(owner, FOREIGN_KEY, "INSERT INTO child VALUES (:t, :w)", t=b, w=workspace_a)
