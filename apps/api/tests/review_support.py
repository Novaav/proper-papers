"""Shared setup for the R1 review tests (disposable test database only).

A reviewable run is produced the way production does it: a member starts
processing and the worker processes the run, here with a fake sandbox result.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from api.processing import pipeline_version
from api.worker import Worker
from tests.conftest import SeededTenant
from tests.processing_support import Seeded, start
from tests.test_worker import CONFLICT, SANDBOX, SETTINGS, TEXT, parsed

# Provider and customer found; effective date in conflict (1 March / 1 April 2026);
# contract value unparseable (a candidate without a value); the rest not found.
# Blocking: effective_date only.
REVIEW_TEXT = CONFLICT + "\n\nContract Value: to be confirmed in writing"
# No customer: a required field not found (blocking), nothing in conflict.
NO_CUSTOMER_TEXT = (
    "SERVICES AGREEMENT\n\nProvider: Northwind Analytics Ltd\nEffective Date: 1 March 2026"
)
COMPLETE_TEXT = TEXT


def new_pipeline() -> str:
    """A pipeline of this test only, so the worker's queue holds only its own runs."""
    return pipeline_version("sha256:" + uuid.uuid4().hex * 2)


def process(
    sessions: sessionmaker[Session],
    worker_sessions: sessionmaker[Session],
    seeded: Seeded,
    text: str = REVIEW_TEXT,
) -> UUID:
    """Start and finish processing of the seeded document; returns the run id."""
    pipeline = new_pipeline()
    run_id = start(sessions, seeded, pipeline)
    worker = Worker(
        worker_sessions,
        sandbox=SANDBOX,
        pipeline=pipeline,
        settings=SETTINGS,
        parse=lambda _content, _settings: parsed(text),
    )
    assert worker.run_once()
    return run_id


@dataclass(frozen=True)
class Reviewable:
    seeded: Seeded
    run_id: UUID

    @property
    def tenant(self) -> SeededTenant:
        return self.seeded.tenant


def reviewable_factory(
    seed_document: Callable[..., Seeded],
    sessions: sessionmaker[Session],
    worker_sessions: sessionmaker[Session],
) -> Callable[..., Reviewable]:
    def make(text: str = REVIEW_TEXT, tenant: SeededTenant | None = None) -> Reviewable:
        seeded = seed_document(tenant=tenant)
        return Reviewable(seeded, process(sessions, worker_sessions, seeded, text))

    return make
