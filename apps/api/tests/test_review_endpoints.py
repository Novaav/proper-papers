"""Review endpoints end to end: real app, PostgreSQL (app_test), local issuer, fake sandbox.

GET  …/documents/{d}/review
POST …/documents/{d}/review/decisions
POST …/documents/{d}/review/approvals
GET  …/documents/{d}/review/approvals/{version}/export
"""

import hashlib
import json
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import UUID, uuid4

import httpx2 as httpx
import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from api.database import (
    DatabaseSettings,
    TenantContext,
    create_runtime_engine,
    create_session_factory,
    tenant_transaction,
)
from api.review import ApprovedRecordV1, ReviewRefused, approve, record_decision
from tests.conftest import SeededTenant, sqlstate
from tests.processing_support import Seeded, block
from tests.review_support import NO_CUSTOMER_TEXT, Reviewable, reviewable_factory
from tests.test_processing_endpoints import _api
from tests.test_workspace_endpoints import Api, _subject
from tests.token_issuer import LocalIssuer

Seed = Callable[..., SeededTenant]
NOT_FOUND = {"detail": "not_found"}
UNIQUE_VIOLATION = "23505"


@pytest.fixture
def issuer() -> LocalIssuer:
    return LocalIssuer()


@pytest.fixture
def api(
    runtime_settings: DatabaseSettings, issuer: LocalIssuer, owner_engine: Engine
) -> Iterator[Api]:
    yield from _api(runtime_settings, issuer, owner_engine)


@pytest.fixture
def reviewable(
    seed_document: Callable[..., Seeded],
    sessions: sessionmaker[Session],
    worker_sessions: sessionmaker[Session],
) -> Callable[..., Reviewable]:
    return reviewable_factory(seed_document, sessions, worker_sessions)


class Review:
    """One document's review endpoints, acting as one subject."""

    def __init__(self, api: Api, target: Reviewable, subject: str) -> None:
        self.api, self.target, self.subject = api, target, subject
        tenant = target.tenant
        self.document = (
            f"/v1/tenants/{tenant.ctx.tenant_id}/workspaces/{tenant.workspace_id}"
            f"/documents/{target.seeded.document.id}"
        )
        self.path = f"{self.document}/review"

    def state(self) -> httpx.Response:
        return self.api.request("GET", self.path, self.subject)

    def decide(self, field: str, resolution: str = "candidate", **body: Any) -> httpx.Response:
        if "expected_seq" not in body:
            body["expected_seq"] = self.state().json()["decisions_through"]
        payload: dict[str, Any] = {
            "run_id": str(self.target.run_id),
            "field_key": field,
            "resolution": resolution,
        } | body
        if resolution == "candidate":
            payload.setdefault("candidate_ordinal", 0)
        return self.post("decisions", payload)

    def approve(self, **body: Any) -> httpx.Response:
        state = self.state().json()
        payload = {
            "run_id": str(self.target.run_id),
            "expected_seq": state["decisions_through"],
            "expected_version": len(state["approvals"]),
        } | body
        return self.post("approvals", payload)

    def post(self, what: str, payload: Any, **kwargs: Any) -> httpx.Response:
        content = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        headers = self.api.headers(self.subject) | {"Content-Type": "application/json"}
        return self.api.request(
            "POST", f"{self.path}/{what}", None, headers=headers, content=content, **kwargs
        )

    def export(self, version: object) -> httpx.Response:
        return self.api.request("GET", f"{self.path}/approvals/{version}/export", self.subject)

    def processing(self) -> dict[str, Any]:
        response = self.api.request("GET", f"{self.document}/processing", self.subject)
        assert response.status_code == 200
        result: dict[str, Any] = response.json()["result"]
        return result


def _as(api: Api, target: Reviewable, role: str) -> Review:
    subject = _subject()
    api.grant(subject, target.tenant.ctx.tenant_id, role)
    return Review(api, target, subject)


def _count(owner_engine: Engine, table: str, run_id: UUID) -> int:
    with owner_engine.connect() as connection:
        count: int = connection.execute(
            text(f"SELECT count(*) FROM {table} WHERE run_id = :run"), {"run": run_id}
        ).scalar_one()
    return count


# --- Reading ----------------------------------------------------------------------------------


