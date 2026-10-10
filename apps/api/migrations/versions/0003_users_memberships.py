"""Users and tenant memberships with row-level security.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-28

Identity is (issuer, subject) from a verified token; no email or name is stored.
The API sets the transaction-local settings app.auth_issuer and app.auth_subject
(to provision and look up the user) and app.user_id (to read the user's own
memberships). Tenant and workspace policies are unchanged by this revision.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AUTH_IDENTITY = (
    "issuer = current_setting('app.auth_issuer', true) "
    "AND subject = current_setting('app.auth_subject', true)"
)
CURRENT_USER_ID = "NULLIF(current_setting('app.user_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("length(issuer) BETWEEN 1 AND 512", name=op.f("ck_users_issuer_length")),
        sa.CheckConstraint(
            "length(subject) BETWEEN 1 AND 255", name=op.f("ck_users_subject_length")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("issuer", "subject", name=op.f("uq_users_issuer_subject")),
    )
    op.create_table(
        "memberships",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("role IN ('owner', 'member')", name=op.f("ck_memberships_role")),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_memberships_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_memberships_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "user_id", name=op.f("pk_memberships")),
    )
    op.create_index(op.f("ix_memberships_user_id"), "memberships", ["user_id"])

    # Policies reference only their own table's columns and settings, never
    # tenants or workspaces, so no policy can recurse into another.
    for table in ("users", "memberships"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        # FORCE binds the owner too; administration (migrations, operator
        # onboarding) needs an explicit policy.
        op.execute(
            f"CREATE POLICY {table}_admin ON {table} FOR ALL TO app_owner "
            "USING (true) WITH CHECK (true)"
        )

    # The runtime may provision and read only the identity of the current
    # transaction, and read its own row by id.
    op.execute(
        f"CREATE POLICY user_by_identity ON users FOR SELECT TO app_runtime USING ({AUTH_IDENTITY})"
    )
    op.execute(
        f"CREATE POLICY user_by_id ON users FOR SELECT TO app_runtime "
        f"USING (id = {CURRENT_USER_ID})"
    )
    op.execute(
        f"CREATE POLICY user_provision ON users FOR INSERT TO app_runtime "
        f"WITH CHECK ({AUTH_IDENTITY})"
    )
    # The runtime reads only the current user's memberships and never writes them.
    op.execute(
        f"CREATE POLICY membership_self_read ON memberships FOR SELECT TO app_runtime "
        f"USING (user_id = {CURRENT_USER_ID})"
    )

    op.execute("GRANT SELECT ON users TO app_runtime")
    op.execute("GRANT INSERT (issuer, subject) ON users TO app_runtime")
    op.execute("GRANT SELECT ON memberships TO app_runtime")


def downgrade() -> None:
    # Dropping the tables also drops their policies, index and grants.
    op.drop_table("memberships")
    op.drop_table("users")
