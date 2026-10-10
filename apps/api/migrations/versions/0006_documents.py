"""Documents: metadata, immutable versions and content, with membership RLS.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-29

Uploaded files are untrusted bytes, stored and returned but never parsed. A
document belongs to a workspace of the same tenant (composite foreign keys all
the way down). The runtime may read and insert only: versions and content are
immutable. It sees and inserts rows only for the transaction's tenant and only
if the transaction's user is a member (as workspaces, 0004), and may insert
only rows created by that user. The operator has no access to documents.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
CURRENT_USER_ID = "NULLIF(current_setting('app.user_id', true), '')::uuid"
TABLES = ("documents", "document_versions", "document_contents")


def member_of_tenant(table: str) -> str:
    return (
        f"tenant_id = {CURRENT_TENANT} AND EXISTS ("
        f"SELECT 1 FROM memberships AS m "
        f"WHERE m.tenant_id = {table}.tenant_id AND m.user_id = {CURRENT_USER_ID})"
    )


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(display_name) BETWEEN 1 AND 255", name=op.f("ck_documents_display_name_length")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_documents_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workspace_id"],
            ["workspaces.tenant_id", "workspaces.id"],
            name=op.f("fk_documents_tenant_id_workspace_id_workspaces"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_documents_tenant_id_id")),
    )
    op.create_index(
        op.f("ix_documents_tenant_id_workspace_id"),
        "documents",
        ["tenant_id", "workspace_id"],
        unique=False,
    )
    op.create_table(
        "document_versions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.LargeBinary(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "media_type IN ('application/pdf')", name=op.f("ck_document_versions_media_type")
        ),
        sa.CheckConstraint(
            "octet_length(sha256) = 32", name=op.f("ck_document_versions_sha256_length")
        ),
        sa.CheckConstraint(
            "size_bytes BETWEEN 1 AND 20971520", name=op.f("ck_document_versions_size_bytes")
        ),
        sa.CheckConstraint(
            "version_number >= 1", name=op.f("ck_document_versions_version_number_positive")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_document_versions_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["documents.tenant_id", "documents.id"],
            name=op.f("fk_document_versions_tenant_id_document_id_documents"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_versions")),
        sa.UniqueConstraint(
            "document_id",
            "version_number",
            name=op.f("uq_document_versions_document_id_version_number"),
        ),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_document_versions_tenant_id_id")),
    )
    op.create_table(
        "document_contents",
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.CheckConstraint(
            "octet_length(content) BETWEEN 1 AND 20971520",
            name=op.f("ck_document_contents_content_length"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "version_id"],
            ["document_versions.tenant_id", "document_versions.id"],
            name=op.f("fk_document_contents_tenant_id_version_id_document_versions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("version_id", name=op.f("pk_document_contents")),
    )

    for table in TABLES:
        member = member_of_tenant(table)
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_admin ON {table} FOR ALL TO app_owner "
            f"USING (true) WITH CHECK (true)"
        )
        op.execute(
            f"CREATE POLICY {table}_member_read ON {table} FOR SELECT TO app_runtime "
            f"USING ({member})"
        )
        # Rows that record an author may only be inserted as the transaction's user.
        author = f" AND created_by = {CURRENT_USER_ID}" if table != "document_contents" else ""
        op.execute(
            f"CREATE POLICY {table}_member_insert ON {table} FOR INSERT TO app_runtime "
            f"WITH CHECK ({member}{author})"
        )

    op.execute("GRANT SELECT ON documents, document_versions, document_contents TO app_runtime")
    op.execute(
        "GRANT INSERT (tenant_id, workspace_id, display_name, created_by) "
        "ON documents TO app_runtime"
    )
    op.execute(
        "GRANT INSERT (tenant_id, document_id, version_number, media_type, size_bytes, sha256, "
        "created_by) ON document_versions TO app_runtime"
    )
    op.execute("GRANT INSERT (version_id, tenant_id, content) ON document_contents TO app_runtime")


def downgrade() -> None:
    op.drop_table("document_contents")
    op.drop_table("document_versions")
    op.drop_index(op.f("ix_documents_tenant_id_workspace_id"), table_name="documents")
    op.drop_table("documents")
