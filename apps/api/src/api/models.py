"""Relational schema. Migrations in `migrations/` must match these definitions (`alembic check`).

Tables live in schema `app` through the roles' search_path (see db/database.sql).
Row-level security and grants are defined in the migrations, not here.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    MetaData,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "pk": "pk_%(table_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
}

NAME_LENGTH_CHECK = "length(btrim(name)) BETWEEN 1 AND 200"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Tenant(Base):
    __tablename__ = "tenants"
    __table_args__ = (CheckConstraint(NAME_LENGTH_CHECK, name="name_length"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    name: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        CheckConstraint(NAME_LENGTH_CHECK, name="name_length"),
        # Lets future child tables reference (tenant_id, workspace_id) so a
        # child row can never point at another tenant's workspace.
        UniqueConstraint("tenant_id", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"))
    name: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# Identity key limits: issuer is the IdP's issuer URL, subject its opaque user id.
ISSUER_MAX_LENGTH = 512
SUBJECT_MAX_LENGTH = 255
MEMBERSHIP_ROLES = ("owner", "reviewer", "member")
# Roles that may record review decisions and approve records (R1). A reviewer is
# otherwise a member; owner is the tenant's administrative role (operator CLI).
REVIEW_ROLES = ("owner", "reviewer")


class User(Base):
    """A person, identified only by (issuer, subject) from a verified token.

    No email or name is stored (D-A5).
    """

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(f"length(issuer) BETWEEN 1 AND {ISSUER_MAX_LENGTH}", name="issuer_length"),
        CheckConstraint(
            f"length(subject) BETWEEN 1 AND {SUBJECT_MAX_LENGTH}", name="subject_length"
        ),
        UniqueConstraint("issuer", "subject"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    issuer: Mapped[str] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Membership(Base):
    """A user's role in a tenant. Created by the owner role only (no runtime writes)."""

    __tablename__ = "memberships"
    __table_args__ = (
        CheckConstraint(
            "role IN (" + ", ".join(f"'{role}'" for role in MEMBERSHIP_ROLES) + ")", name="role"
        ),
        Index(None, "user_id"),
    )

    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True
    )
    role: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


OPERATOR_ACTIONS = (
    "tenant_created",
    "membership_granted",
    "membership_role_changed",
    "membership_revoked",
)
REQUEST_ID_MAX_LENGTH = 200
REASON_MAX_LENGTH = 500
_ROLE_VALUES = ", ".join(f"'{role}'" for role in MEMBERSHIP_ROLES)


