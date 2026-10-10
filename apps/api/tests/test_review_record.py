"""The approved_record.v1 model, its committed schema, and the review API's fixed answers.

No database: these are the pure rules the database tests rely on.
"""

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import DBAPIError

from api.extraction.contract import FieldResult
from api.main import _parse_version, _review_race, _review_refused
from api.review import (
    ApprovedField,
    ApprovedRecordV1,
    ReviewRefused,
    _blocking,
    canonical_json,
)
from scripts.record_schema import SCHEMA_PATH, render

NOW = datetime(2026, 10, 1, 9, 30, tzinfo=UTC)
SPAN = {"page": 1, "start": 0, "end": 15, "quote": "Provider: Ä Ltd"}


def _field(**changes: Any) -> dict[str, Any]:
    return {
        "status": "found",
        "value": "Ä Ltd",
        "reason": None,
        "basis": "approved_as_extracted",
        "decision": None,
        "extracted": {"status": "found", "reason": None},
        "evidence": [SPAN],
    } | changes


def _unknown(**changes: Any) -> dict[str, Any]:
    return (
        _field(
            status="unknown",
            value=None,
            reason="not_found",
            evidence=[],
            extracted={"status": "unknown", "reason": "not_found"},
        )
        | changes
    )


def _record(**fields: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "provider_name",
        "customer_name",
        "effective_date",
        "end_date",
        "governing_law",
        "contract_value",
        "notice_period_days",
    )
    return {
        "schema": "approved_record.v1",
        "record_schema": "service_agreement.v1",
        "document": {
            "id": str(uuid4()),
            "version_id": str(uuid4()),
            "sha256": "a" * 64,
            "name": "x",
        },
        "processing": {
            "run_id": str(uuid4()),
            "pipeline_version": "service_agreement.v1+rules-1+parser@sha256:" + "b" * 64,
            "extractor": "rules-1",
            "finished_at": NOW.isoformat(),
        },
        "approval": {
            "version": 1,
            "approved_by": str(uuid4()),
            "approved_at": NOW.isoformat(),
            "decisions_through": 0,
        },
        "fields": {key: fields.get(key, _unknown()) for key in keys},
    }


@pytest.mark.parametrize(
    "field",
    [
        _field(evidence=[]),  # found without evidence
        _field(value=None),  # found without a value
        _field(reason="not_found"),
        _unknown(value="Ä Ltd"),  # unknown with a value
        _unknown(reason=None),
        _unknown(evidence=[SPAN]),
        _field(basis="reviewer_decision"),  # a decision basis without the decision
        _field(decision={"seq": 1, "decided_by": str(uuid4()), "decided_at": NOW.isoformat()}),
        _field(status="conflict"),
        _field(value="free text", unexpected=True),
    ],
)
def test_every_approved_field_is_grounded_or_explicitly_unknown(field: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        ApprovedField.model_validate_json(json.dumps(field))


def test_a_record_needs_every_field_exactly_once() -> None:
    record = _record()
    ApprovedRecordV1.model_validate_json(json.dumps(record))
    del record["fields"]["end_date"]
    with pytest.raises(ValueError):
        ApprovedRecordV1.model_validate_json(json.dumps(record))


def test_the_canonical_form_is_sorted_compact_utf8_and_stable() -> None:
    record = ApprovedRecordV1.model_validate_json(json.dumps(_record(provider_name=_field())))
    body = canonical_json(record)
    parsed = json.loads(body)
    assert body == json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert "Ä Ltd" in body  # UTF-8, not \\u-escaped
    assert canonical_json(ApprovedRecordV1.model_validate_json(body)) == body


def test_the_committed_schema_matches_the_model() -> None:
    # Regenerate with: uv run --locked python -m scripts.record_schema
    assert SCHEMA_PATH.read_text(encoding="utf-8") == render()


@pytest.mark.parametrize(
    ("key", "status", "reason", "blocking"),
    [
        ("effective_date", "conflict", None, True),
        ("end_date", "conflict", None, True),  # every conflict blocks
        ("provider_name", "unknown", "not_found", True),  # required and not found
        ("customer_name", "unknown", "ambiguous", True),
        ("effective_date", "unknown", "unparseable", True),
        ("end_date", "unknown", "not_found", False),  # optional
        ("contract_value", "unknown", "unparseable", False),
        ("provider_name", "found", None, False),
    ],
)
def test_blocking_fields_are_conflicts_and_required_fields_not_found(
    key: Any, status: str, reason: str | None, blocking: bool
) -> None:
    candidates: list[dict[str, Any]] = []
    span = {"page": 1, "start": 0, "end": 4, "quote": "text"}
    if status == "found":
        candidates = [{"value": "text", "raw": "text", "evidence": [span]}]
    elif status == "conflict":
        candidates = [
            {"value": "2026-03-01", "raw": "text", "evidence": [span]},
            {"value": "2026-04-01", "raw": "text", "evidence": [span]},
        ]
    elif reason != "not_found":
        candidates = [{"value": None, "raw": "text", "evidence": [span]}]
    field = FieldResult.model_validate(
        {"status": status, "reason": reason, "candidates": candidates}, strict=False
    )
    assert _blocking(key, field) is blocking


class _Orig(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


@pytest.mark.parametrize("state", ["23505", "RV001"])
def test_a_review_write_that_lost_a_race_is_409_stale(state: str) -> None:
    with pytest.raises(HTTPException) as raised, _review_race():
        raise DBAPIError("INSERT", {}, _Orig(state))
    assert (raised.value.status_code, raised.value.detail) == (409, "stale")


def test_other_database_errors_pass_through_unchanged() -> None:
    with pytest.raises(DBAPIError), _review_race():
        raise DBAPIError("INSERT", {}, _Orig("23514"))


@pytest.mark.parametrize(
    ("code", "status"),
    [("stale", 409), ("run_changed", 409), ("invalid_decision", 422)],
)
def test_refusals_have_fixed_answers(code: Any, status: int) -> None:
    answer = _review_refused(ReviewRefused(code))
    assert (answer.status_code, answer.detail) == (status, code)


@pytest.mark.parametrize(("value", "number"), [("1", 1), ("12", 12), ("999999999", 999_999_999)])
def test_versions_are_plain_positive_integers(value: str, number: int) -> None:
    assert _parse_version(value) == number


@pytest.mark.parametrize(
    "value", ["", "0", "01", "-1", "+1", "1.0", "1e3", chr(0xFF11), "1000000000"]
)
def test_anything_else_is_not_found(value: str) -> None:
    with pytest.raises(HTTPException) as raised:
        _parse_version(value)
    assert raised.value.status_code == 404
