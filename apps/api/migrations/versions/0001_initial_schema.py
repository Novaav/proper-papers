"""Tenants and workspaces with row-level security on workspaces.

Revision ID: 0001
Revises:
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAME_LENGTH_CHECK = "length(btrim(name)) BETWEEN 1 AND 200"

# The tenant of the current transaction, set by tenant_transaction() with
# set_config(..., true). Unset or empty yields NULL, which matches no row.
CURRENT_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(NAME_LENGTH_CHECK, name=op.f("ck_tenants_name_length")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenants")),
    )
    op.create_table(
        "workspaces",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(NAME_LENGTH_CHECK, name=op.f("ck_workspaces_name_length")),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_workspaces_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspaces")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_workspaces_tenant_id_id")),
    )

    # FORCE also applies the policy to the table owner; nothing bypasses it
    # except superusers and BYPASSRLS roles, which the API refuses to use.
    op.execute("ALTER TABLE workspaces ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE workspaces FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON workspaces "
        f"USING (tenant_id = {CURRENT_TENANT}) "
        f"WITH CHECK (tenant_id = {CURRENT_TENANT})"
    )

    # Runtime privileges: read tenants; read, create, rename and delete
    # workspaces within its tenant. id and tenant_id are never updatable.
    op.execute("GRANT SELECT ON tenants TO app_runtime")
    op.execute("GRANT SELECT, INSERT, DELETE ON workspaces TO app_runtime")
    op.execute("GRANT UPDATE (name) ON workspaces TO app_runtime")


def downgrade() -> None:
    # Dropping the tables also drops their policy and grants.
    op.drop_table("workspaces")
    op.drop_table("tenants")
