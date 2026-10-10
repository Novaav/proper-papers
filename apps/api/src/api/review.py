"""R1 review: decisions, approvals and the `approved_record.v1` export.

A reviewer (membership role owner or reviewer) resolves fields of the
document's newest run once it completed or needs review:
- a found or conflicting field can be resolved to one of its candidates that
  has a valid value (addressed by its ordinal, the candidate's position in the
  processing result), keeping that candidate's evidence;
- any field can be resolved to UNKNOWN.
There is no free text. Decisions are numbered per run (`seq`); every write
names the number the reviewer last saw (`expected_seq`), so a reviewer never
overwrites a change they have not seen.

Blocking fields are the ones a run marks for review: every conflict and every
required field that is not found. A record can be approved once every blocking
field has a decision; the other fields are approved as extracted
(`approved_as_extracted`). An approval is an immutable, numbered version of the
document's record, built here from the stored rows only (never from the
request) and stored as canonical JSON with its SHA-256; the export returns
exactly those bytes.

Functions run inside `tenant_transaction` as the member (app_runtime); row-level
security and the triggers of migration 0009 enforce the same rules again
(reviewer role, sequence numbers, record structure, append-only). Nothing here
logs document text.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from api.documents import DocumentRecord
from api.extraction.contract import (
    FIELDS,
    REQUIRED,
    SCHEMA,
    Evidence,
    FieldKey,
    FieldResult,
    Reason,
    Status,
    Value,
)
from api.processing import RunRecord, _clean_document, _result

RECORD_SCHEMA = "approved_record.v1"
SUCCESSFUL = ("completed", "needs_review")

Resolution = Literal["candidate", "unknown"]
ApprovedReason = Literal["not_found", "unparseable", "ambiguous", "rejected_by_reviewer"]


class ReviewRefused(Exception):
    """A review write that cannot be applied; `code` is the API's fixed detail."""

    def __init__(
        self,
        code: Literal["stale", "run_changed", "invalid_decision", "unresolved_fields"],
        fields: tuple[str, ...] = (),
    ) -> None:
        super().__init__(code)
        self.code = code
        self.fields = fields


# ---- approved_record.v1 (the export; the committed JSON Schema is generated from it)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class DecisionRef(_Strict):
    seq: Annotated[int, Field(ge=1)]
    decided_by: UUID
    decided_at: datetime


class ExtractedState(_Strict):
    status: Status
    reason: Reason | None


class ApprovedField(_Strict):
    status: Literal["found", "unknown"]
    value: Value | None
    reason: ApprovedReason | None
    basis: Literal["reviewer_decision", "approved_as_extracted"]
    decision: DecisionRef | None
    extracted: ExtractedState
    evidence: list[Evidence]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.status == "found":
            ok = self.value is not None and self.reason is None and len(self.evidence) >= 1
        else:
            ok = self.value is None and self.reason is not None and not self.evidence
        if not ok or (self.basis == "reviewer_decision") != (self.decision is not None):
            raise ValueError("inconsistent approved field")
        return self


class ApprovedDocument(_Strict):
    id: UUID
    version_id: UUID
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    name: str


class ApprovedProcessing(_Strict):
    run_id: UUID
    pipeline_version: str
    extractor: str
    finished_at: datetime


class Approval(_Strict):
    version: Annotated[int, Field(ge=1)]
    approved_by: UUID
    approved_at: datetime
    decisions_through: Annotated[int, Field(ge=0)]


class ApprovedRecordV1(_Strict):
    """The approved record of one document version, with each value's provenance."""

    schema_: Literal["approved_record.v1"] = Field(alias="schema")
    record_schema: Literal["service_agreement.v1"]
    document: ApprovedDocument
    processing: ApprovedProcessing
    approval: Approval
    fields: dict[FieldKey, ApprovedField]

    @model_validator(mode="after")
    def _complete(self) -> Self:
        if set(self.fields) != set(FIELDS):
            raise ValueError("every field must be present exactly once")
        return self


def canonical_json(record: ApprovedRecordV1) -> str:
    """The stored and exported form: sorted keys, no whitespace, UTF-8."""
    data = record.model_dump(mode="json", by_alias=True)
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def record_json_schema() -> dict[str, object]:
    return ApprovedRecordV1.model_json_schema(by_alias=True)


# ---- repository


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    seq: int
    field_key: str
    resolution: str
    candidate_ordinal: int | None
    decided_by: UUID
    decided_at: datetime


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    version: int
    run_id: UUID
    decisions_through: int
    approved_by: UUID
    approved_at: datetime
    record_sha256: bytes


@dataclass(frozen=True, slots=True)
class ReviewState:
    # The run that can be reviewed: the newest run, once it completed or needs review.
    run_id: UUID | None
    decisions: list[DecisionRecord]
    approvals: list[ApprovalRecord]

    @property
    def decisions_through(self) -> int:
        return self.decisions[-1].seq if self.decisions else 0


_DECISION_COLUMNS = "seq, field_key, resolution, candidate_ordinal, decided_by, decided_at"
_APPROVAL_COLUMNS = "version, run_id, decisions_through, approved_by, approved_at, record_sha256"


