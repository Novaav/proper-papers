"""The DI-1 processing worker: claims queued runs, parses in the sandbox, extracts, stores.

    python -m api.worker [--once]        (make api-worker)

Settings: DATABASE_URL_WORKER (the app_worker role, and only that role),
PARSER_IMAGE_ID (the full id of the parser image; the worker refuses to start
unless Docker has exactly that image, and runs it by id), PARSER_DOCKER, and
the timing in api.processing.ProcessingSettings.

One run at a time:
1. Claim (one short transaction): the oldest queued run of this pipeline version
   whose retry delay has passed, or a running run whose lease has expired
   (another attempt); `FOR UPDATE SKIP LOCKED`, so concurrent workers never take
   the same run. Attempts are limited to 3 (the database enforces it); a run
   whose lease expired on its last attempt fails as `worker_lost`.
2. Read the version's bytes and hash through row-level security: only the
   claimed run's version, only while it holds the lease, only if the latest
   malware verdict is clean. No bytes: `not_clean`; a hash mismatch:
   `integrity_mismatch`.
3. Parse in the isolated sandbox (never in this process), extract with the
   deterministic rules, and require exact evidence.
4. Finish (one transaction, only if the run is still running under this
   worker's lease): insert pages, fields, candidates and evidence, and set the
   final status. A late or duplicate worker writes nothing. Only
   `sandbox_unavailable` is retried (after the configured delays); every other
   failure is final.

Nothing about a document is logged: only run ids, statuses and failure codes.
"""

import argparse
import hashlib
import json
import logging
import os
import signal
import sys
import threading
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, event, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from api.database import (
    DatabaseRoleError,
    DatabaseSettings,
    create_runtime_engine,
    create_session_factory,
)
from api.extraction.contract import ExtractionResult, evidence_violations
from api.extraction.extract import Page, extract
from api.models import PROCESSING_MAX_ATTEMPTS
from api.processing import (
    ProcessingConfigError,
    ProcessingSettings,
    parser_image_id_from_env,
    pipeline_version,
)
from api.sandbox import (
    DOCKER_TIMEOUT_SECONDS,
    ParseFailed,
    ParseOutcome,
    ParseSucceeded,
    SandboxSettings,
    image_id,
    parse_in_sandbox,
)

logger = logging.getLogger(__name__)

WORKER_ROLE = "app_worker"
TEMPORARY_FAILURES = frozenset({"sandbox_unavailable"})

Parse = Callable[[bytes, SandboxSettings], ParseOutcome]
Extract = Callable[[Iterable[Page]], ExtractionResult]


@dataclass(frozen=True)
class Claim:
    run_id: UUID
    tenant_id: UUID
    version_id: UUID
    lease: UUID
    attempts: int


def create_worker_engine(settings: DatabaseSettings) -> Engine:
    """An engine that connects only as app_worker (the runtime guard also applies)."""
    engine = create_runtime_engine(settings, pool_size=1, max_overflow=0)

    @event.listens_for(engine, "connect")
    def _require_worker_role(dbapi_connection: Any, _: Any) -> None:
        with dbapi_connection.cursor() as cursor:
            cursor.execute("SELECT current_user, session_user")
            roles = cursor.fetchone()
        dbapi_connection.rollback()
        if tuple(roles) != (WORKER_ROLE, WORKER_ROLE):
            raise DatabaseRoleError(f"the worker must connect as {WORKER_ROLE}")

    return engine


