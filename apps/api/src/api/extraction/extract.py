"""Deterministic, rule-based extraction of `service_agreement.v1` (DI-1 baseline).

Input: the validated per-page text from the parser sandbox. Output: an
`ExtractionResult` whose every candidate carries exact evidence spans.

Rules (the development corpus is the only source of phrasings; held-out
documents are never used to add or tune a rule):
- Labelled lines, anywhere on a line: `Provider:`, `Customer:`,
  `Effective Date:`, `End Date:`, `Governing Law:`, `Contract Value:`,
  `Notice Period:`, followed by the value up to the end of the line. A label
  inside prose still counts: a look-alike therefore surfaces as a conflict
  instead of being silently ignored or chosen.
- Clauses: a recital `between <provider> (the "Provider") and <customer>
  (the "Customer")`; `commences on <date>`, `is effective as of <date>`;
  `ends on <date>`; `total fees payable under this Agreement are <money>`,
  `total contract value is <money>`; `on <n> days' [written] notice`;
  `governed by the laws of <jurisdiction>`.

Document text is data: nothing in it changes what the rules look for, and an
instruction-like sentence is matched only if it has the same form as a rule.
"""

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from api.extraction.contract import (
    FIELDS,
    SCHEMA,
    Candidate,
    Evidence,
    ExtractionResult,
    FieldKey,
    FieldResult,
    Reason,
    Value,
    derive_outcome,
)
from api.extraction.normalize import (
    normalize_date,
    normalize_days,
    normalize_money,
    normalize_text,
)

EXTRACTOR_VERSION = "rules-1"

type Normalizer = Callable[[str], tuple[Value, None] | tuple[None, Reason]]

NORMALIZERS: dict[FieldKey, Normalizer] = {
    "provider_name": normalize_text,
    "customer_name": normalize_text,
    "effective_date": normalize_date,
    "end_date": normalize_date,
    "governing_law": normalize_text,
    "contract_value": normalize_money,
    "notice_period_days": normalize_days,
}
LABELS: dict[str, FieldKey] = {
    "Provider": "provider_name",
    "Customer": "customer_name",
    "Effective Date": "effective_date",
    "End Date": "end_date",
    "Governing Law": "governing_law",
    "Contract Value": "contract_value",
    "Notice Period": "notice_period_days",
}
_LABEL = re.compile(
    r"(?<![\w])(?P<label>" + "|".join(map(re.escape, LABELS)) + r"):[ ]*(?P<value>[^\n]*)"
)
_DATE = (
    r"\d{1,2} [A-Z][a-z]+ \d{4}|[A-Z][a-z]+ \d{1,2}, \d{4}|\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4}"
)
_MONEY = r"(?:[A-Z]{3} ?|€)\d[\d,]*(?:\.\d+)?"
_NAME = r"[^\n()\"]{1,200}?"
# (pattern, {group: field}); every group is the raw value, the whole match is the quote.
CLAUSES: tuple[tuple[re.Pattern[str], dict[str, FieldKey]], ...] = (
    (
        re.compile(
            rf"\bbetween (?P<provider>{_NAME})\s*\(the \"Provider\"\)\s+"
            rf"and (?P<customer>{_NAME})\s*\(the \"Customer\"\)"
        ),
        {"provider": "provider_name", "customer": "customer_name"},
    ),
    (
        re.compile(rf"\b(?:commences on|is effective as of) (?P<raw>{_DATE})"),
        {"raw": "effective_date"},
    ),
    (re.compile(rf"\bends on (?P<raw>{_DATE})"), {"raw": "end_date"}),
    (
        re.compile(
            rf"\b(?:total fees payable under this Agreement are|total contract value is) "
            rf"(?P<raw>{_MONEY})"
        ),
        {"raw": "contract_value"},
    ),
    (re.compile(r"\bon (?P<raw>\d{1,4} days)' (?:written )?notice"), {"raw": "notice_period_days"}),
    (re.compile(r"\bgoverned by the laws of (?P<raw>[^.\n]{1,200})"), {"raw": "governing_law"}),
)


@dataclass(frozen=True)
class Page:
    number: int
    text: str


@dataclass(frozen=True)
class _Mention:
    field: FieldKey
    raw: str
    raw_at: tuple[int, int]  # (page, offset of the raw value): identifies a mention
    value: Value | None
    reason: Reason | None
    evidence: Evidence


def extract(pages: Iterable[Page]) -> ExtractionResult:
    mentions: dict[FieldKey, list[_Mention]] = {key: [] for key in FIELDS}
    seen: set[tuple[FieldKey, tuple[int, int]]] = set()
    for mention in _mentions(pages):
        if (mention.field, mention.raw_at) not in seen:
            seen.add((mention.field, mention.raw_at))
            mentions[mention.field].append(mention)
    fields = {key: _field(sorted(found, key=lambda m: m.raw_at)) for key, found in mentions.items()}
    return ExtractionResult.model_validate(
        {
            "schema": SCHEMA,
            "extractor": EXTRACTOR_VERSION,
            "outcome": derive_outcome(fields),
            "fields": fields,
        }
    )


def _mentions(pages: Iterable[Page]) -> Iterable[_Mention]:
    for page in pages:
        for match in _LABEL.finditer(page.text):
            key = LABELS[match["label"]]
            raw = match["value"].rstrip()
            if key not in ("provider_name", "customer_name") and raw.endswith("."):
                raw = raw[:-1]  # a sentence's full stop is not part of a date, amount or place
            if raw:
                end = match.start("value") + len(raw)
                yield _mention(page, key, raw, match.start("value"), match.start(), end)
        for pattern, groups in CLAUSES:
            for match in pattern.finditer(page.text):
                for group, key in groups.items():
                    yield _mention(page, key, match[group], match.start(group), *match.span())


def _mention(page: Page, key: FieldKey, raw: str, raw_start: int, start: int, end: int) -> _Mention:
    value, reason = NORMALIZERS[key](raw)
    quote = page.text[start:end]
    return _Mention(
        field=key,
        raw=raw,
        raw_at=(page.number, raw_start),
        value=value,
        reason=reason,
        evidence=Evidence(page=page.number, start=start, end=end, quote=quote),
    )


def _field(mentions: list[_Mention]) -> FieldResult:
    if not mentions:
        return FieldResult(status="unknown", reason="not_found", candidates=[])
    grouped: list[tuple[Value, list[_Mention]]] = []
    unresolved: list[_Mention] = []
    for mention in mentions:
        if mention.value is None:
            unresolved.append(mention)
            continue
        for value, members in grouped:
            if value == mention.value:
                members.append(mention)
                break
        else:
            grouped.append((mention.value, [mention]))
    candidates = [
        Candidate(value=value, raw=members[0].raw, evidence=_evidence(members))
        for value, members in grouped
    ] + [Candidate(value=None, raw=m.raw, evidence=[m.evidence]) for m in unresolved]
    candidates.sort(key=lambda c: (c.evidence[0].page, c.evidence[0].start))
    if len(grouped) == 1 and not unresolved:
        return FieldResult(status="found", reason=None, candidates=candidates)
    if grouped:
        return FieldResult(status="conflict", reason=None, candidates=candidates)
    reason: Reason = (
        "ambiguous" if any(m.reason == "ambiguous" for m in unresolved) else "unparseable"
    )
    return FieldResult(status="unknown", reason=reason, candidates=candidates)


def _evidence(members: list[_Mention]) -> list[Evidence]:
    spans: list[Evidence] = []
    for member in members:
        if member.evidence not in spans:
            spans.append(member.evidence)
    return spans
