"""Row-level security on tenants: the runtime sees only its own tenant.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-28
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    op.execute("ALTER TABLE tenants ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenants FORCE ROW LEVEL SECURITY")
    # app_runtime has only SELECT on tenants (0001); it may read its own tenant.
    op.execute(
        f"CREATE POLICY tenant_self_read ON tenants FOR SELECT TO app_runtime "
        f"USING (id = {CURRENT_TENANT})"
    )
    # FORCE binds the owner too, so tenant administration (creation) needs an
    # explicit policy. Roles without a policy see and change nothing.
    op.execute(
        "CREATE POLICY tenant_admin ON tenants FOR ALL TO app_owner USING (true) WITH CHECK (true)"
    )


def downgrade() -> None:
    op.execute("DROP POLICY tenant_admin ON tenants")
    op.execute("DROP POLICY tenant_self_read ON tenants")
    op.execute("ALTER TABLE tenants NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenants DISABLE ROW LEVEL SECURITY")
