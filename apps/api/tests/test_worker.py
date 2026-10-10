"""The DI-1 worker: claiming, leases, retries, failures, results (fake sandbox; real database).

The real sandbox end to end is tests/test_processing_e2e.py.
"""

import json
import logging
import time
import uuid
from collections.abc import Callable, Iterable
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from api.database import DatabaseRoleError, DatabaseSettings, tenant_transaction
from api.extraction.contract import ExtractionResult
from api.extraction.extract import Page, extract
from api.processing import (
    ProcessingConfigError,
    ProcessingSettings,
    parser_image_id_from_env,
    pipeline_version,
    start_processing,
)
from api.sandbox import ParseFailed, ParseOutcome, SandboxSettings, _interpret
from api.worker import Worker, build_worker, create_worker_engine
from tests.processing_support import IMAGE, PIPELINE, Seeded, as_worker, block, run_row, start

TEXT = (
    "SERVICES AGREEMENT\n\nProvider: Northwind Analytics Ltd\n"
    "Customer: Harbour Freight Services Ltd\nEffective Date: 1 March 2026"
)
CONFLICT = TEXT + "\n\nThis Agreement is effective as of 1 April 2026."
SETTINGS = ProcessingSettings(retry_delays_seconds=(1, 1))
SANDBOX = SandboxSettings(image=IMAGE, docker="/nonexistent/docker")


def parsed(*pages: str) -> ParseOutcome:
    """A sandbox result, through the runner's own validation."""
    return _interpret(
        json.dumps(
            {
                "schema": "docparser.output.v1",
                "ok": True,
                "versions": {"docparser": "0.1.0", "pdfminer.six": "20260107", "normalizer": "1"},
                "page_count": len(pages),
                "pages": [
                    {
                        "number": n,
                        "text": page,
                        "off_page_chars": 0,
                        "invisible_chars": 0,
                        "undefined_chars": 0,
                        "control_chars_removed": 0,
                    }
                    for n, page in enumerate(pages, start=1)
                ],
                "features": {"javascript": False, "xfa": False},
                "metrics": {"cpu_seconds": 0.1, "max_rss_kib": 1},
            }
        ).encode()
    )


def make_worker(
    sessions: sessionmaker[Session],
    parse: Callable[[bytes, SandboxSettings], ParseOutcome] | ParseOutcome,
    *,
    pipeline: str,
    extract_fn: Callable[[Iterable[Page]], ExtractionResult] = extract,
) -> Worker:
    parse_fn = parse if callable(parse) else (lambda _content, _settings: parse)
    return Worker(
        sessions,
        sandbox=SANDBOX,
        pipeline=pipeline,
        settings=SETTINGS,
        parse=parse_fn,
        extract=extract_fn,
    )


@pytest.fixture
def pipeline() -> str:
    """A pipeline version of this test only: runs left by other tests are never claimed."""
    return pipeline_version("sha256:" + uuid.uuid4().hex * 2)


@pytest.fixture
def queued(
    seed_document: Callable[..., Seeded], sessions: sessionmaker[Session], pipeline: str
) -> tuple[Seeded, UUID]:
    seeded = seed_document()
    return seeded, start(sessions, seeded, pipeline)


def _results(owner_engine: Engine, run_id: UUID) -> dict[str, Any]:
    with owner_engine.connect() as c:
        pages = {
            n: t
            for n, t in c.execute(
                text("SELECT page_number, text FROM document_pages WHERE run_id = :r"),
                {"r": run_id},
            )
        }
        fields: dict[str, str] = dict(
            c.execute(
                text("SELECT field_key, status FROM extracted_fields WHERE run_id = :r"),
                {"r": run_id},
            ).all()
        )
        evidence = c.execute(
            text(
                "SELECT e.page_number, e.char_start, e.char_end, e.quote, f.raw, f.value "
                "FROM candidate_evidence e JOIN field_candidates f ON f.id = e.candidate_id "
                "WHERE e.run_id = :r"
            ),
            {"r": run_id},
        ).all()
    return {"pages": pages, "fields": fields, "evidence": evidence}


# ---------------------------------------------------------------- configuration


