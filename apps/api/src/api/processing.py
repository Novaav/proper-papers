"""DI-1 processing: configuration, pipeline version, and the member-side repository.

A processing run is one document version processed by one pipeline version:
`service_agreement.v1+<extractor>+parser@<parser image id>`. The image id
(`PARSER_IMAGE_ID`, a full `sha256:` id) names the exact parser artifact; the
worker verifies it and runs that image by id (api.worker). `(version,
pipeline version)` is unique, so starting processing is idempotent, and a
failed run is never re-queued: the same input and pipeline give the same
result. Only a new pipeline version processes again.

Functions here run inside `tenant_transaction` as the member (app_runtime);
row-level security (migration 0008) confines them to the member's tenant and
hides derived text (pages, candidates, evidence) unless the version's latest
malware verdict is clean. Nothing here logs or raises document text.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from api.documents import DocumentRecord, get_document
from api.extraction.contract import SCHEMA, ExtractionResult
from api.extraction.extract import EXTRACTOR_VERSION
from api.models import PROCESSING_MAX_ATTEMPTS

IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")


class ProcessingConfigError(ValueError):
    """Invalid processing configuration (names the setting, never a secret)."""


@dataclass(frozen=True)
class ProcessingSettings:
    """Worker timing (DI-1 decision P6). There is no heartbeat: the lease must outlast
    the longest possible processing (the sandbox's wall-clock limit plus its Docker calls)."""

    lease_seconds: int = 300
    # The delay before attempt 2 and before attempt 3 of a temporary failure.
    retry_delays_seconds: tuple[int, ...] = (30, 120)
    idle_seconds: float = 2.0

    def __post_init__(self) -> None:
        if not 60 <= self.lease_seconds <= 3600:
            raise ProcessingConfigError("PROCESSING_LEASE_SECONDS must be between 60 and 3600")
        if len(self.retry_delays_seconds) != PROCESSING_MAX_ATTEMPTS - 1 or not all(
            1 <= delay <= 3600 for delay in self.retry_delays_seconds
        ):
            raise ProcessingConfigError(
                f"PROCESSING_RETRY_DELAYS must be {PROCESSING_MAX_ATTEMPTS - 1} delays "
                "of 1 to 3600 seconds"
            )
        if list(self.retry_delays_seconds) != sorted(self.retry_delays_seconds):
            raise ProcessingConfigError("PROCESSING_RETRY_DELAYS must not decrease")
        if not 0.1 <= self.idle_seconds <= 60:
            raise ProcessingConfigError("PROCESSING_IDLE_SECONDS must be between 0.1 and 60")

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> ProcessingSettings:
        defaults = cls()
        try:
            lease = int(env.get("PROCESSING_LEASE_SECONDS", defaults.lease_seconds))
            delays_text = env.get("PROCESSING_RETRY_DELAYS")
            delays = (
                tuple(int(part) for part in delays_text.split(","))
                if delays_text
                else defaults.retry_delays_seconds
            )
            idle = float(env.get("PROCESSING_IDLE_SECONDS", defaults.idle_seconds))
        except ValueError:
            raise ProcessingConfigError(
                "PROCESSING_LEASE_SECONDS, PROCESSING_RETRY_DELAYS and PROCESSING_IDLE_SECONDS "
                "must be numbers"
            ) from None
        return cls(lease_seconds=lease, retry_delays_seconds=delays, idle_seconds=idle)


def parser_image_id_from_env(env: Mapping[str, str]) -> str | None:
    """`PARSER_IMAGE_ID`, or None if unset. A malformed value is a configuration error."""
    value = env.get("PARSER_IMAGE_ID", "").strip()
    if not value:
        return None
    if not IMAGE_ID.match(value):
        raise ProcessingConfigError("PARSER_IMAGE_ID must be a full image id (sha256:<64 hex>)")
    return value


def pipeline_version(parser_image_id: str) -> str:
    if not IMAGE_ID.match(parser_image_id):
        raise ProcessingConfigError("the parser image id must be sha256:<64 hex>")
    return f"{SCHEMA}+{EXTRACTOR_VERSION}+parser@{parser_image_id}"


class VersionNotClean(Exception):
    """The document's version is not readable (never scanned, or blocked by a later verdict)."""

    def __init__(self, scan_status: str) -> None:
        super().__init__(scan_status)
        self.scan_status = scan_status


@dataclass(frozen=True, slots=True)
class RunRecord:
    id: UUID
    status: str
    failure_code: str | None
    attempts: int
    pipeline_version: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


_RUN_COLUMNS = (
    "id, status, failure_code, attempts, pipeline_version, created_at, started_at, finished_at"
)


def start_processing(
    session: Session,
    workspace_id: UUID,
    document_id: UUID,
    *,
    pipeline: str,
    user_id: UUID,
) -> tuple[RunRecord, bool] | None:
    """Queue the document's version for `pipeline`: (run, created), or None if not visible.

    Idempotent: an existing run for the same version and pipeline is returned
    as it is, whatever its status (a failed run is not re-queued). Concurrent
    calls create one run. Raises VersionNotClean unless the latest verdict is clean.
    """
    record = _clean_document(session, workspace_id, document_id)
    if record is None:
        return None
    inserted = session.execute(
        text(
            "INSERT INTO processing_runs (tenant_id, version_id, pipeline_version, created_by) "
            "SELECT tenant_id, id, :pipeline, :user FROM document_versions WHERE id = :version "
            "ON CONFLICT (version_id, pipeline_version) DO NOTHING RETURNING id"
        ),
        {"pipeline": pipeline, "user": user_id, "version": record.version_id},
    ).scalar_one_or_none()
    run = session.execute(
        text(
            f"SELECT {_RUN_COLUMNS} FROM processing_runs "
            "WHERE version_id = :version AND pipeline_version = :pipeline"
        ),
        {"version": record.version_id, "pipeline": pipeline},
    ).one()
    return RunRecord(*run), inserted is not None


def latest_processing(
    session: Session, workspace_id: UUID, document_id: UUID
) -> tuple[RunRecord | None, ExtractionResult | None] | None:
    """The newest run of the document's version and, once it has one, its result.

    None if the document is not visible. Raises VersionNotClean unless the
    latest verdict is clean (derived text is then unreadable, like the bytes).
    """
    record = _clean_document(session, workspace_id, document_id)
    if record is None:
        return None
    row = session.execute(
        text(
            f"SELECT {_RUN_COLUMNS} FROM processing_runs WHERE version_id = :version "
            "ORDER BY created_at DESC, id DESC LIMIT 1"
        ),
        {"version": record.version_id},
    ).one_or_none()
    if row is None:
        return None, None
    run = RunRecord(*row)
    if run.status not in ("completed", "needs_review"):
        return run, None
    return run, _result(session, run)


def _clean_document(
    session: Session, workspace_id: UUID, document_id: UUID
) -> DocumentRecord | None:
    record = get_document(session, workspace_id, document_id)
    if record is not None and record.scan_status != "clean":
        raise VersionNotClean(record.scan_status)
    return record


def _result(session: Session, run: RunRecord) -> ExtractionResult:
    fields: dict[str, dict[str, Any]] = {
        key: {"status": status, "reason": reason, "candidates": []}
        for key, status, reason in session.execute(
            text("SELECT field_key, status, reason FROM extracted_fields WHERE run_id = :run"),
            {"run": run.id},
        )
    }
    candidates: dict[UUID, dict[str, Any]] = {}
    for candidate_id, key, value, raw in session.execute(
        text(
            "SELECT id, field_key, value, raw FROM field_candidates WHERE run_id = :run "
            "ORDER BY field_key, ordinal"
        ),
        {"run": run.id},
    ):
        candidate = {"value": value, "raw": raw, "evidence": []}
        candidates[candidate_id] = candidate
        fields[key]["candidates"].append(candidate)
    for candidate_id, page, start, end, quote in session.execute(
        text(
            "SELECT candidate_id, page_number, char_start, char_end, quote "
            "FROM candidate_evidence WHERE run_id = :run ORDER BY candidate_id, ordinal"
        ),
        {"run": run.id},
    ):
        candidates[candidate_id]["evidence"].append(
            {"page": page, "start": start, "end": end, "quote": quote}
        )
    # The stored result must still satisfy the contract (types, consistency, outcome).
    return ExtractionResult.model_validate(
        {
            "schema": SCHEMA,
            "extractor": run.pipeline_version.split("+")[1],
            "outcome": run.status,
            "fields": fields,
        }
    )