def test_the_review_state_names_the_run_and_whether_the_caller_may_write(
    api: Api, reviewable: Callable[..., Reviewable]
) -> None:
    target = reviewable()
    owner = _as(api, target, "owner").state()
    member = _as(api, target, "member").state()
    assert owner.status_code == member.status_code == 200
    assert owner.json() == {
        "run_id": str(target.run_id),
        "can_review": True,
        "decisions_through": 0,
        "decisions": [],
        "approvals": [],
    }
    assert member.json()["can_review"] is False
    assert _as(api, target, "reviewer").state().json()["can_review"] is True


def test_the_processing_result_is_the_one_under_review(
    api: Api, reviewable: Callable[..., Reviewable]
) -> None:
    fields = _as(api, reviewable(), "owner").processing()["fields"]
    summary = {
        key: (field["status"], field["reason"], [c["value"] for c in field["candidates"]])
        for key, field in fields.items()
    }
    assert summary == {
        "provider_name": ("found", None, ["Northwind Analytics Ltd"]),
        "customer_name": ("found", None, ["Harbour Freight Services Ltd"]),
        "effective_date": ("conflict", None, ["2026-03-01", "2026-04-01"]),
        "end_date": ("unknown", "not_found", []),
        "governing_law": ("unknown", "not_found", []),
        "contract_value": ("unknown", "unparseable", [None]),
        "notice_period_days": ("unknown", "not_found", []),
    }


# --- Least privilege ---------------------------------------------------------------------------


def test_members_read_but_cannot_decide_or_approve(
    api: Api, reviewable: Callable[..., Reviewable], owner_engine: Engine
) -> None:
    target = reviewable()
    member = _as(api, target, "member")
    for response in (member.decide("effective_date", candidate_ordinal=1), member.approve()):
        assert (response.status_code, response.json()) == (403, {"detail": "not_reviewer"})
    assert _count(owner_engine, "review_decisions", target.run_id) == 0
    assert _count(owner_engine, "approved_records", target.run_id) == 0


@pytest.mark.parametrize("role", ["owner", "reviewer"])
def test_owners_and_reviewers_decide_and_approve(
    api: Api, reviewable: Callable[..., Reviewable], role: str
) -> None:
    review = _as(api, reviewable(), role)
    decided = review.decide("effective_date", candidate_ordinal=1)
    assert decided.status_code == 201
    assert decided.json() | {"decided_at": None, "decided_by": None} == {
        "seq": 1,
        "field_key": "effective_date",
        "resolution": "candidate",
        "candidate_ordinal": 1,
        "decided_by": None,
        "decided_at": None,
    }
    approved = review.approve()
    assert approved.status_code == 201
    assert (approved.json()["version"], approved.json()["decisions_through"]) == (1, 1)


def test_other_tenants_and_unknown_documents_are_404(
    api: Api, reviewable: Callable[..., Reviewable], seed_tenant: Seed
) -> None:
    target = reviewable()
    outsider_tenant = seed_tenant()
    outsider = _subject()
    api.grant(outsider, outsider_tenant.ctx.tenant_id, "owner")
    foreign = Review(api, target, outsider)
    decided = foreign.decide("provider_name", expected_seq=0)
    for response in (foreign.state(), decided, foreign.export(1)):
        assert (response.status_code, response.json()) == (404, NOT_FOUND)
    # A reviewer of their own tenant addressing another tenant's document there.
    mine = _as(api, reviewable(tenant=outsider_tenant), "owner")
    mine.path = mine.path.replace(
        str(mine.target.seeded.document.id), str(target.seeded.document.id)
    )
    assert mine.state().status_code == 404
    unknown = mine.path.replace(str(target.seeded.document.id), str(uuid4()))
    assert api.request("GET", unknown, mine.subject).status_code == 404
    malformed = mine.path.replace(str(target.seeded.document.id), "not-a-uuid")
    assert api.request("GET", malformed, mine.subject).status_code == 404


def test_a_document_that_is_no_longer_clean_cannot_be_reviewed_or_exported(
    api: Api, reviewable: Callable[..., Reviewable], owner_engine: Engine
) -> None:
    target = reviewable()
    review = _as(api, target, "owner")
    assert review.decide("effective_date", candidate_ordinal=0).status_code == 201
    assert review.approve().status_code == 201
    block(owner_engine, target.seeded)
    payload = {
        "run_id": str(target.run_id),
        "expected_seq": 1,
        "field_key": "effective_date",
        "resolution": "unknown",
    }
    for response in (review.state(), review.post("decisions", payload), review.export(1)):
        assert (response.status_code, response.json()) == (409, {"detail": "blocked"})


