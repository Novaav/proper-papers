"""Malware scans: append-only verdicts; bytes readable only when the latest is clean.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-30

`document_scans` records verdicts per document version, ordered by
`scan_number` (the primary key gives each version a total order without
relying on clocks; the latest verdict is the highest number).

Two database invariants:
- A. A new version commits only with scan 1 = clean: a deferred constraint
  trigger checks it at commit (runs as the invoker; no SECURITY DEFINER).
- B. The runtime reads a version's bytes only if its latest verdict is clean:
  the document_contents read policy requires it. A version without any scan
  (uploaded before this revision) is therefore still listed but not readable.

The runtime may insert only scan 1 = clean, as the transaction's user, for a
tenant it is a member of; nothing can be updated or deleted. The operator has
no access. Existing rows are left untouched (no backfill).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
CURRENT_USER_ID = "NULLIF(current_setting('app.user_id', true), '')::uuid"


def member_of_tenant(table: str) -> str:
    # Identical to 0006, so the restored policy on downgrade matches exactly.
    return (
        f"tenant_id = {CURRENT_TENANT} AND EXISTS ("
        f"SELECT 1 FROM memberships AS m "
        f"WHERE m.tenant_id = {table}.tenant_id AND m.user_id = {CURRENT_USER_ID})"
    )


LATEST_VERDICT_IS_CLEAN = (
    "'clean' = (SELECT s.verdict FROM document_scans AS s "
    "WHERE s.tenant_id = document_contents.tenant_id "
    "AND s.version_id = document_contents.version_id "
    "ORDER BY s.scan_number DESC LIMIT 1)"
)


def upgrade() -> None:
    op.create_table(
        "document_scans",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("scan_number", sa.Integer(), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("engine", sa.Text(), nullable=False),
        sa.Column("engine_version", sa.Text(), nullable=False),
        sa.Column("signature_version", sa.Text(), nullable=False),
        sa.Column("signature_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column(
            "scanned_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(engine) BETWEEN 1 AND 100", name=op.f("ck_document_scans_engine_length")
        ),
        sa.CheckConstraint(
            "length(engine_version) BETWEEN 1 AND 100",
            name=op.f("ck_document_scans_engine_version_length"),
        ),
        sa.CheckConstraint(
            "length(signature_version) BETWEEN 1 AND 100",
            name=op.f("ck_document_scans_signature_version_length"),
        ),
        sa.CheckConstraint("scan_number >= 1", name=op.f("ck_document_scans_scan_number_positive")),
        sa.CheckConstraint(
            "verdict IN ('clean', 'infected', 'incomplete', 'encrypted', 'error')",
            name=op.f("ck_document_scans_verdict"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_document_scans_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "version_id"],
            ["document_versions.tenant_id", "document_versions.id"],
            name=op.f("fk_document_scans_tenant_id_version_id_document_versions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "version_id", "scan_number", name=op.f("pk_document_scans")
        ),
    )

    member = member_of_tenant("document_scans")
    op.execute("ALTER TABLE document_scans ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE document_scans FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY document_scans_admin ON document_scans FOR ALL TO app_owner "
        "USING (true) WITH CHECK (true)"
    )
    op.execute(
        "CREATE POLICY document_scans_member_read ON document_scans FOR SELECT TO app_runtime "
        f"USING ({member})"
    )
    # Only the upload's own scan: number 1, clean, by the transaction's user.
    op.execute(
        "CREATE POLICY document_scans_member_insert ON document_scans FOR INSERT TO app_runtime "
        f"WITH CHECK ({member} AND created_by = {CURRENT_USER_ID} "
        "AND scan_number = 1 AND verdict = 'clean')"
    )
    op.execute("GRANT SELECT ON document_scans TO app_runtime")
    op.execute(
        "GRANT INSERT (tenant_id, version_id, scan_number, verdict, engine, engine_version, "
        "signature_version, signature_date, created_by) ON document_scans TO app_runtime"
    )

    # Invariant A: no version without a clean first scan (checked at commit).
    op.execute(
        """
        CREATE FUNCTION document_versions_require_clean_scan() RETURNS trigger
        LANGUAGE plpgsql SET search_path = app, pg_temp AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM document_scans AS s
                WHERE s.tenant_id = NEW.tenant_id AND s.version_id = NEW.id
                  AND s.scan_number = 1 AND s.verdict = 'clean'
            ) THEN
                RAISE EXCEPTION 'document version has no clean first scan'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NULL;
        END
        $$
        """
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER document_versions_require_clean_scan "
        "AFTER INSERT ON document_versions DEFERRABLE INITIALLY DEFERRED "
        "FOR EACH ROW EXECUTE FUNCTION document_versions_require_clean_scan()"
    )

    # Invariant B: bytes are readable only when the latest verdict is clean.
    op.execute("DROP POLICY document_contents_member_read ON document_contents")
    op.execute(
        "CREATE POLICY document_contents_member_read ON document_contents FOR SELECT "
        f"TO app_runtime USING ({member_of_tenant('document_contents')} "
        f"AND {LATEST_VERDICT_IS_CLEAN})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY document_contents_member_read ON document_contents")
    op.execute(
        "CREATE POLICY document_contents_member_read ON document_contents FOR SELECT "
        f"TO app_runtime USING ({member_of_tenant('document_contents')})"
    )
    op.execute("DROP TRIGGER document_versions_require_clean_scan ON document_versions")
    op.execute("DROP FUNCTION document_versions_require_clean_scan()")
    op.drop_table("document_scans")