def review_state(session: Session, workspace_id: UUID, document_id: UUID) -> ReviewState | None:
    """The document's reviewable run, its decisions and all approvals; None if not visible.

    Raises VersionNotClean unless the latest verdict is clean.
    """
    document = _clean_document(session, workspace_id, document_id)
    if document is None:
        return None
    run = _reviewable_run(session, document)
    decisions = [] if run is None else _decisions(session, run.id)
    return ReviewState(
        run_id=None if run is None else run.id,
        decisions=decisions,
        approvals=_approvals(session, document.id),
    )


def record_decision(
    session: Session,
    workspace_id: UUID,
    document_id: UUID,
    *,
    tenant_id: UUID,
    user_id: UUID,
    run_id: UUID,
    expected_seq: int,
    field_key: FieldKey,
    resolution: Resolution,
    candidate_ordinal: int | None,
) -> DecisionRecord | None:
    """Record one decision as the next of the run; None if the document is not visible.

    The caller has checked the reviewer role (row-level security checks it again).
    """
    document = _clean_document(session, workspace_id, document_id)
    if document is None:
        return None
    run = _current_run(session, document, run_id)
    if _latest_seq(session, run.id) != expected_seq:
        raise ReviewRefused("stale")
    field = _result(session, run).fields[field_key]
    if resolution == "candidate" and not _choosable(field, candidate_ordinal):
        raise ReviewRefused("invalid_decision")
    if resolution == "unknown" and candidate_ordinal is not None:
        raise ReviewRefused("invalid_decision")
    row = session.execute(
        text(
            "INSERT INTO review_decisions (tenant_id, run_id, seq, field_key, resolution, "
            "candidate_ordinal, decided_by) VALUES (:tenant, :run, :seq, :key, :resolution, "
            f":ordinal, :user) RETURNING {_DECISION_COLUMNS}"
        ),
        {
            "tenant": tenant_id,
            "run": run.id,
            "seq": expected_seq + 1,
            "key": field_key,
            "resolution": resolution,
            "ordinal": candidate_ordinal,
            "user": user_id,
        },
    ).one()
    return DecisionRecord(*row)


def approve(
    session: Session,
    workspace_id: UUID,
    document_id: UUID,
    *,
    tenant_id: UUID,
    user_id: UUID,
    run_id: UUID,
    expected_seq: int,
    expected_version: int,
) -> ApprovalRecord | None:
    """Freeze the run and its decisions up to `expected_seq` as the next approved version.

    None if the document is not visible. Refused when the run is no longer the
    reviewable one, when decisions or approvals changed since the reviewer saw
    them, or while a blocking field has no decision. The caller has checked the
    reviewer role (row-level security checks it again).
    """
    document = _clean_document(session, workspace_id, document_id)
    if document is None:
        return None
    run = _current_run(session, document, run_id)
    decisions = _decisions(session, run.id)
    if (decisions[-1].seq if decisions else 0) != expected_seq:
        raise ReviewRefused("stale")
    approvals = _approvals(session, document.id)
    if (approvals[-1].version if approvals else 0) != expected_version:
        raise ReviewRefused("stale")
    result = _result(session, run)
    latest = {decision.field_key: decision for decision in decisions}
    unresolved = tuple(
        key for key in FIELDS if _blocking(key, result.fields[key]) and key not in latest
    )
    if unresolved:
        raise ReviewRefused("unresolved_fields", unresolved)
    # The transaction's timestamp: the same value the database stores as approved_at.
    approved_at: datetime = session.execute(text("SELECT now()")).scalar_one()
    assert run.finished_at is not None  # a run that completed or needs review has finished
    record = ApprovedRecordV1.model_validate(
        {
            "schema": RECORD_SCHEMA,
            "record_schema": SCHEMA,
            "document": {
                "id": document.id,
                "version_id": document.version_id,
                "sha256": document.sha256.hex(),
                "name": document.name,
            },
            "processing": {
                "run_id": run.id,
                "pipeline_version": run.pipeline_version,
                "extractor": result.extractor,
                "finished_at": run.finished_at,
            },
            "approval": {
                "version": expected_version + 1,
                "approved_by": user_id,
                "approved_at": approved_at,
                "decisions_through": expected_seq,
            },
            "fields": {key: _approved_field(result.fields[key], latest.get(key)) for key in FIELDS},
        },
        strict=False,
    )
    body = canonical_json(record)
    row = session.execute(
        text(
            "INSERT INTO approved_records (tenant_id, document_id, version, run_id, "
            "document_version_id, decisions_through, record_json, record_sha256, approved_by) "
            "VALUES (:tenant, :document, :version, :run, :doc_version, :through, :body, "
            f":digest, :user) RETURNING {_APPROVAL_COLUMNS}"
        ),
        {
            "tenant": tenant_id,
            "document": document.id,
            "version": expected_version + 1,
            "run": run.id,
            "doc_version": document.version_id,
            "through": expected_seq,
            "body": body,
            "digest": hashlib.sha256(body.encode()).digest(),
            "user": user_id,
        },
    ).one()
    return ApprovalRecord(*row)