# --- Decisions ----------------------------------------------------------------------------------


def test_each_field_allows_only_its_decisions(
    api: Api, reviewable: Callable[..., Reviewable], owner_engine: Engine
) -> None:
    target = reviewable()
    review = _as(api, target, "owner")
    invalid = [
        review.decide("effective_date", candidate_ordinal=2),  # no such candidate
        review.decide("end_date", candidate_ordinal=0),  # not found: nothing to choose
        review.decide("contract_value", candidate_ordinal=0),  # unparseable: no value
        review.decide("provider_name", candidate_ordinal=1),  # found has one candidate
        review.decide("provider_name", "unknown", candidate_ordinal=0),  # unknown takes none
    ]
    for response in invalid:
        assert (response.status_code, response.json()) == (422, {"detail": "invalid_decision"})
    assert _count(owner_engine, "review_decisions", target.run_id) == 0
    valid = [
        review.decide("effective_date", candidate_ordinal=1),
        review.decide("provider_name", candidate_ordinal=0),
        review.decide("customer_name", "unknown"),
        review.decide("contract_value", "unknown"),
        review.decide("end_date", "unknown"),
    ]
    assert [r.status_code for r in valid] == [201] * 5
    assert [r.json()["seq"] for r in valid] == [1, 2, 3, 4, 5]


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b"[]",
        b'{"run_id": "x"}',
        b'{"run_id": "%s", "expected_seq": "0", "field_key": "provider_name", '
        b'"resolution": "unknown"}',
        b'{"run_id": "%s", "expected_seq": -1, "field_key": "provider_name", '
        b'"resolution": "unknown"}',
        b'{"run_id": "%s", "expected_seq": 0, "field_key": "provider", "resolution": "unknown"}',
        b'{"run_id": "%s", "expected_seq": 0, "field_key": "provider_name", "resolution": "x"}',
        b'{"run_id": "%s", "expected_seq": 0, "field_key": "provider_name", '
        b'"resolution": "unknown", "value": "Acme"}',
    ],
)
def test_malformed_decisions_are_422_invalid_request(
    api: Api, reviewable: Callable[..., Reviewable], body: bytes
) -> None:
    target = reviewable()
    review = _as(api, target, "owner")
    content = body.replace(b"%s", str(target.run_id).encode())
    response = review.post("decisions", content)
    assert (response.status_code, response.json()) == (422, {"detail": "invalid_request"})


def test_a_decision_on_a_number_the_reviewer_has_not_seen_is_stale(
    api: Api, reviewable: Callable[..., Reviewable], owner_engine: Engine
) -> None:
    target = reviewable()
    first, second = _as(api, target, "owner"), _as(api, target, "reviewer")
    payload = {
        "run_id": str(target.run_id),
        "expected_seq": 0,
        "field_key": "effective_date",
        "resolution": "candidate",
        "candidate_ordinal": 0,
    }
    assert first.post("decisions", payload).status_code == 201
    late = second.post("decisions", payload | {"candidate_ordinal": 1})
    assert (late.status_code, late.json()) == (409, {"detail": "stale"})
    assert second.post("decisions", payload | {"expected_seq": 1}).status_code == 201
    ahead = second.post("decisions", payload | {"expected_seq": 5})
    assert (ahead.status_code, ahead.json()) == (409, {"detail": "stale"})
    assert _count(owner_engine, "review_decisions", target.run_id) == 2


def test_decisions_on_another_run_are_run_changed(
    api: Api, reviewable: Callable[..., Reviewable]
) -> None:
    target = reviewable()
    review = _as(api, target, "owner")
    other = str(reviewable().run_id)
    decided = review.decide("effective_date", run_id=other)
    approved = review.approve(run_id=other)
    for response in (decided, approved):
        assert (response.status_code, response.json()) == (409, {"detail": "run_changed"})


