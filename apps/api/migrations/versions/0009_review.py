"""R1 review: the reviewer role, review decisions and approved records.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-01

A reviewer (membership role `owner` or the new `reviewer`) resolves fields of a
finished run and approves the record; a `member` can only read.

- review_decisions: one row per decision, numbered per run (`seq` = previous +
  1). A decision chooses one of the field's candidates by ordinal (only a
  candidate with a valid value, of a found or conflicting field) or sets the
  field to UNKNOWN. There is no free text.
- approved_records: immutable versions per document (`version` = previous + 1),
  frozen from one run and the decisions up to `decisions_through`, which must
  be the run's newest decision at insert. The record is the canonical JSON text
  of `approved_record.v1` with its SHA-256; the database checks the hash, that
  the record names exactly this row (document, version, run, approver, time),
  and that every field is either `found` with a value and at least one evidence
  span or `unknown` with no value and a reason.

Database rules:
- Both tables are append-only for every role, app_owner included: a trigger
  refuses UPDATE, DELETE and TRUNCATE. Triggers run as the invoker; no SECURITY
  DEFINER.
- A next number that is not previous + 1 raises unique_violation, like the race
  it reports (two writers with the same expectation); a decision newer than an
  approval's `decisions_through` raises SQLSTATE RV001 (the record changed).
- app_runtime: members read their tenant's decisions, and approved records while
  the version's latest verdict is clean (the record contains quotes). Only an
  owner or reviewer inserts, only as themselves; ids and timestamps are set by
  the database (column grants). No UPDATE or DELETE.
- app_worker and the operator have no access.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
CURRENT_USER_ID = "NULLIF(current_setting('app.user_id', true), '')::uuid"
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
SUCCESSFUL = "('completed', 'needs_review')"
ROLES_BEFORE = "('owner', 'member')"
ROLES_AFTER = "('owner', 'reviewer', 'member')"
TABLES = ("review_decisions", "approved_records")
APPROVED_RECORD_MAX_LENGTH = 2_000_000


def member_of_tenant(table: str) -> str:
    # As 0006 to 0008.
    return (
        f"{table}.tenant_id = {CURRENT_TENANT} AND EXISTS ("
        f"SELECT 1 FROM memberships AS m "
        f"WHERE m.tenant_id = {table}.tenant_id AND m.user_id = {CURRENT_USER_ID})"
    )


def reviewer_of_tenant(table: str) -> str:
    return (
        f"{table}.tenant_id = {CURRENT_TENANT} AND EXISTS ("
        f"SELECT 1 FROM memberships AS m "
        f"WHERE m.tenant_id = {table}.tenant_id AND m.user_id = {CURRENT_USER_ID} "
        "AND m.role IN ('owner', 'reviewer'))"
    )


def latest_clean(tenant: str, version: str) -> str:
    # As 0007's invariant B.
    return (
        "'clean' = (SELECT s.verdict FROM document_scans AS s "
        f"WHERE s.tenant_id = {tenant} AND s.version_id = {version} "
        "ORDER BY s.scan_number DESC LIMIT 1)"
    )


DECISION_FUNCTION = f"""
CREATE FUNCTION review_decisions_check() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  run_status text;
  field_status text;
  latest integer;
BEGIN
  SELECT r.status INTO run_status FROM processing_runs AS r
    WHERE r.id = NEW.run_id AND r.tenant_id = NEW.tenant_id;
  IF run_status IS NULL OR run_status NOT IN {SUCCESSFUL} THEN
    RAISE EXCEPTION 'a review decision needs a run that completed or needs review'
      USING ERRCODE = 'check_violation';
  END IF;
  SELECT coalesce(max(d.seq), 0) INTO latest FROM review_decisions AS d
    WHERE d.run_id = NEW.run_id;
  IF NEW.seq <> latest + 1 THEN
    RAISE EXCEPTION 'review decision out of sequence' USING ERRCODE = 'unique_violation';
  END IF;
  IF NEW.resolution = 'candidate' THEN
    SELECT f.status INTO field_status FROM extracted_fields AS f
      WHERE f.run_id = NEW.run_id AND f.tenant_id = NEW.tenant_id
        AND f.field_key = NEW.field_key;
    IF coalesce(field_status, '') NOT IN ('found', 'conflict') OR NOT EXISTS (
         SELECT 1 FROM field_candidates AS c
         WHERE c.run_id = NEW.run_id AND c.tenant_id = NEW.tenant_id
           AND c.field_key = NEW.field_key AND c.ordinal = NEW.candidate_ordinal
           AND c.value IS NOT NULL AND jsonb_typeof(c.value) <> 'null') THEN
      RAISE EXCEPTION 'only a candidate with a valid value of a found or conflicting field '
        'can be chosen' USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  RETURN NEW;
