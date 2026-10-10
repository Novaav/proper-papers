"""DI-1 processing: runs, pages, fields, candidates, evidence; the worker's access.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-30

A processing run is one document version processed by one pipeline version
(extractor and exact parser image id); `(version_id, pipeline_version)` is
unique, so processing is idempotent. The worker (app_worker, created by
db/roles.sql; no user identity) claims runs with a lease and writes each run's
results once, in the transaction that ends the run.

Database rules:
- Run state changes only along queued -> running -> completed | needs_review |
  failed, with running -> queued (a retry, below the attempts limit) and the
  takeover of an expired lease; final states and identity columns never change
  (a trigger). A run ending completed or needs_review must have its pages and
  all seven fields at commit (a deferred constraint trigger). Both run as the
  invoker; no SECURITY DEFINER.
- The results (pages, fields, candidates, evidence) are append-only.
- app_runtime: members read their tenant's runs and results; derived text
  (pages, candidates, evidence) only while the version's latest verdict is
  clean, like the bytes (0007). Members create runs only as themselves, queued,
  for a version whose latest verdict is clean.
- app_worker: reads and updates the queue state of runs of every tenant (ids
  and state only); reads the bytes, hash and verdicts only of the version of the
  run it has claimed (`app.run_id`, `app.tenant_id`, running under its lease
  `app.worker_id`) while the latest verdict is clean; inserts results only for
  that run; reads back only what the completeness check needs. It has no access
  to users, memberships, workspaces, documents or audit rows.
- The operator has no access.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
CURRENT_USER_ID = "NULLIF(current_setting('app.user_id', true), '')::uuid"
CURRENT_RUN = "NULLIF(current_setting('app.run_id', true), '')::uuid"
CURRENT_WORKER = "NULLIF(current_setting('app.worker_id', true), '')::uuid"
FINAL = "('completed', 'needs_review', 'failed')"
FAILURE_CODES = (
    "("
    "'input_too_large', "
    "'parse_failed', "
    "'encrypted_pdf', "
    "'embedded_content', "
    "'too_many_pages', "
    "'text_too_large', "
    "'no_text_layer', "
    "'memory_exceeded', "
    "'timeout', "
    "'output_too_large', "
    "'output_invalid', "
    "'not_clean', "
    "'integrity_mismatch', "
    "'extraction_failed', "
    "'sandbox_unavailable', "
    "'worker_lost'"
    ")"
)
FIELD_KEYS = (
    "("
    "'provider_name', "
    "'customer_name', "
    "'effective_date', "
    "'end_date', "
    "'governing_law', "
    "'contract_value', "
    "'notice_period_days'"
    ")"
)
RESULT_TABLES = ("document_pages", "extracted_fields", "field_candidates", "candidate_evidence")
DERIVED_TEXT_TABLES = ("document_pages", "field_candidates", "candidate_evidence")
RUN_STATE_COLUMNS = (
    "status, failure_code, attempts, lease_owner, lease_expires_at, not_before, "
    "started_at, finished_at"
)


def member_of_tenant(table: str) -> str:
    # As 0006 and 0007.
    return (
        f"{table}.tenant_id = {CURRENT_TENANT} AND EXISTS ("
        f"SELECT 1 FROM memberships AS m "
        f"WHERE m.tenant_id = {table}.tenant_id AND m.user_id = {CURRENT_USER_ID})"
    )


def latest_clean(tenant: str, version: str) -> str:
    # As 0007's invariant B.
    return (
        "'clean' = (SELECT s.verdict FROM document_scans AS s "
        f"WHERE s.tenant_id = {tenant} AND s.version_id = {version} "
        "ORDER BY s.scan_number DESC LIMIT 1)"
    )


def claimed_version(tenant: str, version: str) -> str:
    """The version belongs to the run this worker transaction has claimed and holds."""
    return (
        "EXISTS (SELECT 1 FROM processing_runs AS r "
        f"WHERE r.id = {CURRENT_RUN} AND r.tenant_id = {CURRENT_TENANT} "
        f"AND r.tenant_id = {tenant} AND r.version_id = {version} "
        f"AND r.status = 'running' AND r.lease_owner = {CURRENT_WORKER})"
    )


def claimed_run(table: str, *, running: bool) -> str:
    """The row belongs to this worker transaction's claimed run (held under its lease)."""
    state = "AND r.status = 'running' " if running else ""
    return (
        f"{table}.run_id = {CURRENT_RUN} AND {table}.tenant_id = {CURRENT_TENANT} "
        "AND EXISTS (SELECT 1 FROM processing_runs AS r "
        f"WHERE r.id = {table}.run_id AND r.tenant_id = {table}.tenant_id "
        f"{state}AND r.lease_owner = {CURRENT_WORKER})"
    )