def _race(
    runtime_settings: DatabaseSettings,
    ctx: TenantContext,
    count: int,
    write: Callable[[Session], object],
) -> list[str]:
    """Run `write` in `count` transactions released together; each outcome as a word."""
    engines = [create_runtime_engine(runtime_settings) for _ in range(count)]
    barrier = threading.Barrier(count)

    def attempt(engine: Engine) -> str:
        sessions = create_session_factory(engine)
        barrier.wait()
        try:
            with tenant_transaction(sessions, ctx) as session:
                write(session)
        except ReviewRefused as refusal:
            return refusal.code
        except DBAPIError as error:
            return f"sqlstate {sqlstate(error)}"
        return "written"

    try:
        with ThreadPoolExecutor(count) as pool:
            return sorted(pool.map(attempt, engines))
    finally:
        for engine in engines:
            engine.dispose()


def test_concurrent_decisions_on_the_same_number_record_exactly_one(
    reviewable: Callable[..., Reviewable], runtime_settings: DatabaseSettings, owner_engine: Engine
) -> None:
    target = reviewable()

    def decide(session: Session) -> object:
        return record_decision(
            session,
            target.tenant.workspace_id,
            target.seeded.document.id,
            tenant_id=target.tenant.ctx.tenant_id,
            user_id=target.tenant.ctx.user_id,
            run_id=target.run_id,
            expected_seq=0,
            field_key="effective_date",
            resolution="candidate",
            candidate_ordinal=1,
        )

    outcomes = _race(runtime_settings, target.tenant.ctx, 8, decide)
    assert outcomes.count("written") == 1
    # The losers either saw the winner (stale) or lost the insert race (unique violation,
    # which the API answers as 409 stale, see test_review_record.py).
    assert set(outcomes) <= {"written", "stale", f"sqlstate {UNIQUE_VIOLATION}"}
    assert _count(owner_engine, "review_decisions", target.run_id) == 1


# --- Approvals ----------------------------------------------------------------------------------


def test_approval_waits_for_every_blocking_field(
    api: Api, reviewable: Callable[..., Reviewable]
) -> None:
    review = _as(api, reviewable(), "owner")
    refused = review.approve()
    assert (refused.status_code, refused.json()) == (
        422,
        {"detail": "unresolved_fields", "fields": ["effective_date"]},
    )
    assert refused.headers["cache-control"] == "no-store"
    # Deciding a non-blocking field does not unblock it.
    assert review.decide("provider_name").status_code == 201
    assert review.approve().json()["fields"] == ["effective_date"]
    assert review.decide("effective_date", "unknown").status_code == 201
    assert review.approve().status_code == 201


def test_a_required_field_not_found_blocks_until_confirmed_unknown(
    api: Api, reviewable: Callable[..., Reviewable]
) -> None:
    review = _as(api, reviewable(NO_CUSTOMER_TEXT), "owner")
    assert review.approve().json() == {"detail": "unresolved_fields", "fields": ["customer_name"]}
    assert review.decide("customer_name", "unknown").status_code == 201
    approved = review.approve()
    assert approved.status_code == 201
    record = json.loads(review.export(1).content)
    assert record["fields"]["customer_name"] | {"decision": None} == {
        "status": "unknown",
        "value": None,
        "reason": "not_found",
        "basis": "reviewer_decision",
        "decision": None,
        "extracted": {"status": "unknown", "reason": "not_found"},
        "evidence": [],
    }


def test_an_approval_on_numbers_the_reviewer_has_not_seen_is_stale(
    api: Api, reviewable: Callable[..., Reviewable]
) -> None:
    target = reviewable()
    review = _as(api, target, "owner")
    assert review.decide("effective_date", candidate_ordinal=1).status_code == 201
    for body in ({"expected_seq": 0}, {"expected_seq": 2}, {"expected_version": 1}):
        response = review.approve(**body)
        assert (response.status_code, response.json()) == (409, {"detail": "stale"}), body
    assert review.approve().status_code == 201
    again = review.approve(expected_version=0)
    assert (again.status_code, again.json()) == (409, {"detail": "stale"})


def test_concurrent_approvals_create_exactly_one_version(
    api: Api,
    reviewable: Callable[..., Reviewable],
    runtime_settings: DatabaseSettings,
    owner_engine: Engine,
) -> None:
    target = reviewable()
    review = _as(api, target, "owner")
    assert review.decide("effective_date", candidate_ordinal=1).status_code == 201

    def approve_once(session: Session) -> object:
        return approve(
            session,
            target.tenant.workspace_id,
            target.seeded.document.id,
            tenant_id=target.tenant.ctx.tenant_id,
            user_id=target.tenant.ctx.user_id,
            run_id=target.run_id,
            expected_seq=1,
            expected_version=0,
        )

    outcomes = _race(runtime_settings, target.tenant.ctx, 8, approve_once)
    assert outcomes.count("written") == 1
    assert set(outcomes) <= {"written", "stale", f"sqlstate {UNIQUE_VIOLATION}"}
    assert _count(owner_engine, "approved_records", target.run_id) == 1