def test_processing_settings_defaults_and_validation() -> None:
    assert ProcessingSettings() == ProcessingSettings(300, (30, 120), 2.0)
    assert ProcessingSettings.from_env({}) == ProcessingSettings()
    assert ProcessingSettings.from_env(
        {"PROCESSING_LEASE_SECONDS": "600", "PROCESSING_RETRY_DELAYS": "5,10"}
    ) == ProcessingSettings(600, (5, 10), 2.0)
    for env in (
        {"PROCESSING_LEASE_SECONDS": "30"},
        {"PROCESSING_LEASE_SECONDS": "x"},
        {"PROCESSING_RETRY_DELAYS": "30"},
        {"PROCESSING_RETRY_DELAYS": "30,120,300"},
        {"PROCESSING_RETRY_DELAYS": "120,30"},
        {"PROCESSING_RETRY_DELAYS": "0,30"},
        {"PROCESSING_IDLE_SECONDS": "0"},
    ):
        with pytest.raises(ProcessingConfigError):
            ProcessingSettings.from_env(env)


def test_the_lease_must_outlast_the_sandbox(sessions: sessionmaker[Session]) -> None:
    with pytest.raises(ProcessingConfigError):
        Worker(sessions, sandbox=SANDBOX, pipeline=PIPELINE, settings=ProcessingSettings(150))


def test_pipeline_version_names_the_exact_parser_image() -> None:
    assert f"service_agreement.v1+rules-1+parser@{IMAGE}" == PIPELINE
    assert parser_image_id_from_env({}) is None
    assert parser_image_id_from_env({"PARSER_IMAGE_ID": IMAGE}) == IMAGE
    for bad in ("production-ai-flagship-parser:local", "sha256:abc", IMAGE.upper(), IMAGE + "0"):
        with pytest.raises(ProcessingConfigError):
            parser_image_id_from_env({"PARSER_IMAGE_ID": bad})
        with pytest.raises(ProcessingConfigError):
            pipeline_version(bad)


def test_the_worker_refuses_to_start_without_a_verified_image(
    worker_settings: DatabaseSettings,
) -> None:
    url = worker_settings.url.render_as_string(hide_password=False)
    for env, message in (
        ({"DATABASE_URL_WORKER": url}, "PARSER_IMAGE_ID is required"),
        (
            {"DATABASE_URL_WORKER": url, "PARSER_IMAGE_ID": IMAGE, "PARSER_DOCKER": "/nonexistent"},
            "Docker does not have the parser image",
        ),
    ):
        with pytest.raises(ProcessingConfigError, match=message):
            build_worker(env)


def test_the_worker_engine_accepts_only_app_worker(runtime_settings: DatabaseSettings) -> None:
    engine = create_worker_engine(runtime_settings)
    try:
        with pytest.raises(DatabaseRoleError), engine.connect():
            pass
    finally:
        engine.dispose()


# ---------------------------------------------------------------- start and claim


def test_starting_is_idempotent(
    queued: tuple[Seeded, UUID], sessions: sessionmaker[Session], pipeline: str
) -> None:
    seeded, run_id = queued
    with tenant_transaction(sessions, seeded.tenant.ctx) as session:
        again = start_processing(
            session,
            seeded.tenant.workspace_id,
            seeded.document.id,
            pipeline=pipeline,
            user_id=seeded.tenant.ctx.user_id,
        )
    assert again is not None and again[0].id == run_id and again[1] is False


def test_claims_only_its_own_pipeline_version(
    queued: tuple[Seeded, UUID],
    worker_sessions: sessionmaker[Session],
    pipeline: str,
) -> None:
    other = make_worker(
        worker_sessions, parsed(TEXT), pipeline=pipeline_version("sha256:" + "b" * 64)
    )
    assert other.claim() is None
    claim = make_worker(worker_sessions, parsed(TEXT), pipeline=pipeline).claim()
    assert claim is not None and claim.run_id == queued[1] and claim.attempts == 1


def test_concurrent_workers_never_claim_the_same_run(
    seed_document: Callable[..., Seeded],
    sessions: sessionmaker[Session],
    worker_settings: DatabaseSettings,
    worker_sessions: sessionmaker[Session],
    pipeline: str,
) -> None:
    first, second = (start(sessions, seed_document(), pipeline) for _ in range(2))
    other_engine = create_worker_engine(worker_settings)
    try:
        with other_engine.begin() as held:
            as_worker(held, worker=uuid.uuid7())
            locked: UUID = held.execute(
                text(
                    "SELECT id FROM processing_runs WHERE id = ANY(:ids) "
                    "ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED"
                ),
                {"ids": [first, second]},
            ).scalar_one()
            claim = make_worker(worker_sessions, parsed(TEXT), pipeline=pipeline).claim()
            assert claim is not None and claim.run_id != locked
    finally:
        other_engine.dispose()