@dataclass(frozen=True, slots=True)
class ExportedRecord:
    name: str
    version: int
    body: bytes
    sha256: bytes


class StoredRecordInvalid(Exception):
    """A stored record no longer matches its hash or schema; it is never served."""


def export_record(
    session: Session, workspace_id: UUID, document_id: UUID, version: int
) -> ExportedRecord | None:
    """The stored bytes of one approved version; None if the document or version is not visible."""
    document = _clean_document(session, workspace_id, document_id)
    if document is None:
        return None
    row = session.execute(
        text(
            "SELECT record_json, record_sha256 FROM approved_records "
            "WHERE document_id = :document AND version = :version"
        ),
        {"document": document.id, "version": version},
    ).one_or_none()
    if row is None:
        return None
    body = row.record_json.encode()
    if hashlib.sha256(body).digest() != row.record_sha256:
        raise StoredRecordInvalid("hash")
    try:
        ApprovedRecordV1.model_validate_json(body)
    except ValueError:
        raise StoredRecordInvalid("schema") from None
    return ExportedRecord(name=document.name, version=version, body=body, sha256=row.record_sha256)


def _reviewable_run(session: Session, document: DocumentRecord) -> RunRecord | None:
    row = session.execute(
        text(
            "SELECT id, status, failure_code, attempts, pipeline_version, created_at, "
            "started_at, finished_at FROM processing_runs WHERE version_id = :version "
            "ORDER BY created_at DESC, id DESC LIMIT 1"
        ),
        {"version": document.version_id},
    ).one_or_none()
    if row is None:
        return None
    run = RunRecord(*row)
    return run if run.status in SUCCESSFUL else None


def _current_run(session: Session, document: DocumentRecord, run_id: UUID) -> RunRecord:
    run = _reviewable_run(session, document)
    if run is None or run.id != run_id:
        raise ReviewRefused("run_changed")
    return run


def _latest_seq(session: Session, run_id: UUID) -> int:
    latest: int = session.execute(
        text("SELECT coalesce(max(seq), 0) FROM review_decisions WHERE run_id = :run"),
        {"run": run_id},
    ).scalar_one()
    return latest


def _decisions(session: Session, run_id: UUID) -> list[DecisionRecord]:
    rows = session.execute(
        text(f"SELECT {_DECISION_COLUMNS} FROM review_decisions WHERE run_id = :run ORDER BY seq"),
        {"run": run_id},
    )
    return [DecisionRecord(*row) for row in rows]


def _approvals(session: Session, document_id: UUID) -> list[ApprovalRecord]:
    rows = session.execute(
        text(
            f"SELECT {_APPROVAL_COLUMNS} FROM approved_records WHERE document_id = :document "
            "ORDER BY version"
        ),
        {"document": document_id},
    )
    return [ApprovalRecord(*row) for row in rows]


def _blocking(key: FieldKey, field: FieldResult) -> bool:
    """The rows a run marks for review: every conflict, every required field not found."""
    return field.status == "conflict" or (key in REQUIRED and field.status != "found")


def _choosable(field: FieldResult, ordinal: int | None) -> bool:
    return (
        field.status in ("found", "conflict")
        and ordinal is not None
        and 0 <= ordinal < len(field.candidates)
        and field.candidates[ordinal].value is not None
    )


def _approved_field(field: FieldResult, decision: DecisionRecord | None) -> dict[str, object]:
    extracted: dict[str, object] = {"status": field.status, "reason": field.reason}
    ref = (
        None
        if decision is None
        else {
            "seq": decision.seq,
            "decided_by": decision.decided_by,
            "decided_at": decision.decided_at,
        }
    )
    basis = "approved_as_extracted" if decision is None else "reviewer_decision"
    if decision is None:
        # Only non-blocking fields reach approval without a decision: found, or optional unknown.
        if field.status == "found":
            return _found(field, 0, basis, ref, extracted)
        return _unknown(field.reason, basis, ref, extracted)
    if decision.resolution == "candidate":
        assert decision.candidate_ordinal is not None
        return _found(field, decision.candidate_ordinal, basis, ref, extracted)
    reason = field.reason if field.status == "unknown" else "rejected_by_reviewer"
    return _unknown(reason, basis, ref, extracted)


def _found(
    field: FieldResult,
    ordinal: int,
    basis: str,
    ref: dict[str, object] | None,
    extracted: dict[str, object],
) -> dict[str, object]:
    candidate = field.candidates[ordinal]
    return {
        "status": "found",
        "value": candidate.value,
        "reason": None,
        "basis": basis,
        "decision": ref,
        "extracted": extracted,
        "evidence": [span.model_dump() for span in candidate.evidence],
    }


def _unknown(
    reason: str | None, basis: str, ref: dict[str, object] | None, extracted: dict[str, object]
) -> dict[str, object]:
    return {
        "status": "unknown",
        "value": None,
        "reason": reason,
        "basis": basis,
        "decision": ref,
        "extracted": extracted,
        "evidence": [],
    }