# --- The approved record and its export ---------------------------------------------------------


def test_the_export_is_the_stored_record_with_full_provenance(
    api: Api, reviewable: Callable[..., Reviewable], owner_engine: Engine
) -> None:
    target = reviewable()
    review = _as(api, target, "owner")
    reviewer_id = api.user(review.subject)
    candidates = {k: f["candidates"] for k, f in review.processing()["fields"].items()}
    run = api.request("GET", f"{review.document}/processing", review.subject).json()["run"]
    decided = review.decide("effective_date", candidate_ordinal=1).json()
    approval = review.approve().json()

    exported = review.export(1)
    assert exported.status_code == 200
    assert exported.headers["content-type"] == "application/json"
    assert exported.headers["content-disposition"] == (
        'attachment; filename="contract-approved-v1.json"; '
        "filename*=UTF-8''contract-approved-v1.json"
    )
    assert exported.headers["x-content-type-options"] == "nosniff"
    assert exported.headers["content-security-policy"] == "sandbox; default-src 'none'"
    assert exported.headers["cache-control"] == "no-store"
    digest = hashlib.sha256(exported.content).hexdigest()
    assert exported.headers["x-record-sha256"] == digest == approval["record_sha256"]
    with owner_engine.connect() as connection:
        stored: str = connection.execute(
            text("SELECT record_json FROM approved_records WHERE run_id = :run"),
            {"run": target.run_id},
        ).scalar_one()
    assert exported.content == stored.encode()
    assert review.export(1).content == exported.content  # byte-identical on every call

    record = ApprovedRecordV1.model_validate_json(exported.content)
    document = target.seeded.document
    assert record.model_dump(mode="json", by_alias=True, exclude={"fields"}) == {
        "schema": "approved_record.v1",
        "record_schema": "service_agreement.v1",
        "document": {
            "id": str(document.id),
            "version_id": str(document.version_id),
            "sha256": document.sha256.hex(),
            "name": "contract.pdf",
        },
        "processing": {
            "run_id": str(target.run_id),
            "pipeline_version": record.processing.pipeline_version,
            "extractor": "rules-1",
            "finished_at": run["finished_at"],
        },
        "approval": {
            "version": 1,
            "approved_by": str(reviewer_id),
            "approved_at": approval["approved_at"],
            "decisions_through": 1,
        },
    }
    fields = json.loads(exported.content)["fields"]
    assert fields["effective_date"] == {
        "status": "found",
        "value": "2026-04-01",
        "reason": None,
        "basis": "reviewer_decision",
        "decision": {
            "seq": 1,
            "decided_by": str(reviewer_id),
            "decided_at": decided["decided_at"],
        },
        "extracted": {"status": "conflict", "reason": None},
        "evidence": candidates["effective_date"][1]["evidence"],
    }
    assert fields["provider_name"] == {
        "status": "found",
        "value": "Northwind Analytics Ltd",
        "reason": None,
        "basis": "approved_as_extracted",
        "decision": None,
        "extracted": {"status": "found", "reason": None},
        "evidence": candidates["provider_name"][0]["evidence"],
    }
    assert fields["contract_value"] == {
        "status": "unknown",
        "value": None,
        "reason": "unparseable",
        "basis": "approved_as_extracted",
        "decision": None,
        "extracted": {"status": "unknown", "reason": "unparseable"},
        "evidence": [],
    }
    assert {key: field["status"] for key, field in fields.items()} == {
        "provider_name": "found",
        "customer_name": "found",
        "effective_date": "found",
        "end_date": "unknown",
        "governing_law": "unknown",
        "contract_value": "unknown",
        "notice_period_days": "unknown",
    }