def run_version_clean(table: str) -> str:
    return (
        "EXISTS (SELECT 1 FROM processing_runs AS r "
        f"WHERE r.id = {table}.run_id AND r.tenant_id = {table}.tenant_id "
        f"AND {latest_clean('r.tenant_id', 'r.version_id')})"
    )


TRANSITION_FUNCTION = f"""
CREATE FUNCTION processing_runs_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.id IS DISTINCT FROM OLD.id OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
     OR NEW.version_id IS DISTINCT FROM OLD.version_id
     OR NEW.pipeline_version IS DISTINCT FROM OLD.pipeline_version
     OR NEW.created_by IS DISTINCT FROM OLD.created_by
     OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'processing run identity is immutable' USING ERRCODE = 'check_violation';
  END IF;
  IF OLD.status IN {FINAL} THEN
    RAISE EXCEPTION 'a finished processing run cannot change' USING ERRCODE = 'check_violation';
  END IF;
  IF OLD.status = 'queued' AND NEW.status = 'running'
     AND NEW.attempts = OLD.attempts + 1 THEN
    RETURN NEW;
  END IF;
  IF OLD.status = 'running' AND NEW.status = 'running' AND OLD.lease_expires_at < now()
     AND NEW.attempts = OLD.attempts + 1 THEN
    RETURN NEW;  -- takeover of an expired lease
  END IF;
  IF OLD.status = 'running' AND NEW.status = 'queued' AND NEW.attempts = OLD.attempts
     AND OLD.attempts < 3 AND NEW.lease_owner IS NULL AND NEW.lease_expires_at IS NULL THEN
    RETURN NEW;  -- a retry
  END IF;
  IF OLD.status = 'running' AND NEW.status IN {FINAL} AND NEW.attempts = OLD.attempts THEN
    RETURN NEW;
  END IF;
  RAISE EXCEPTION 'processing run transition not allowed' USING ERRCODE = 'check_violation';
END
$$
"""

COMPLETENESS_FUNCTION = """
CREATE FUNCTION processing_runs_require_results() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (SELECT count(*) FROM extracted_fields AS f
      WHERE f.tenant_id = NEW.tenant_id AND f.run_id = NEW.id) <> 7
     OR NOT EXISTS (SELECT 1 FROM document_pages AS p
                    WHERE p.tenant_id = NEW.tenant_id AND p.run_id = NEW.id) THEN
    RAISE EXCEPTION 'a finished processing run needs its pages and all seven fields'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NULL;
END
$$
"""