class Worker:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        sandbox: SandboxSettings,
        pipeline: str,
        settings: ProcessingSettings,
        parse: Parse = parse_in_sandbox,
        extract: Extract = extract,
    ) -> None:
        # No heartbeat: the lease must outlast the sandbox's worst case.
        worst_case = sandbox.wall_seconds + 3 * DOCKER_TIMEOUT_SECONDS
        if settings.lease_seconds <= worst_case:
            raise ProcessingConfigError(
                f"PROCESSING_LEASE_SECONDS must exceed {worst_case:g} "
                "(the sandbox wall clock plus its Docker calls)"
            )
        self._sessions = sessions
        self._sandbox = sandbox
        self._pipeline = pipeline
        self._settings = settings
        self._parse = parse
        self._extract = extract

    def run_once(self) -> bool:
        """Process at most one run; False if there was nothing to claim."""
        claim = self.claim()
        if claim is None:
            return False
        self.process(claim)
        return True

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                worked = self.run_once()
            except SQLAlchemyError as error:
                logger.warning("worker: database error: %s", type(error).__name__)
                worked = False
            if not worked:
                stop.wait(self._settings.idle_seconds)

    # ---------------------------------------------------------------- claim

    def claim(self) -> Claim | None:
        while True:
            lease = uuid.uuid7()
            with self._sessions() as session, session.begin():
                _set(session, worker=lease)
                row = session.execute(
                    text(
                        "SELECT id, tenant_id, version_id, status, attempts FROM processing_runs "
                        "WHERE pipeline_version = :pipeline AND ("
                        "(status = 'queued' AND not_before <= now()) OR "
                        "(status = 'running' AND lease_expires_at < now())) "
                        "ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED"
                    ),
                    {"pipeline": self._pipeline},
                ).one_or_none()
                if row is None:
                    return None
                run_id, tenant_id, version_id, status, attempts = row
                if status == "running" and attempts >= PROCESSING_MAX_ATTEMPTS:
                    # Its last attempt's worker was lost: take the lease only to fail it.
                    session.execute(
                        text(
                            "UPDATE processing_runs SET status = 'failed', "
                            "failure_code = 'worker_lost', finished_at = now() WHERE id = :id"
                        ),
                        {"id": run_id},
                    )
                    logger.info("worker: run %s failed: worker_lost", run_id)
                    continue
                session.execute(
                    text(
                        "UPDATE processing_runs SET status = 'running', attempts = attempts + 1, "
                        "lease_owner = :lease, "
                        "lease_expires_at = now() + make_interval(secs => :seconds), "
                        "started_at = coalesce(started_at, now()) WHERE id = :id"
                    ),
                    {"id": run_id, "lease": lease, "seconds": self._settings.lease_seconds},
                )
            logger.info("worker: claimed run %s (attempt %d)", run_id, attempts + 1)
            return Claim(run_id, tenant_id, version_id, lease, attempts + 1)

    # ---------------------------------------------------------------- process

    def process(self, claim: Claim) -> str:
        """Process a claimed run; returns what happened (a status, "retry" or "lost")."""
        content = self._read(claim)
        if isinstance(content, str):
            return self._fail(claim, content)
        try:
            outcome = self._parse(content, self._sandbox)
        except Exception as error:
            # parse_in_sandbox reports every document problem as a code; an exception
            # here is an infrastructure fault: treated as a temporary unavailability.
            logger.warning("worker: run %s: sandbox error: %s", claim.run_id, type(error).__name__)
            outcome = ParseFailed("sandbox_unavailable")
        if isinstance(outcome, ParseFailed):
            if outcome.code in TEMPORARY_FAILURES:
                return self._retry(claim, outcome.code)
            return self._fail(claim, outcome.code)
        pages = {page.number: page.text for page in outcome.pages}
        try:
            result = self._extract(Page(number, page) for number, page in pages.items())
            if evidence_violations(result, pages):
                raise ValueError("evidence does not match the page text")
        except Exception as error:
            logger.warning(
                "worker: run %s: extraction error: %s", claim.run_id, type(error).__name__
            )
            return self._fail(claim, "extraction_failed")
        return self._complete(claim, outcome, result)

    def _read(self, claim: Claim) -> bytes | str:
        with self._sessions() as session, session.begin():
            _set(session, worker=claim.lease, run=claim.run_id, tenant=claim.tenant_id)
            row = session.execute(
                text(
                    "SELECT c.content, v.sha256 FROM document_contents AS c "
                    "JOIN document_versions AS v ON v.tenant_id = c.tenant_id "
                    "AND v.id = c.version_id WHERE c.version_id = :version"
                ),
                {"version": claim.version_id},
            ).one_or_none()
        if row is None:
            return "not_clean"
        content, digest = bytes(row[0]), bytes(row[1])
        if hashlib.sha256(content).digest() != digest:
            return "integrity_mismatch"
        return content

    # ---------------------------------------------------------------- finish

    def _complete(self, claim: Claim, outcome: ParseSucceeded, result: ExtractionResult) -> str:
        status = result.outcome

        def write(session: Session) -> None:
            session.execute(
                text(
                    "INSERT INTO document_pages (run_id, tenant_id, page_number, text, "
                    "text_sha256, off_page_chars, invisible_chars, undefined_chars, "
                    "control_chars_removed) VALUES (:run, :tenant, :number, :text, :digest, "
                    ":off_page, :invisible, :undefined, :removed)"
                ),
                [
                    {
                        **self._ids(claim),
                        "number": page.number,
                        "text": page.text,
                        "digest": hashlib.sha256(page.text.encode("utf-8")).digest(),
                        "off_page": page.off_page_chars,
                        "invisible": page.invisible_chars,
                        "undefined": page.undefined_chars,
                        "removed": page.control_chars_removed,
                    }
                    for page in outcome.pages
                ],
            )
            session.execute(
                text(
                    "INSERT INTO extracted_fields (run_id, tenant_id, field_key, status, reason) "
                    "VALUES (:run, :tenant, :key, :status, :reason)"
                ),
                [
                    {**self._ids(claim), "key": key, "status": f.status, "reason": f.reason}
                    for key, f in result.fields.items()
                ],
            )
            candidates: list[dict[str, Any]] = []
            evidence: list[dict[str, Any]] = []
            for key, field in result.fields.items():
                for ordinal, candidate in enumerate(field.candidates):
                    candidate_id = uuid.uuid7()
                    value = candidate.model_dump(mode="json")["value"]
                    candidates.append(
                        {
                            **self._ids(claim),
                            "id": candidate_id,
                            "key": key,
                            "ordinal": ordinal,
                            "value": None if value is None else json.dumps(value),
                            "raw": candidate.raw,
                        }
                    )
                    evidence.extend(
                        {
                            **self._ids(claim),
                            "candidate": candidate_id,
                            "ordinal": index,
                            "page": span.page,
                            "start": span.start,
                            "end": span.end,
                            "quote": span.quote,
                        }
                        for index, span in enumerate(candidate.evidence)
                    )
            if candidates:
                session.execute(
                    text(
                        "INSERT INTO field_candidates (id, run_id, tenant_id, field_key, ordinal, "
                        "value, raw) VALUES (:id, :run, :tenant, :key, :ordinal, "
                        "CAST(:value AS jsonb), :raw)"
                    ),
                    candidates,
                )
                session.execute(
                    text(
                        "INSERT INTO candidate_evidence (candidate_id, ordinal, run_id, tenant_id, "
                        "page_number, char_start, char_end, quote) VALUES (:candidate, :ordinal, "
                        ":run, :tenant, :page, :start, :end, :quote)"
                    ),
                    evidence,
                )
            session.execute(
                text(
                    "UPDATE processing_runs SET status = :status, finished_at = now(), "
                    "parser_versions = CAST(:versions AS jsonb), "
                    "parser_features = CAST(:features AS jsonb) WHERE id = :run"
                ),
                {
                    "run": claim.run_id,
                    "status": status,
                    "versions": json.dumps(outcome.versions.model_dump(by_alias=True)),
                    "features": json.dumps(outcome.features.model_dump()),
                },
            )

        return self._finish(claim, write, status)

    def _fail(self, claim: Claim, code: str) -> str:
        def write(session: Session) -> None:
            session.execute(
                text(
                    "UPDATE processing_runs SET status = 'failed', failure_code = :code, "
                    "finished_at = now() WHERE id = :run"
                ),
                {"run": claim.run_id, "code": code},
            )

        return self._finish(claim, write, "failed", code)

    def _retry(self, claim: Claim, code: str) -> str:
        if claim.attempts >= PROCESSING_MAX_ATTEMPTS:
            return self._fail(claim, code)
        delay = self._settings.retry_delays_seconds[claim.attempts - 1]

        def write(session: Session) -> None:
            session.execute(
                text(
                    "UPDATE processing_runs SET status = 'queued', lease_owner = NULL, "
                    "lease_expires_at = NULL, not_before = now() + make_interval(secs => :delay) "
                    "WHERE id = :run"
                ),
                {"run": claim.run_id, "delay": delay},
            )

        return self._finish(claim, write, "retry", code)

    def _finish(
        self, claim: Claim, write: Callable[[Session], None], status: str, code: str = ""
    ) -> str:
        """Write only if the run is still running under this worker's lease (compare-and-set)."""
        try:
            with self._sessions() as session, session.begin():
                _set(session, worker=claim.lease, run=claim.run_id, tenant=claim.tenant_id)
                held = session.execute(
                    text(
                        "SELECT 1 FROM processing_runs WHERE id = :run AND status = 'running' "
                        "AND lease_owner = :lease FOR UPDATE"
                    ),
                    {"run": claim.run_id, "lease": claim.lease},
                ).one_or_none()
                if held is None:
                    logger.warning("worker: run %s: lease lost; nothing written", claim.run_id)
                    return "lost"
                write(session)
        except DBAPIError as error:
            # Nothing was written; the lease expires and the run is claimed again.
            logger.warning(
                "worker: run %s: could not finish: %s", claim.run_id, type(error).__name__
            )
            return "lost"
        logger.info("worker: run %s: %s%s", claim.run_id, status, f" ({code})" if code else "")
        return status

    @staticmethod
    def _ids(claim: Claim) -> dict[str, UUID]:
        return {"run": claim.run_id, "tenant": claim.tenant_id}