END
$$
"""

APPROVAL_FUNCTION = f"""
CREATE FUNCTION approved_records_check() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  run_version uuid;
  run_status text;
  version_document uuid;
  latest_version integer;
  latest_seq integer;
  body jsonb;
BEGIN
  SELECT r.version_id, r.status INTO run_version, run_status FROM processing_runs AS r
    WHERE r.id = NEW.run_id AND r.tenant_id = NEW.tenant_id;
  IF run_status IS NULL OR run_status NOT IN {SUCCESSFUL}
     OR run_version IS DISTINCT FROM NEW.document_version_id THEN
    RAISE EXCEPTION 'an approval needs a run of this document version that completed or '
      'needs review' USING ERRCODE = 'check_violation';
  END IF;
  SELECT v.document_id INTO version_document FROM document_versions AS v
    WHERE v.id = NEW.document_version_id AND v.tenant_id = NEW.tenant_id;
  IF version_document IS DISTINCT FROM NEW.document_id THEN
    RAISE EXCEPTION 'the version does not belong to the document'
      USING ERRCODE = 'check_violation';
  END IF;
  SELECT coalesce(max(a.version), 0) INTO latest_version FROM approved_records AS a
    WHERE a.document_id = NEW.document_id;
  IF NEW.version <> latest_version + 1 THEN
    RAISE EXCEPTION 'approved record version out of sequence' USING ERRCODE = 'unique_violation';
  END IF;
  SELECT coalesce(max(d.seq), 0) INTO latest_seq FROM review_decisions AS d
    WHERE d.run_id = NEW.run_id;
  IF NEW.decisions_through <> latest_seq THEN
    RAISE EXCEPTION 'the review decisions changed' USING ERRCODE = 'RV001';
  END IF;
  IF NEW.record_sha256 <> sha256(convert_to(NEW.record_json, 'UTF8')) THEN
    RAISE EXCEPTION 'record hash mismatch' USING ERRCODE = 'check_violation';
  END IF;
  body := NEW.record_json::jsonb;
  IF jsonb_typeof(body) IS DISTINCT FROM 'object'
     OR body->>'schema' IS DISTINCT FROM 'approved_record.v1'
     OR body->>'record_schema' IS DISTINCT FROM 'service_agreement.v1'
     OR body->'document'->>'id' IS DISTINCT FROM NEW.document_id::text
     OR body->'document'->>'version_id' IS DISTINCT FROM NEW.document_version_id::text
     OR body->'processing'->>'run_id' IS DISTINCT FROM NEW.run_id::text
     OR body->'approval'->>'version' IS DISTINCT FROM NEW.version::text
     OR body->'approval'->>'decisions_through' IS DISTINCT FROM NEW.decisions_through::text
     OR body->'approval'->>'approved_by' IS DISTINCT FROM NEW.approved_by::text
     OR (body->'approval'->>'approved_at')::timestamptz IS DISTINCT FROM NEW.approved_at
     OR jsonb_typeof(body->'fields') IS DISTINCT FROM 'object' THEN
    RAISE EXCEPTION 'the record does not describe this approval' USING ERRCODE = 'check_violation';
  END IF;
  IF (SELECT count(*) FROM jsonb_object_keys(body->'fields')) <> 7
     OR EXISTS (SELECT 1 FROM jsonb_object_keys(body->'fields') AS k(key)
                WHERE k.key NOT IN {FIELD_KEYS})
     OR EXISTS (
       SELECT 1 FROM jsonb_each(body->'fields') AS f(key, field)
       WHERE NOT CASE
         WHEN jsonb_typeof(f.field) <> 'object' THEN false
         WHEN f.field->>'status' = 'found' THEN
           jsonb_typeof(f.field->'value') IS NOT NULL
           AND jsonb_typeof(f.field->'value') <> 'null'
           AND jsonb_typeof(f.field->'evidence') = 'array'
           AND jsonb_array_length(f.field->'evidence') >= 1
         WHEN f.field->>'status' = 'unknown' THEN
           jsonb_typeof(f.field->'value') = 'null'
           AND jsonb_typeof(f.field->'reason') = 'string'
         ELSE false
       END) THEN
    RAISE EXCEPTION 'every approved field must be found with evidence or unknown'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END