def upgrade() -> None:
    op.create_table(
        "processing_runs",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("pipeline_version", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("failure_code", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("lease_owner", sa.Uuid(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "not_before",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("parser_versions", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("parser_features", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'failed') = (failure_code IS NOT NULL)",
            name=op.f("ck_processing_runs_failed_has_code"),
        ),
        sa.CheckConstraint(
            "(status IN ('completed', 'needs_review', 'failed')) = (finished_at IS NOT NULL)",
            name=op.f("ck_processing_runs_final_has_finish"),
        ),
        sa.CheckConstraint(
            "failure_code IS NULL OR failure_code IN " + FAILURE_CODES,
            name=op.f("ck_processing_runs_failure_code"),
        ),
        sa.CheckConstraint(
            "status <> 'running' OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name=op.f("ck_processing_runs_running_has_lease"),
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'needs_review', 'failed')",
            name=op.f("ck_processing_runs_status"),
        ),
        sa.CheckConstraint("attempts BETWEEN 0 AND 3", name=op.f("ck_processing_runs_attempts")),
        sa.CheckConstraint(
            "length(pipeline_version) BETWEEN 1 AND 200",
            name=op.f("ck_processing_runs_pipeline_version_length"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_processing_runs_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "version_id"],
            ["document_versions.tenant_id", "document_versions.id"],
            name=op.f("fk_processing_runs_tenant_id_version_id_document_versions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_processing_runs")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_processing_runs_tenant_id_id")),
        sa.UniqueConstraint(
            "version_id",
            "pipeline_version",
            name=op.f("uq_processing_runs_version_id_pipeline_version"),
        ),
    )
    op.create_index(
        "ix_processing_runs_claimable",
        "processing_runs",
        ["pipeline_version", "created_at"],
        unique=False,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_table(
        "document_pages",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_sha256", sa.LargeBinary(), nullable=False),
        sa.Column("off_page_chars", sa.Integer(), nullable=False),
        sa.Column("invisible_chars", sa.Integer(), nullable=False),
        sa.Column("undefined_chars", sa.Integer(), nullable=False),
        sa.Column("control_chars_removed", sa.Integer(), nullable=False),
        sa.CheckConstraint("length(text) <= 200000", name=op.f("ck_document_pages_text_length")),
        sa.CheckConstraint(
            "octet_length(text_sha256) = 32", name=op.f("ck_document_pages_text_sha256_length")
        ),
        sa.CheckConstraint(
            "off_page_chars >= 0 AND invisible_chars >= 0 AND undefined_chars >= 0 "
            "AND control_chars_removed >= 0",
            name=op.f("ck_document_pages_counts"),
        ),
        sa.CheckConstraint(
            "page_number BETWEEN 1 AND 100", name=op.f("ck_document_pages_page_number")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["processing_runs.tenant_id", "processing_runs.id"],
            name=op.f("fk_document_pages_tenant_id_run_id_processing_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("run_id", "page_number", name=op.f("pk_document_pages")),
        sa.UniqueConstraint(
            "tenant_id",
            "run_id",
            "page_number",
            name=op.f("uq_document_pages_tenant_id_run_id_page_number"),
        ),
    )
    op.create_table(
        "extracted_fields",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("field_key", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "(status = 'unknown') = (reason IS NOT NULL)",
            name=op.f("ck_extracted_fields_unknown_has_reason"),
        ),
        sa.CheckConstraint(
            "field_key IN " + FIELD_KEYS,
            name=op.f("ck_extracted_fields_field_key"),
        ),
        sa.CheckConstraint(
            "reason IS NULL OR reason IN ('not_found', 'unparseable', 'ambiguous')",
            name=op.f("ck_extracted_fields_reason"),
        ),
        sa.CheckConstraint(
            "status IN ('found', 'unknown', 'conflict')", name=op.f("ck_extracted_fields_status")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["processing_runs.tenant_id", "processing_runs.id"],
            name=op.f("fk_extracted_fields_tenant_id_run_id_processing_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("run_id", "field_key", name=op.f("pk_extracted_fields")),
        sa.UniqueConstraint(
            "tenant_id",
            "run_id",
            "field_key",
            name=op.f("uq_extracted_fields_tenant_id_run_id_field_key"),
        ),
    )
    op.create_table(
        "field_candidates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("field_key", sa.Text(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("raw", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "length(raw) BETWEEN 1 AND 200000", name=op.f("ck_field_candidates_raw_length")
        ),
        sa.CheckConstraint("ordinal >= 0", name=op.f("ck_field_candidates_ordinal")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "field_key"],
            ["extracted_fields.tenant_id", "extracted_fields.run_id", "extracted_fields.field_key"],
            name=op.f("fk_field_candidates_tenant_id_run_id_field_key_extracted_fields"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_field_candidates")),
        sa.UniqueConstraint(
            "run_id",
            "field_key",
            "ordinal",
            name=op.f("uq_field_candidates_run_id_field_key_ordinal"),
        ),
        sa.UniqueConstraint(
            "tenant_id", "run_id", "id", name=op.f("uq_field_candidates_tenant_id_run_id_id")
        ),
    )
    op.create_table(
        "candidate_evidence",
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "char_end - char_start = length(quote)", name=op.f("ck_candidate_evidence_quote_length")
        ),
        sa.CheckConstraint(
            "char_start >= 0 AND char_end > char_start", name=op.f("ck_candidate_evidence_range")
        ),
        sa.CheckConstraint("ordinal >= 0", name=op.f("ck_candidate_evidence_ordinal")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "candidate_id"],
            ["field_candidates.tenant_id", "field_candidates.run_id", "field_candidates.id"],
            name=op.f("fk_candidate_evidence_tenant_id_run_id_candidate_id_field_candidates"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "page_number"],
            ["document_pages.tenant_id", "document_pages.run_id", "document_pages.page_number"],
            name=op.f("fk_candidate_evidence_tenant_id_run_id_page_number_document_pages"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("candidate_id", "ordinal", name=op.f("pk_candidate_evidence")),
    )

    op.execute(TRANSITION_FUNCTION)
    op.execute(
        "CREATE TRIGGER processing_runs_transition BEFORE UPDATE ON processing_runs "
        "FOR EACH ROW EXECUTE FUNCTION processing_runs_transition()"
    )
    op.execute(COMPLETENESS_FUNCTION)
    op.execute(
        "CREATE CONSTRAINT TRIGGER processing_runs_require_results AFTER UPDATE ON "
        "processing_runs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "WHEN (NEW.status IN ('completed', 'needs_review')) "
        "EXECUTE FUNCTION processing_runs_require_results()"
    )

    for table in ("processing_runs", *RESULT_TABLES):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_admin ON {table} FOR ALL TO app_owner "
            "USING (true) WITH CHECK (true)"
        )

    # app_runtime (the API, as a member).
    op.execute(
        "CREATE POLICY processing_runs_member_read ON processing_runs FOR SELECT "
        f"TO app_runtime USING ({member_of_tenant('processing_runs')})"
    )
    op.execute(
        "CREATE POLICY processing_runs_member_create ON processing_runs FOR INSERT "
        f"TO app_runtime WITH CHECK ({member_of_tenant('processing_runs')} "
        f"AND created_by = {CURRENT_USER_ID} AND status = 'queued' AND attempts = 0 "
        "AND failure_code IS NULL AND lease_owner IS NULL AND lease_expires_at IS NULL "
        "AND started_at IS NULL AND finished_at IS NULL "
        "AND parser_versions IS NULL AND parser_features IS NULL "
        f"AND {latest_clean('processing_runs.tenant_id', 'processing_runs.version_id')})"
    )
    op.execute("GRANT SELECT ON processing_runs TO app_runtime")
    op.execute(
        "GRANT INSERT (tenant_id, version_id, pipeline_version, created_by) "
        "ON processing_runs TO app_runtime"
    )
    op.execute(
        "CREATE POLICY extracted_fields_member_read ON extracted_fields FOR SELECT "
        f"TO app_runtime USING ({member_of_tenant('extracted_fields')})"
    )
    for table in DERIVED_TEXT_TABLES:
        op.execute(
            f"CREATE POLICY {table}_member_read ON {table} FOR SELECT TO app_runtime "
            f"USING ({member_of_tenant(table)} AND {run_version_clean(table)})"
        )
    op.execute("GRANT SELECT ON " + ", ".join(RESULT_TABLES) + " TO app_runtime")

    # app_worker: the queue (every tenant, ids and state only).
    op.execute(
        "CREATE POLICY processing_runs_worker_read ON processing_runs FOR SELECT "
        "TO app_worker USING (true)"
    )
    op.execute(
        "CREATE POLICY processing_runs_worker_update ON processing_runs FOR UPDATE "
        "TO app_worker USING (status = 'queued' OR (status = 'running' AND "
        f"(lease_owner = {CURRENT_WORKER} OR lease_expires_at < now()))) WITH CHECK (true)"
    )
    op.execute(
        "GRANT SELECT (id, tenant_id, version_id, pipeline_version, created_at, "
        f"{RUN_STATE_COLUMNS}) ON processing_runs TO app_worker"
    )
    op.execute(
        f"GRANT UPDATE ({RUN_STATE_COLUMNS}, parser_versions, parser_features) "
        "ON processing_runs TO app_worker"
    )
    # app_worker: the claimed run's version only, while its latest verdict is clean.
    op.execute(
        "CREATE POLICY document_contents_worker_read ON document_contents FOR SELECT "
        "TO app_worker USING ("
        f"{claimed_version('document_contents.tenant_id', 'document_contents.version_id')} "
        f"AND {latest_clean('document_contents.tenant_id', 'document_contents.version_id')})"
    )
    op.execute("GRANT SELECT (version_id, tenant_id, content) ON document_contents TO app_worker")
    op.execute(
        "CREATE POLICY document_versions_worker_read ON document_versions FOR SELECT "
        "TO app_worker USING ("
        f"{claimed_version('document_versions.tenant_id', 'document_versions.id')})"
    )
    op.execute("GRANT SELECT (id, tenant_id, sha256) ON document_versions TO app_worker")
    op.execute(
        "CREATE POLICY document_scans_worker_read ON document_scans FOR SELECT "
        "TO app_worker USING ("
        f"{claimed_version('document_scans.tenant_id', 'document_scans.version_id')})"
    )
    op.execute(
        "GRANT SELECT (tenant_id, version_id, scan_number, verdict) ON document_scans TO app_worker"
    )
    # app_worker: results of the claimed run, written while it is running under the lease.
    for table in RESULT_TABLES:
        op.execute(
            f"CREATE POLICY {table}_worker_insert ON {table} FOR INSERT TO app_worker "
            f"WITH CHECK ({claimed_run(table, running=True)})"
        )
    op.execute("GRANT INSERT ON " + ", ".join(RESULT_TABLES) + " TO app_worker")
    # Read back only for the completeness check at commit (the run is final by then).
    for table, columns in (
        ("document_pages", "run_id, tenant_id, page_number"),
        ("extracted_fields", "run_id, tenant_id, field_key"),
    ):
        op.execute(
            f"CREATE POLICY {table}_worker_read ON {table} FOR SELECT TO app_worker "
            f"USING ({claimed_run(table, running=False)})"
        )
        op.execute(f"GRANT SELECT ({columns}) ON {table} TO app_worker")


def downgrade() -> None:
    op.execute("DROP POLICY document_scans_worker_read ON document_scans")
    op.execute("DROP POLICY document_versions_worker_read ON document_versions")
    op.execute("DROP POLICY document_contents_worker_read ON document_contents")
    op.execute("REVOKE ALL ON document_scans, document_versions, document_contents FROM app_worker")
    op.drop_table("candidate_evidence")
    op.drop_table("field_candidates")
    op.drop_table("extracted_fields")
    op.drop_table("document_pages")
    op.drop_index(
        "ix_processing_runs_claimable",
        table_name="processing_runs",
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.drop_table("processing_runs")
    op.execute("DROP FUNCTION processing_runs_require_results()")
    op.execute("DROP FUNCTION processing_runs_transition()")
