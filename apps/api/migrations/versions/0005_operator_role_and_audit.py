"""Operator administration: app_operator grants, operator_audit, member-only tenant reads.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-28

app_operator (created by db/roles.sql) administers tenants and memberships
through the operator CLI: it may read users, create tenants (name only), and
create, re-role and delete memberships. It has no access to workspaces. Every
change is recorded in operator_audit in the same transaction; the operator may
only insert and read audit rows.

The runtime's tenant read policy now also requires a membership of the
transaction's user (as workspaces do since 0004), before any endpoint exposes
tenant data.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
CURRENT_USER_ID = "NULLIF(current_setting('app.user_id', true), '')::uuid"
TENANT_MEMBER = (
    f"id = {CURRENT_TENANT} AND EXISTS ("
    f"SELECT 1 FROM memberships AS m "
    f"WHERE m.tenant_id = tenants.id AND m.user_id = {CURRENT_USER_ID})"
)
ROLES = "'owner', 'member'"


def upgrade() -> None:
    op.create_table(
        "operator_audit",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("db_role", sa.Text(), server_default=sa.text("SESSION_USER"), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("old_role", sa.Text(), nullable=True),
        sa.Column("new_role", sa.Text(), nullable=True),
        sa.Column("request_id", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "action IN ('tenant_created', 'membership_granted', "
            "'membership_role_changed', 'membership_revoked')",
            name=op.f("ck_operator_audit_action"),
        ),
        sa.CheckConstraint(
            f"old_role IS NULL OR old_role IN ({ROLES})", name=op.f("ck_operator_audit_old_role")
        ),
        sa.CheckConstraint(
            f"new_role IS NULL OR new_role IN ({ROLES})", name=op.f("ck_operator_audit_new_role")
        ),
        sa.CheckConstraint(
            "(action = 'tenant_created' AND old_role IS NULL AND new_role = 'owner')"
            " OR (action = 'membership_granted' AND old_role IS NULL AND new_role IS NOT NULL)"
            " OR (action = 'membership_role_changed' AND old_role IS NOT NULL"
            " AND new_role IS NOT NULL AND old_role <> new_role)"
            " OR (action = 'membership_revoked' AND old_role IS NOT NULL AND new_role IS NULL)",
            name=op.f("ck_operator_audit_roles_match_action"),
        ),
        sa.CheckConstraint(
            "(action = 'tenant_created') = (request_id IS NOT NULL)",
            name=op.f("ck_operator_audit_request_id_for_create"),
        ),
        sa.CheckConstraint(
            "length(request_id) BETWEEN 1 AND 200",
            name=op.f("ck_operator_audit_request_id_length"),
        ),
        sa.CheckConstraint(
            "length(btrim(reason)) BETWEEN 1 AND 500", name=op.f("ck_operator_audit_reason_length")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_operator_audit")),
        sa.UniqueConstraint("request_id", name=op.f("uq_operator_audit_request_id")),
    )
    op.create_index(op.f("ix_operator_audit_tenant_id"), "operator_audit", ["tenant_id"])

    op.execute("ALTER TABLE operator_audit ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE operator_audit FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY operator_audit_admin ON operator_audit FOR ALL TO app_owner "
        "USING (true) WITH CHECK (true)"
    )
    # Separate SELECT and INSERT policies: there is deliberately none for UPDATE or DELETE.
    op.execute(
        "CREATE POLICY operator_audit_read ON operator_audit FOR SELECT TO app_operator "
        "USING (true)"
    )
    op.execute(
        "CREATE POLICY operator_audit_append ON operator_audit FOR INSERT TO app_operator "
        "WITH CHECK (db_role = SESSION_USER)"
    )

    op.execute(
        "CREATE POLICY tenants_operator ON tenants FOR ALL TO app_operator "
        "USING (true) WITH CHECK (true)"
    )
    op.execute("CREATE POLICY users_operator_read ON users FOR SELECT TO app_operator USING (true)")
    op.execute(
        "CREATE POLICY memberships_operator ON memberships FOR ALL TO app_operator "
        "USING (true) WITH CHECK (true)"
    )

    op.execute("GRANT SELECT ON tenants TO app_operator")
    op.execute("GRANT INSERT (name) ON tenants TO app_operator")
    op.execute("GRANT SELECT ON users TO app_operator")
    op.execute("GRANT SELECT, DELETE ON memberships TO app_operator")
    op.execute("GRANT INSERT (tenant_id, user_id, role) ON memberships TO app_operator")
    op.execute("GRANT UPDATE (role) ON memberships TO app_operator")
    op.execute("GRANT SELECT ON operator_audit TO app_operator")
    op.execute(
        "GRANT INSERT (action, tenant_id, user_id, old_role, new_role, request_id, reason) "
        "ON operator_audit TO app_operator"
    )

    op.execute("DROP POLICY tenant_self_read ON tenants")
    op.execute(
        f"CREATE POLICY tenant_member_read ON tenants FOR SELECT TO app_runtime "
        f"USING ({TENANT_MEMBER})"
    )


def downgrade() -> None:
    # Restores revision 0004 exactly.
    op.execute("DROP POLICY tenant_member_read ON tenants")
    op.execute(
        f"CREATE POLICY tenant_self_read ON tenants FOR SELECT TO app_runtime "
        f"USING (id = {CURRENT_TENANT})"
    )
    op.execute("REVOKE ALL ON tenants, users, memberships FROM app_operator")
    op.execute("DROP POLICY memberships_operator ON memberships")
    op.execute("DROP POLICY users_operator_read ON users")
    op.execute("DROP POLICY tenants_operator ON tenants")
    op.drop_table("operator_audit")
