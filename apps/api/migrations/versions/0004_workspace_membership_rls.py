"""Workspace access requires membership; owner administration; unused grants revoked.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-28

The runtime may read and create workspaces only for the transaction's tenant
AND only if the transaction's user is a member of that tenant, so a tenant
context built for the wrong tenant (a bug, or a membership revoked mid-request)
sees and changes nothing. The schema owner gets an explicit administration
policy, as for tenants, users and memberships. The runtime's UPDATE(name) and
DELETE grants are revoked until endpoints that need them are designed.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
CURRENT_USER_ID = "NULLIF(current_setting('app.user_id', true), '')::uuid"
# memberships' own policy already limits the subquery to the current user's rows;
# the explicit user_id condition keeps this policy correct on its own.
MEMBER_OF_TENANT = (
    f"tenant_id = {CURRENT_TENANT} AND EXISTS ("
    f"SELECT 1 FROM memberships AS m "
    f"WHERE m.tenant_id = workspaces.tenant_id AND m.user_id = {CURRENT_USER_ID})"
)


def upgrade() -> None:
    op.execute("DROP POLICY tenant_isolation ON workspaces")
    op.execute(
        f"CREATE POLICY workspace_member_access ON workspaces FOR ALL TO app_runtime "
        f"USING ({MEMBER_OF_TENANT}) WITH CHECK ({MEMBER_OF_TENANT})"
    )
    op.execute(
        "CREATE POLICY workspaces_admin ON workspaces FOR ALL TO app_owner "
        "USING (true) WITH CHECK (true)"
    )
    op.execute("REVOKE UPDATE (name) ON workspaces FROM app_runtime")
    op.execute("REVOKE DELETE ON workspaces FROM app_runtime")


def downgrade() -> None:
    # Restores revision 0003 exactly (policy text as created by 0001).
    op.execute("GRANT DELETE ON workspaces TO app_runtime")
    op.execute("GRANT UPDATE (name) ON workspaces TO app_runtime")
    op.execute("DROP POLICY workspaces_admin ON workspaces")
    op.execute("DROP POLICY workspace_member_access ON workspaces")
    op.execute(
        f"CREATE POLICY tenant_isolation ON workspaces "
        f"USING (tenant_id = {CURRENT_TENANT}) "
        f"WITH CHECK (tenant_id = {CURRENT_TENANT})"
    )