$$
"""

IMMUTABLE_FUNCTION = """
CREATE FUNCTION review_rows_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = 'check_violation';
END
$$
"""


def upgrade() -> None:
    # The reviewer role (memberships and the operator's audit of role changes).
    op.drop_constraint(op.f("ck_memberships_role"), "memberships", type_="check")
    op.create_check_constraint(op.f("ck_memberships_role"), "memberships", f"role IN {ROLES_AFTER}")
    for column in ("old_role", "new_role"):
        name = op.f(f"ck_operator_audit_{column}")
        op.drop_constraint(name, "operator_audit", type_="check")
        op.create_check_constraint(
            name, "operator_audit", f"{column} IS NULL OR {column} IN {ROLES_AFTER}"
        )

    op.create_table(
        "review_decisions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("field_key", sa.Text(), nullable=False),
        sa.Column("resolution", sa.Text(), nullable=False),
        sa.Column("candidate_ordinal", sa.Integer(), nullable=True),
        sa.Column("decided_by", sa.Uuid(), nullable=False),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("seq >= 1", name=op.f("ck_review_decisions_seq_positive")),
        sa.CheckConstraint(
            "field_key IN " + FIELD_KEYS, name=op.f("ck_review_decisions_field_key")
        ),
        sa.CheckConstraint(
            "resolution IN ('candidate', 'unknown')", name=op.f("ck_review_decisions_resolution")
        ),
        sa.CheckConstraint(
            "(resolution = 'candidate') = (candidate_ordinal IS NOT NULL)",
            name=op.f("ck_review_decisions_candidate_iff_chosen"),
        ),
        sa.CheckConstraint(
            "candidate_ordinal >= 0", name=op.f("ck_review_decisions_candidate_ordinal")
        ),
        sa.ForeignKeyConstraint(
            ["decided_by"],
            ["users.id"],
            name=op.f("fk_review_decisions_decided_by_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "field_key"],
            ["extracted_fields.tenant_id", "extracted_fields.run_id", "extracted_fields.field_key"],
            name=op.f("fk_review_decisions_tenant_id_run_id_field_key_extracted_fields"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "field_key", "candidate_ordinal"],
            ["field_candidates.run_id", "field_candidates.field_key", "field_candidates.ordinal"],
            name=op.f("fk_review_decisions_run_id_field_key_candidate_ordinal_field_candidates"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_review_decisions")),
        sa.UniqueConstraint("run_id", "seq", name=op.f("uq_review_decisions_run_id_seq")),
    )
    op.create_table(
        "approved_records",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("decisions_through", sa.Integer(), nullable=False),
        sa.Column("record_json", sa.Text(), nullable=False),
        sa.Column("record_sha256", sa.LargeBinary(), nullable=False),
        sa.Column("approved_by", sa.Uuid(), nullable=False),
        sa.Column(
            "approved_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_approved_records_version_positive")),
        sa.CheckConstraint(
            "decisions_through >= 0", name=op.f("ck_approved_records_decisions_through")
        ),
        sa.CheckConstraint(
            "octet_length(record_sha256) = 32",
            name=op.f("ck_approved_records_record_sha256_length"),
        ),
        sa.CheckConstraint(
            f"length(record_json) BETWEEN 2 AND {APPROVED_RECORD_MAX_LENGTH}",
            name=op.f("ck_approved_records_record_json_length"),
        ),
        sa.ForeignKeyConstraint(
            ["approved_by"],
            ["users.id"],
            name=op.f("fk_approved_records_approved_by_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["documents.tenant_id", "documents.id"],
            name=op.f("fk_approved_records_tenant_id_document_id_documents"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_version_id"],
            ["document_versions.tenant_id", "document_versions.id"],
            name=op.f("fk_approved_records_tenant_id_document_version_id_document_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["processing_runs.tenant_id", "processing_runs.id"],
            name=op.f("fk_approved_records_tenant_id_run_id_processing_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approved_records")),
        sa.UniqueConstraint(
            "document_id", "version", name=op.f("uq_approved_records_document_id_version")
        ),
    )

    op.execute(DECISION_FUNCTION)
    op.execute(
        "CREATE TRIGGER review_decisions_check BEFORE INSERT ON review_decisions "
        "FOR EACH ROW EXECUTE FUNCTION review_decisions_check()"
    )
    op.execute(APPROVAL_FUNCTION)
    op.execute(
        "CREATE TRIGGER approved_records_check BEFORE INSERT ON approved_records "
        "FOR EACH ROW EXECUTE FUNCTION approved_records_check()"
    )
    op.execute(IMMUTABLE_FUNCTION)
    for table in TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION review_rows_immutable()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION review_rows_immutable()"
        )

    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_admin ON {table} FOR ALL TO app_owner "
            "USING (true) WITH CHECK (true)"
        )

    # app_runtime (the API, as a member; writes only as an owner or reviewer).
    op.execute(
        "CREATE POLICY review_decisions_member_read ON review_decisions FOR SELECT "
        f"TO app_runtime USING ({member_of_tenant('review_decisions')})"
    )
    op.execute(
        "CREATE POLICY review_decisions_reviewer_create ON review_decisions FOR INSERT "
        f"TO app_runtime WITH CHECK ({reviewer_of_tenant('review_decisions')} "
        f"AND decided_by = {CURRENT_USER_ID})"
    )
    op.execute("GRANT SELECT ON review_decisions TO app_runtime")
    op.execute(
        "GRANT INSERT (tenant_id, run_id, seq, field_key, resolution, candidate_ordinal, "
        "decided_by) ON review_decisions TO app_runtime"
    )
    clean = latest_clean("approved_records.tenant_id", "approved_records.document_version_id")
    op.execute(
        "CREATE POLICY approved_records_member_read ON approved_records FOR SELECT "
        f"TO app_runtime USING ({member_of_tenant('approved_records')} AND {clean})"
    )
    op.execute(
        "CREATE POLICY approved_records_reviewer_create ON approved_records FOR INSERT "
        f"TO app_runtime WITH CHECK ({reviewer_of_tenant('approved_records')} "
        f"AND approved_by = {CURRENT_USER_ID} AND {clean})"
    )
    op.execute("GRANT SELECT ON approved_records TO app_runtime")
    op.execute(
        "GRANT INSERT (tenant_id, document_id, version, run_id, document_version_id, "
        "decisions_through, record_json, record_sha256, approved_by) "
        "ON approved_records TO app_runtime"
    )


def downgrade() -> None:
    op.drop_table("approved_records")
    op.drop_table("review_decisions")
    op.execute("DROP FUNCTION review_rows_immutable()")
    op.execute("DROP FUNCTION approved_records_check()")
    op.execute("DROP FUNCTION review_decisions_check()")
    # Fails (and changes nothing) while any membership or audit row uses `reviewer`.
    for column in ("old_role", "new_role"):
        name = op.f(f"ck_operator_audit_{column}")
        op.drop_constraint(name, "operator_audit", type_="check")
        op.create_check_constraint(
            name, "operator_audit", f"{column} IS NULL OR {column} IN {ROLES_BEFORE}"
        )
    op.drop_constraint(op.f("ck_memberships_role"), "memberships", type_="check")
    op.create_check_constraint(
        op.f("ck_memberships_role"), "memberships", f"role IN {ROLES_BEFORE}"
    )