def _expire(owner_engine: Engine, run_id: UUID, attempts: int) -> None:
    with owner_engine.begin() as connection:
        for attempt in range(attempts):
            if attempt:
                connection.execute(
                    text(
                        "UPDATE processing_runs SET status = 'queued', lease_owner = NULL, "
                        "lease_expires_at = NULL WHERE id = :id"
                    ),
                    {"id": run_id},
                )
            connection.execute(
                text(
                    "UPDATE processing_runs SET status = 'running', attempts = attempts + 1, "
                    "lease_owner = gen_random_uuid(), "
                    "lease_expires_at = now() - interval '1 second' WHERE id = :id"
                ),
                {"id": run_id},
            )


def test_an_expired_lease_is_taken_over_as_another_attempt(
    queued: tuple[Seeded, UUID],
    owner_engine: Engine,
    worker_sessions: sessionmaker[Session],
    pipeline: str,
) -> None:
    _expire(owner_engine, queued[1], attempts=1)
    claim = make_worker(worker_sessions, parsed(TEXT), pipeline=pipeline).claim()
    assert claim is not None and claim.run_id == queued[1] and claim.attempts == 2


def test_a_run_lost_on_its_last_attempt_fails_as_worker_lost(
    queued: tuple[Seeded, UUID],
    owner_engine: Engine,
    worker_sessions: sessionmaker[Session],
    pipeline: str,
) -> None:
    _expire(owner_engine, queued[1], attempts=3)
    assert make_worker(worker_sessions, parsed(TEXT), pipeline=pipeline).claim() is None
    row = run_row(owner_engine, queued[1])
    assert (row["status"], row["failure_code"], row["attempts"]) == ("failed", "worker_lost", 3)


# ---------------------------------------------------------------- processing


@pytest.mark.parametrize(("page", "status"), [(TEXT, "completed"), (CONFLICT, "needs_review")])
def test_results_are_stored_with_exact_evidence(
    queued: tuple[Seeded, UUID],
    owner_engine: Engine,
    worker_sessions: sessionmaker[Session],
    page: str,
    status: str,
    pipeline: str,
) -> None:
    worker = make_worker(worker_sessions, parsed(page), pipeline=pipeline)
    assert worker.run_once()
    row = run_row(owner_engine, queued[1])
    assert (row["status"], row["failure_code"], row["attempts"]) == (status, None, 1)
    assert row["parser_versions"]["pdfminer.six"] == "20260107"
    stored = _results(owner_engine, queued[1])
    assert stored["pages"] == {1: page}
    assert len(stored["fields"]) == 7
    assert stored["evidence"]
    for page_number, start_at, end, quote, raw, _value in stored["evidence"]:
        assert stored["pages"][page_number][start_at:end] == quote
        assert raw in quote


def test_a_deterministic_failure_is_final_at_once(
    queued: tuple[Seeded, UUID],
    owner_engine: Engine,
    worker_sessions: sessionmaker[Session],
    pipeline: str,
) -> None:
    make_worker(worker_sessions, ParseFailed("embedded_content"), pipeline=pipeline).run_once()
    row = run_row(owner_engine, queued[1])
    assert (row["status"], row["failure_code"], row["attempts"]) == (
        "failed",
        "embedded_content",
        1,
    )
    assert _results(owner_engine, queued[1])["fields"] == {}


def test_an_unavailable_sandbox_is_retried_with_the_configured_delays_then_fails(
    queued: tuple[Seeded, UUID],
    owner_engine: Engine,
    worker_sessions: sessionmaker[Session],
    pipeline: str,
) -> None:
    worker = make_worker(worker_sessions, ParseFailed("sandbox_unavailable"), pipeline=pipeline)
    for attempt in (1, 2):
        assert worker.run_once()
        row = run_row(owner_engine, queued[1])
        assert (row["status"], row["attempts"], row["lease_owner"]) == ("queued", attempt, None)
        assert worker.claim() is None  # the delay has not passed
        time.sleep(1.1)
    assert worker.run_once()
    row = run_row(owner_engine, queued[1])
    assert (row["status"], row["failure_code"], row["attempts"]) == (
        "failed",
        "sandbox_unavailable",
        3,
    )