def test_a_new_version_supersedes_without_changing_the_previous_one(
    api: Api, reviewable: Callable[..., Reviewable]
) -> None:
    review = _as(api, reviewable(), "owner")
    assert review.decide("effective_date", candidate_ordinal=0).status_code == 201
    assert review.approve().status_code == 201
    first = review.export(1).content
    assert review.decide("provider_name", "unknown").status_code == 201
    second = review.approve()
    assert (second.status_code, second.json()["version"]) == (201, 2)
    assert review.export(1).content == first
    fields = json.loads(review.export(2).content)["fields"]
    assert (fields["provider_name"]["status"], fields["provider_name"]["reason"]) == (
        "unknown",
        "rejected_by_reviewer",
    )
    assert fields["effective_date"]["value"] == "2026-03-01"
    state = review.state().json()
    assert [a["version"] for a in state["approvals"]] == [1, 2]
    assert [a["decisions_through"] for a in state["approvals"]] == [1, 2]


@pytest.mark.parametrize("version", ["2", "0", "01", "abc", "1e3", "-1", "9999999999", chr(0xFF11)])
def test_unknown_or_malformed_versions_are_404(
    api: Api, reviewable: Callable[..., Reviewable], version: str
) -> None:
    review = _as(api, reviewable(), "owner")
    assert review.decide("effective_date", candidate_ordinal=0).status_code == 201
    assert review.approve().status_code == 201
    response = review.export(version)
    assert (response.status_code, response.json()) == (404, NOT_FOUND)


def test_members_can_export_approved_records(
    api: Api, reviewable: Callable[..., Reviewable]
) -> None:
    target = reviewable()
    owner = _as(api, target, "owner")
    assert owner.decide("effective_date", candidate_ordinal=0).status_code == 201
    assert owner.approve().status_code == 201
    member = _as(api, target, "member")
    assert member.export(1).content == owner.export(1).content


def test_without_a_finished_run_there_is_nothing_to_review(
    api: Api, seed_document: Callable[..., Seeded]
) -> None:
    seeded = seed_document()
    target = Reviewable(seeded, uuid4())
    review = _as(api, target, "owner")
    state = review.state().json()
    assert (state["run_id"], state["decisions"], state["approvals"]) == (None, [], [])
    decided = review.decide("provider_name", "unknown")
    assert (decided.status_code, decided.json()) == (409, {"detail": "run_changed"})


def test_signed_out_callers_are_401(api: Api, reviewable: Callable[..., Reviewable]) -> None:
    review = Review(api, reviewable(), _subject())
    assert api.request("GET", review.path, None).status_code == 401
    assert api.request("POST", f"{review.path}/decisions", None).status_code == 401


def test_the_repository_refuses_unseen_numbers_before_writing(
    reviewable: Callable[..., Reviewable], sessions: sessionmaker[Session]
) -> None:
    # The database would refuse these too (test_review_schema.py); the repository
    # answers first, so the API never depends on a constraint error for a seen change.
    target = reviewable()
    ctx = target.tenant.ctx
    workspace, document = target.tenant.workspace_id, target.seeded.document.id
    with pytest.raises(ReviewRefused) as raised, tenant_transaction(sessions, ctx) as session:
        record_decision(
            session,
            workspace,
            document,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            run_id=target.run_id,
            expected_seq=3,
            field_key="end_date",
            resolution="unknown",
            candidate_ordinal=None,
        )
    assert raised.value.code == "stale"
    for seq, version in ((1, 0), (0, 2)):
        with pytest.raises(ReviewRefused) as raised, tenant_transaction(sessions, ctx) as session:
            approve(
                session,
                workspace,
                document,
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                run_id=target.run_id,
                expected_seq=seq,
                expected_version=version,
            )
        assert raised.value.code == "stale"


def test_a_stored_record_that_no_longer_matches_its_hash_is_never_served(
    api: Api, reviewable: Callable[..., Reviewable], admin_engine: Engine
) -> None:
    target = reviewable()
    review = _as(api, target, "owner")
    assert review.decide("effective_date", candidate_ordinal=0).status_code == 201
    assert review.approve().status_code == 201
    # Simulated storage corruption: only a superuser bypassing triggers can do this.
    with admin_engine.connect() as connection:
        connection.execute(text("SET session_replication_role = replica"))
        connection.execute(
            text(
                "UPDATE app.approved_records SET record_json = "
                "replace(record_json, '2026-03-01', '2026-03-02') WHERE run_id = :run"
            ),
            {"run": target.run_id},
        )
        connection.execute(text("SET session_replication_role = origin"))
    response = review.export(1)
    assert (response.status_code, response.json()) == (503, {"detail": "unavailable"})