class OperatorAudit(Base):
    """One row per change made by the operator CLI, written in the same transaction.

    Append-only: app_operator may only insert and read; id, occurred_at and
    db_role are set by the database. No foreign keys, so the history outlives
    the rows it describes.
    """

    __tablename__ = "operator_audit"
    __table_args__ = (
        CheckConstraint(
            "action IN (" + ", ".join(f"'{a}'" for a in OPERATOR_ACTIONS) + ")", name="action"
        ),
        CheckConstraint(f"old_role IS NULL OR old_role IN ({_ROLE_VALUES})", name="old_role"),
        CheckConstraint(f"new_role IS NULL OR new_role IN ({_ROLE_VALUES})", name="new_role"),
        CheckConstraint(
            "(action = 'tenant_created' AND old_role IS NULL AND new_role = 'owner')"
            " OR (action = 'membership_granted' AND old_role IS NULL AND new_role IS NOT NULL)"
            " OR (action = 'membership_role_changed' AND old_role IS NOT NULL"
            " AND new_role IS NOT NULL AND old_role <> new_role)"
            " OR (action = 'membership_revoked' AND old_role IS NOT NULL AND new_role IS NULL)",
            name="roles_match_action",
        ),
        CheckConstraint(
            "(action = 'tenant_created') = (request_id IS NOT NULL)", name="request_id_for_create"
        ),
        CheckConstraint(
            f"length(request_id) BETWEEN 1 AND {REQUEST_ID_MAX_LENGTH}", name="request_id_length"
        ),
        CheckConstraint(
            f"length(btrim(reason)) BETWEEN 1 AND {REASON_MAX_LENGTH}", name="reason_length"
        ),
        UniqueConstraint("request_id"),
        Index(None, "tenant_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    db_role: Mapped[str] = mapped_column(Text, server_default=text("SESSION_USER"))
    action: Mapped[str] = mapped_column(Text)
    tenant_id: Mapped[UUID]
    user_id: Mapped[UUID]
    old_role: Mapped[str | None] = mapped_column(Text)
    new_role: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)


# Documents (B1). Uploaded files are untrusted bytes: stored and returned, never
# parsed or rendered. A document's content is immutable: a version row and its
# bytes are only ever inserted, never updated or deleted by the runtime role.
DOCUMENT_MAX_BYTES = 20 * 1024 * 1024
DOCUMENT_NAME_MAX_LENGTH = 255
DOCUMENT_MEDIA_TYPES = ("application/pdf",)


class Document(Base):
    """A named document in a workspace; its bytes live in versions."""

    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(
            f"length(display_name) BETWEEN 1 AND {DOCUMENT_NAME_MAX_LENGTH}",
            name="display_name_length",
        ),
        # The workspace must belong to the same tenant.
        ForeignKeyConstraint(
            ["tenant_id", "workspace_id"],
            ["workspaces.tenant_id", "workspaces.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("tenant_id", "id"),
        Index(None, "tenant_id", "workspace_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    tenant_id: Mapped[UUID]
    workspace_id: Mapped[UUID]
    display_name: Mapped[str] = mapped_column(Text)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DocumentVersion(Base):
    """The immutable record of one upload: type, size and SHA-256 of the bytes."""

    __tablename__ = "document_versions"
    __table_args__ = (
        CheckConstraint("version_number >= 1", name="version_number_positive"),
        CheckConstraint(
            "media_type IN (" + ", ".join(f"'{t}'" for t in DOCUMENT_MEDIA_TYPES) + ")",
            name="media_type",
        ),
        CheckConstraint(f"size_bytes BETWEEN 1 AND {DOCUMENT_MAX_BYTES}", name="size_bytes"),
        CheckConstraint("octet_length(sha256) = 32", name="sha256_length"),
        ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["documents.tenant_id", "documents.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("document_id", "version_number"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    tenant_id: Mapped[UUID]
    document_id: Mapped[UUID]
    version_number: Mapped[int]
    media_type: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int]
    sha256: Mapped[bytes] = mapped_column(LargeBinary)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DocumentContent(Base):
    """The bytes of one document version (kept apart so metadata queries never read them)."""

    __tablename__ = "document_contents"
    __table_args__ = (
        CheckConstraint(
            f"octet_length(content) BETWEEN 1 AND {DOCUMENT_MAX_BYTES}", name="content_length"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "version_id"],
            ["document_versions.tenant_id", "document_versions.id"],
            ondelete="RESTRICT",
        ),
    )

    version_id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]
    content: Mapped[bytes] = mapped_column(LargeBinary)


# Malware scans of document versions. Append-only: verdicts are ordered per
# version by scan_number, and the latest one decides whether the bytes may be
# read (row-level security on document_contents, migration 0007). A version is
# only ever inserted together with scan 1 = clean (a deferred constraint
# trigger enforces it at commit).
SCAN_VERDICTS = ("clean", "infected", "incomplete", "encrypted", "error")
SCAN_TEXT_MAX_LENGTH = 100


class DocumentScan(Base):
    """One scan verdict for a document version, with the engine and signatures used."""

    __tablename__ = "document_scans"
    __table_args__ = (
        CheckConstraint("scan_number >= 1", name="scan_number_positive"),
        CheckConstraint(
            "verdict IN (" + ", ".join(f"'{v}'" for v in SCAN_VERDICTS) + ")", name="verdict"
        ),
        CheckConstraint(
            f"length(engine) BETWEEN 1 AND {SCAN_TEXT_MAX_LENGTH}", name="engine_length"
        ),
        CheckConstraint(
            f"length(engine_version) BETWEEN 1 AND {SCAN_TEXT_MAX_LENGTH}",
            name="engine_version_length",
        ),
        CheckConstraint(
            f"length(signature_version) BETWEEN 1 AND {SCAN_TEXT_MAX_LENGTH}",
            name="signature_version_length",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "version_id"],
            ["document_versions.tenant_id", "document_versions.id"],
            ondelete="RESTRICT",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(primary_key=True)
    version_id: Mapped[UUID] = mapped_column(primary_key=True)
    scan_number: Mapped[int] = mapped_column(primary_key=True)
    verdict: Mapped[str] = mapped_column(Text)
    engine: Mapped[str] = mapped_column(Text)
    engine_version: Mapped[str] = mapped_column(Text)
    signature_version: Mapped[str] = mapped_column(Text)
    signature_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# DI-1 processing (migration 0008). A run processes one document version with one
# pipeline version (extractor + exact parser image id); the worker claims queued
# runs with a lease and writes the results once, in the transaction that ends the
# run. Result tables are append-only; row-level security and the state-transition
# and completeness triggers are defined in the migration.
PROCESSING_STATUSES = ("queued", "running", "completed", "needs_review", "failed")
PROCESSING_FINAL_STATUSES = ("completed", "needs_review", "failed")
PROCESSING_FAILURE_CODES = (
    # From the parser sandbox (deterministic).
    "input_too_large",
    "parse_failed",
    "encrypted_pdf",
    "embedded_content",
    "too_many_pages",
    "text_too_large",
    "no_text_layer",
    "memory_exceeded",
    "timeout",
    "output_too_large",
    "output_invalid",
    # From the worker (deterministic).
    "not_clean",
    "integrity_mismatch",
    "extraction_failed",
    # Temporary: retried until the attempts run out.
    "sandbox_unavailable",
    "worker_lost",
)
PROCESSING_MAX_ATTEMPTS = 3
PIPELINE_VERSION_MAX_LENGTH = 200
PAGE_MAX_NUMBER = 100
PAGE_TEXT_MAX_LENGTH = 200_000
FIELD_KEYS = (
    "provider_name",
    "customer_name",
    "effective_date",
    "end_date",
    "governing_law",
    "contract_value",
    "notice_period_days",
)
FIELD_STATUSES = ("found", "unknown", "conflict")
FIELD_REASONS = ("not_found", "unparseable", "ambiguous")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


class ProcessingRun(Base):
    """One processing of a document version by one pipeline version, and its state."""

    __tablename__ = "processing_runs"
    __table_args__ = (
        CheckConstraint(
            f"length(pipeline_version) BETWEEN 1 AND {PIPELINE_VERSION_MAX_LENGTH}",
            name="pipeline_version_length",
        ),
        CheckConstraint(_in("status", PROCESSING_STATUSES), name="status"),
        CheckConstraint(
            "failure_code IS NULL OR " + _in("failure_code", PROCESSING_FAILURE_CODES),
            name="failure_code",
        ),
        CheckConstraint("(status = 'failed') = (failure_code IS NOT NULL)", name="failed_has_code"),
        CheckConstraint(f"attempts BETWEEN 0 AND {PROCESSING_MAX_ATTEMPTS}", name="attempts"),
        CheckConstraint(
            "status <> 'running' OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="running_has_lease",
        ),
        CheckConstraint(
            f"({_in('status', PROCESSING_FINAL_STATUSES)}) = (finished_at IS NOT NULL)",
            name="final_has_finish",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "version_id"],
            ["document_versions.tenant_id", "document_versions.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("version_id", "pipeline_version"),
        Index(
            "ix_processing_runs_claimable",
            "pipeline_version",
            "created_at",
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    tenant_id: Mapped[UUID]
    version_id: Mapped[UUID]
    pipeline_version: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'queued'"))
    failure_code: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(server_default=text("0"))
    lease_owner: Mapped[UUID | None]
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    not_before: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    parser_versions: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    parser_features: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DocumentPage(Base):
    """The parser's normalized text of one page (derived document text: RLS as the bytes)."""

    __tablename__ = "document_pages"
    __table_args__ = (
        CheckConstraint(f"page_number BETWEEN 1 AND {PAGE_MAX_NUMBER}", name="page_number"),
        CheckConstraint(f"length(text) <= {PAGE_TEXT_MAX_LENGTH}", name="text_length"),
        CheckConstraint("octet_length(text_sha256) = 32", name="text_sha256_length"),
        CheckConstraint(
            "off_page_chars >= 0 AND invisible_chars >= 0 AND undefined_chars >= 0 "
            "AND control_chars_removed >= 0",
            name="counts",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["processing_runs.tenant_id", "processing_runs.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("tenant_id", "run_id", "page_number"),
    )

    run_id: Mapped[UUID] = mapped_column(primary_key=True)
    page_number: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]
    text: Mapped[str] = mapped_column(Text)
    text_sha256: Mapped[bytes] = mapped_column(LargeBinary)
    off_page_chars: Mapped[int]
    invisible_chars: Mapped[int]
    undefined_chars: Mapped[int]
    control_chars_removed: Mapped[int]


class ExtractedField(Base):
    """One field of the service_agreement.v1 record: its status and, if unknown, why."""

    __tablename__ = "extracted_fields"
    __table_args__ = (
        CheckConstraint(_in("field_key", FIELD_KEYS), name="field_key"),
        CheckConstraint(_in("status", FIELD_STATUSES), name="status"),
        CheckConstraint("reason IS NULL OR " + _in("reason", FIELD_REASONS), name="reason"),
        CheckConstraint("(status = 'unknown') = (reason IS NOT NULL)", name="unknown_has_reason"),
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["processing_runs.tenant_id", "processing_runs.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("tenant_id", "run_id", "field_key"),
    )

    run_id: Mapped[UUID] = mapped_column(primary_key=True)
    field_key: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[UUID]
    status: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)


class FieldCandidate(Base):
    """A candidate value of a field (value null when unparseable or ambiguous)."""

    __tablename__ = "field_candidates"
    __table_args__ = (
        CheckConstraint("ordinal >= 0", name="ordinal"),
        CheckConstraint(f"length(raw) BETWEEN 1 AND {PAGE_TEXT_MAX_LENGTH}", name="raw_length"),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "field_key"],
            [
                "extracted_fields.tenant_id",
                "extracted_fields.run_id",
                "extracted_fields.field_key",
            ],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("run_id", "field_key", "ordinal"),
        UniqueConstraint("tenant_id", "run_id", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]
    run_id: Mapped[UUID]
    field_key: Mapped[str] = mapped_column(Text)
    ordinal: Mapped[int]
    value: Mapped[Any | None] = mapped_column(JSONB)
    raw: Mapped[str] = mapped_column(Text)


class CandidateEvidence(Base):
    """One exact evidence span of a candidate: page, character range and quote."""

    __tablename__ = "candidate_evidence"
    __table_args__ = (
        CheckConstraint("ordinal >= 0", name="ordinal"),
        CheckConstraint("char_start >= 0 AND char_end > char_start", name="range"),
        CheckConstraint("char_end - char_start = length(quote)", name="quote_length"),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "candidate_id"],
            ["field_candidates.tenant_id", "field_candidates.run_id", "field_candidates.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "page_number"],
            [
                "document_pages.tenant_id",
                "document_pages.run_id",
                "document_pages.page_number",
            ],
            ondelete="RESTRICT",
        ),
    )

    candidate_id: Mapped[UUID] = mapped_column(primary_key=True)
    ordinal: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]
    run_id: Mapped[UUID]
    page_number: Mapped[int]
    char_start: Mapped[int]
    char_end: Mapped[int]
    quote: Mapped[str] = mapped_column(Text)


# R1 review (migration 0009). Both tables are append-only for every role (a
# trigger refuses UPDATE, DELETE and TRUNCATE). A decision resolves one field of
# a finished run: the chosen candidate (by its ordinal) or UNKNOWN, never free
# text. An approval freezes the run and the decisions up to `decisions_through`
# into an immutable `approved_record.v1` document, stored as its canonical JSON
# text with its SHA-256. Validation, sequencing and row-level security are
# defined in the migration.
REVIEW_RESOLUTIONS = ("candidate", "unknown")
APPROVED_RECORD_MAX_LENGTH = 2_000_000


class ReviewDecision(Base):
    """One reviewer decision on one field of a run (append-only, numbered per run)."""

    __tablename__ = "review_decisions"
    __table_args__ = (
        CheckConstraint("seq >= 1", name="seq_positive"),
        CheckConstraint(_in("field_key", FIELD_KEYS), name="field_key"),
        CheckConstraint(_in("resolution", REVIEW_RESOLUTIONS), name="resolution"),
        CheckConstraint(
            "(resolution = 'candidate') = (candidate_ordinal IS NOT NULL)",
            name="candidate_iff_chosen",
        ),
        CheckConstraint("candidate_ordinal >= 0", name="candidate_ordinal"),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "field_key"],
            [
                "extracted_fields.tenant_id",
                "extracted_fields.run_id",
                "extracted_fields.field_key",
            ],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["run_id", "field_key", "candidate_ordinal"],
            ["field_candidates.run_id", "field_candidates.field_key", "field_candidates.ordinal"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("run_id", "seq"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    tenant_id: Mapped[UUID]
    run_id: Mapped[UUID]
    seq: Mapped[int]
    field_key: Mapped[str] = mapped_column(Text)
    resolution: Mapped[str] = mapped_column(Text)
    candidate_ordinal: Mapped[int | None]
    decided_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ApprovedRecord(Base):
    """One immutable approved version of a document's record (numbered per document)."""

    __tablename__ = "approved_records"
    __table_args__ = (
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("decisions_through >= 0", name="decisions_through"),
        CheckConstraint("octet_length(record_sha256) = 32", name="record_sha256_length"),
        CheckConstraint(
            f"length(record_json) BETWEEN 2 AND {APPROVED_RECORD_MAX_LENGTH}",
            name="record_json_length",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["documents.tenant_id", "documents.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "document_version_id"],
            ["document_versions.tenant_id", "document_versions.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["processing_runs.tenant_id", "processing_runs.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("document_id", "version"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    tenant_id: Mapped[UUID]
    document_id: Mapped[UUID]
    version: Mapped[int]
    run_id: Mapped[UUID]
    document_version_id: Mapped[UUID]
    decisions_through: Mapped[int]
    record_json: Mapped[str] = mapped_column(Text)
    record_sha256: Mapped[bytes] = mapped_column(LargeBinary)
    approved_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
