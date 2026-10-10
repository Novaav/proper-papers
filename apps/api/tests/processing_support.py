"""Shared setup for the DI-1 processing tests (disposable test database only)."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import Connection, Engine, text
from sqlalchemy.orm import Session, sessionmaker

from api.database import tenant_transaction
from api.documents import CleanScan, DocumentRecord
from api.processing import pipeline_version, start_processing

if TYPE_CHECKING:
    from tests.conftest import SeededTenant

PDF = b"%PDF-1.7\n% synthetic test document\n%%EOF\n"
IMAGE = "sha256:" + "a" * 64
PIPELINE = pipeline_version(IMAGE)
CLEAN = CleanScan(
    engine="ClamAV",
    engine_version="1.4.6",
    signature_version="27777",
    signature_date=datetime(2026, 9, 30, tzinfo=UTC),
)


@dataclass(frozen=True)
class Seeded:
    tenant: SeededTenant
    document: DocumentRecord

    @property
    def tenant_id(self) -> UUID:
        return self.tenant.ctx.tenant_id


def start(sessions: sessionmaker[Session], seeded: Seeded, pipeline: str = PIPELINE) -> UUID:
    with tenant_transaction(sessions, seeded.tenant.ctx) as session:
        started = start_processing(
            session,
            seeded.tenant.workspace_id,
            seeded.document.id,
            pipeline=pipeline,
            user_id=seeded.tenant.ctx.user_id,
        )
    assert started is not None
    return started[0].id


def block(owner_engine: Engine, seeded: Seeded, verdict: str = "infected") -> None:
    block_version(owner_engine, seeded.document.version_id, seeded.tenant.ctx.user_id, verdict)


def block_version(
    owner_engine: Engine, version_id: UUID, user_id: UUID, verdict: str = "infected"
) -> None:
    """Record a later, non-clean verdict (as the schema owner: the runtime cannot)."""
    with owner_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO document_scans (tenant_id, version_id, scan_number, verdict, engine, "
                "engine_version, signature_version, signature_date, created_by) "
                "SELECT tenant_id, version_id, max(scan_number) + 1, :verdict, 'ClamAV', "
                "'1.4.6', '27778', now(), :user FROM document_scans "
                "WHERE version_id = :version GROUP BY tenant_id, version_id"
            ),
            {"verdict": verdict, "user": user_id, "version": version_id},
        )


def run_row(owner_engine: Engine, run_id: UUID) -> dict[str, Any]:
    with owner_engine.connect() as connection:
        row = connection.execute(
            text("SELECT * FROM processing_runs WHERE id = :id"), {"id": run_id}
        ).mappings()
        return dict(row.one())


def as_worker(
    connection: Connection,
    *,
    worker: UUID | None = None,
    run: UUID | None = None,
    tenant: UUID | None = None,
) -> None:
    connection.execute(
        text(
            "SELECT set_config('app.worker_id', :w, true), set_config('app.run_id', :r, true), "
            "set_config('app.tenant_id', :t, true)"
        ),
        {"w": str(worker or ""), "r": str(run or ""), "t": str(tenant or "")},
    )