def _set(
    session: Session,
    *,
    worker: UUID,
    run: UUID | None = None,
    tenant: UUID | None = None,
) -> None:
    session.execute(
        text(
            "SELECT set_config('app.worker_id', :worker, true), "
            "set_config('app.run_id', :run, true), set_config('app.tenant_id', :tenant, true)"
        ),
        {"worker": str(worker), "run": str(run or ""), "tenant": str(tenant or "")},
    )


def build_worker(env: Mapping[str, str]) -> tuple[Worker, Engine]:
    """The worker from the environment; refuses to start on any configuration problem."""
    image = parser_image_id_from_env(env)
    if image is None:
        raise ProcessingConfigError("PARSER_IMAGE_ID is required")
    sandbox = SandboxSettings(image=image, docker=env.get("PARSER_DOCKER", "docker"))
    actual = image_id(sandbox)
    if actual != image:
        raise ProcessingConfigError("Docker does not have the parser image PARSER_IMAGE_ID names")
    url = env.get("DATABASE_URL_WORKER", "")
    if not url:
        raise ProcessingConfigError("DATABASE_URL_WORKER is required")
    engine = create_worker_engine(DatabaseSettings.from_env({"DATABASE_URL": url}))
    worker = Worker(
        create_session_factory(engine),
        sandbox=sandbox,
        pipeline=pipeline_version(image),
        settings=ProcessingSettings.from_env(env),
    )
    return worker, engine


def main(argv: list[str] | None = None, env: Mapping[str, str] = os.environ) -> int:
    parser = argparse.ArgumentParser(prog="python -m api.worker")
    parser.add_argument("--once", action="store_true", help="process at most one run")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        worker, engine = build_worker(env)
    except ProcessingConfigError as error:
        print(f"worker: {error}", file=sys.stderr)
        return 2
    try:
        if args.once:
            worker.run_once()
            return 0
        stop = threading.Event()
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: stop.set())
        worker.run_forever(stop)
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
