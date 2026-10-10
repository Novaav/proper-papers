"""The `service_agreement.v1` extraction contract (DI-1, decision F1).

Every field has exactly one status:
- `found`: exactly one distinct normalized value, with at least one evidence span.
- `unknown` with a reason: `not_found` (no candidate at all), `unparseable` (a
  candidate whose value cannot be normalized) or `ambiguous` (a candidate with
  more than one valid reading, never guessed). Unparseable and ambiguous
  candidates keep their evidence, with no value.
- `conflict`: at least two distinct values, or a value next to an unresolved
  (unparseable or ambiguous) candidate. Every candidate is kept; none is chosen.

The run needs review if any required field is not `found` or any field is in
conflict. There is no numeric confidence.

Evidence is `(page, start, end, quote)` over the parser's normalized page text:
`page_text[start:end] == quote`, and each candidate's `raw` text (what the value
was normalized from) lies inside its quote. `evidence_violations` checks both
against the page texts; a result with violations must not be used.
"""

from collections.abc import Mapping
from datetime import date
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA = "service_agreement.v1"

FieldKey = Literal[
    "provider_name",
    "customer_name",
    "effective_date",
    "end_date",
    "governing_law",
    "contract_value",
    "notice_period_days",
]
FIELDS: tuple[FieldKey, ...] = (
    "provider_name",
    "customer_name",
    "effective_date",
    "end_date",
    "governing_law",
    "contract_value",
    "notice_period_days",
)
REQUIRED: frozenset[FieldKey] = frozenset({"provider_name", "customer_name", "effective_date"})
TEXT_FIELDS: frozenset[FieldKey] = frozenset({"provider_name", "customer_name", "governing_law"})
DATE_FIELDS: frozenset[FieldKey] = frozenset({"effective_date", "end_date"})
MAX_TEXT_VALUE = 200
MAX_NOTICE_DAYS = 3650

Status = Literal["found", "unknown", "conflict"]
Reason = Literal["not_found", "unparseable", "ambiguous"]
Outcome = Literal["completed", "needs_review"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Money(_Strict):
    amount: Annotated[str, Field(pattern=r"^(0|[1-9]\d{0,14})\.\d{2}$")]
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]


Value = str | int | Money


class Evidence(_Strict):
    page: Annotated[int, Field(ge=1)]
    start: Annotated[int, Field(ge=0)]
    end: Annotated[int, Field(ge=1)]
    quote: Annotated[str, Field(min_length=1)]

    @model_validator(mode="after")
    def _range(self) -> Self:
        if self.end <= self.start or self.end - self.start != len(self.quote):
            raise ValueError("evidence range does not match its quote")
        return self


class Candidate(_Strict):
    value: Value | None
    raw: Annotated[str, Field(min_length=1)]
    evidence: Annotated[list[Evidence], Field(min_length=1)]


class FieldResult(_Strict):
    status: Status
    reason: Reason | None
    candidates: list[Candidate]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        values = [c.value for c in self.candidates if c.value is not None]
        unresolved = [c for c in self.candidates if c.value is None]
        distinct = _distinct(values)
        if self.status == "found":
            ok = self.reason is None and len(self.candidates) == 1 and len(values) == 1
        elif self.status == "unknown":
            if self.reason == "not_found":
                ok = not self.candidates
            else:
                ok = self.reason is not None and bool(self.candidates) and not values
        else:
            ok = (
                self.reason is None
                and len(distinct) == len(values)  # one candidate per distinct value
                and (len(distinct) >= 2 or (len(distinct) == 1 and bool(unresolved)))
            )
        if not ok:
            raise ValueError(f"inconsistent field result for status {self.status}")
        return self


class ExtractionResult(_Strict):
    schema_: Literal["service_agreement.v1"] = Field(alias="schema")
    extractor: Annotated[str, Field(min_length=1, max_length=40)]
    outcome: Outcome
    fields: dict[FieldKey, FieldResult]

    @model_validator(mode="after")
    def _complete(self) -> Self:
        if set(self.fields) != set(FIELDS):
            raise ValueError("every field must be present exactly once")
        for key, result in self.fields.items():
            for candidate in result.candidates:
                if candidate.value is not None and not _valid_value(key, candidate.value):
                    raise ValueError(f"value of the wrong type or range for {key}")
        if self.outcome != derive_outcome(self.fields):
            raise ValueError("outcome does not follow from the field statuses")
        return self


def derive_outcome(fields: Mapping[FieldKey, FieldResult]) -> Outcome:
    for key, result in fields.items():
        if result.status == "conflict" or (key in REQUIRED and result.status != "found"):
            return "needs_review"
    return "completed"


def evidence_violations(result: ExtractionResult, pages: Mapping[int, str]) -> list[str]:
    """Provenance problems (field names and counts only, never document text)."""
    problems: list[str] = []
    for key, field in result.fields.items():
        for index, candidate in enumerate(field.candidates):
            for span in candidate.evidence:
                text = pages.get(span.page)
                if text is None or span.end > len(text):
                    problems.append(f"{key}[{index}]: evidence outside the document")
                elif text[span.start : span.end] != span.quote:
                    problems.append(f"{key}[{index}]: quote does not match the page text")
            if not any(candidate.raw in span.quote for span in candidate.evidence):
                problems.append(f"{key}[{index}]: raw value not inside any quote")
    return problems


def _distinct(values: list[Value]) -> list[Value]:
    distinct: list[Value] = []
    for value in values:
        if value not in distinct:
            distinct.append(value)
    return distinct


def _valid_value(key: FieldKey, value: Value) -> bool:
    if key in TEXT_FIELDS:
        return (
            isinstance(value, str) and 0 < len(value) <= MAX_TEXT_VALUE and value == value.strip()
        )
    if key in DATE_FIELDS:
        if not isinstance(value, str):
            return False
        try:
            return date.fromisoformat(value).isoformat() == value
        except ValueError:
            return False
    if key == "contract_value":
        return isinstance(value, Money)
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_NOTICE_DAYS