def test_an_exception_from_the_sandbox_runner_is_a_temporary_failure(
    queued: tuple[Seeded, UUID],
    owner_engine: Engine,
    worker_sessions: sessionmaker[Session],
    pipeline: str,
) -> None:
    def broken(_content: bytes, _settings: SandboxSettings) -> ParseOutcome:
        raise RuntimeError("Northwind Analytics Ltd")

    make_worker(worker_sessions, broken, pipeline=pipeline).run_once()
    assert run_row(owner_engine, queued[1])["status"] == "queued"


@pytest.mark.parametrize("problem", ["raises", "bad_evidence"])
def test_extraction_problems_fail_the_run(
    queued: tuple[Seeded, UUID],
    owner_engine: Engine,
    worker_sessions: sessionmaker[Session],
    problem: str,
    pipeline: str,
) -> None:
    def extract_fn(pages: Iterable[Page]) -> ExtractionResult:
        if problem == "raises":
            raise ValueError("Northwind Analytics Ltd")
        # Evidence from a different text than the page's.
        return extract([Page(1, TEXT.replace("Northwind", "Southwind"))])

    make_worker(worker_sessions, parsed(TEXT), extract_fn=extract_fn, pipeline=pipeline).run_once()
    row = run_row(owner_engine, queued[1])
    assert (row["status"], row["failure_code"]) == ("failed", "extraction_failed")


def test_a_version_blocked_after_queueing_fails_as_not_clean(
    queued: tuple[Seeded, UUID],
    owner_engine: Engine,
    worker_sessions: sessionmaker[Session],
    pipeline: str,
) -> None:
    block(owner_engine, queued[0])
    make_worker(worker_sessions, parsed(TEXT), pipeline=pipeline).run_once()
    row = run_row(owner_engine, queued[1])
    assert (row["status"], row["failure_code"]) == ("failed", "not_clean")


def test_bytes_that_do_not_match_their_hash_fail_as_integrity_mismatch(
    queued: tuple[Seeded, UUID],
    owner_engine: Engine,
    worker_sessions: sessionmaker[Session],
    pipeline: str,
) -> None:
    with owner_engine.begin() as connection:
        connection.execute(
            text("UPDATE document_contents SET content = :c WHERE version_id = :v"),
            {"c": b"%PDF-1.7\n% altered\n", "v": queued[0].document.version_id},
        )
    make_worker(worker_sessions, parsed(TEXT), pipeline=pipeline).run_once()
    row = run_row(owner_engine, queued[1])
    assert (row["status"], row["failure_code"]) == ("failed", "integrity_mismatch")


def test_a_worker_that_lost_its_run_writes_nothing(
    queued: tuple[Seeded, UUID],
    owner_engine: Engine,
    worker_sessions: sessionmaker[Session],
    pipeline: str,
) -> None:
    worker = make_worker(worker_sessions, parsed(TEXT), pipeline=pipeline)
    claim = worker.claim()
    assert claim is not None
    with owner_engine.begin() as connection:  # someone else ended the run meanwhile
        connection.execute(
            text(
                "UPDATE processing_runs SET status = 'failed', failure_code = 'timeout', "
                "finished_at = now() WHERE id = :id"
            ),
            {"id": claim.run_id},
        )
    assert worker.process(claim) == "lost"
    assert run_row(owner_engine, claim.run_id)["failure_code"] == "timeout"
    assert _results(owner_engine, claim.run_id)["fields"] == {}


def test_no_document_text_in_the_logs(
    seed_document: Callable[..., Seeded],
    sessions: sessionmaker[Session],
    worker_sessions: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
    pipeline: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    for outcome in (parsed(TEXT), parsed(CONFLICT), ParseFailed("parse_failed")):
        start(sessions, seed_document(), pipeline)
        make_worker(worker_sessions, outcome, pipeline=pipeline).run_once()

    def broken(_content: bytes, _settings: SandboxSettings) -> ParseOutcome:
        raise RuntimeError("Northwind Analytics Ltd")

    start(sessions, seed_document(), pipeline)
    make_worker(worker_sessions, broken, pipeline=pipeline).run_once()
    assert caplog.records
    for word in ("Northwind", "Harbour", "March", "Provider"):
        assert word not in caplog.text
