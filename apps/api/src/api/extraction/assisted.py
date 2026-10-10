"""Model-assisted extraction (AI1, architecture B'): rules first, the model proposes spans.

1. The rules (`extract`, rules-1) run first. Their results are final: the model
   is never asked about, and can never change, a field the rules found
   anything for (found, conflict, unparseable or ambiguous).
2. Only for the fields the rules left `not_found`, the model is asked for
   mentions `{page, quote, raw}` (or none). It decides no value.
3. Every mention is verified here, deterministically: the page must exist, the
   quote must occur verbatim on that page (the offsets are computed here, never
   taken from the model), `raw` must lie inside the quote, and both are bounded.
   A mention that fails is rejected and counted; it never becomes a claim.
4. Surviving mentions go through the same normalizers and contract rules as the
   rules' own mentions: one distinct value is found, several are a conflict, an
   unnormalizable value is unknown (unparseable or ambiguous).
5. The combined result is validated against the contract and must pass the
   unchanged `evidence_violations` check.

Anything wrong with the model's answer as a whole (no answer, a refusal, a
truncated or malformed answer, a cap exceeded) leaves the rules' result
unchanged and is recorded in the call record; a partial answer is never used.
"""

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from api.extraction.contract import (
    FIELDS,
    SCHEMA,
    ExtractionResult,
    FieldKey,
    FieldResult,
    derive_outcome,
    evidence_violations,
)
from api.extraction.entities import is_defined_term, is_truncated, same_party
from api.extraction.extract import Page, _field, _Mention, _mention, extract
from api.extraction.proposer import (
    Proposer,
    ProviderResponse,
    build_request,
    configuration,
    request_sha256,
)

PROMPTS = Path(__file__).with_name("prompts")
MAX_INPUT_CHARS = 20_000  # page text sent per document; above it the model is not asked
MAX_MENTIONS_PER_FIELD = 5
MAX_QUOTE_CHARS = 300
_NAME_FIELDS: frozenset[FieldKey] = frozenset({"provider_name", "customer_name"})

CallStatus = Literal[
    "not_needed",  # the rules left no field not_found: no call
    "input_too_long",  # no call
    "ok",
    "invalid",  # the answer as a whole was unusable (malformed, over a cap, failed checks)
    "refused",
    "truncated",
    "unavailable",
]


@dataclass(frozen=True)
class Profile:
    """One frozen variant of assisted extraction: its prompt and its verification.

    assisted-1 is historical evidence (measured, not enabled) and must replay
    exactly: its version hashes the prompt and the request configuration only,
    as when it was frozen. Later profiles also record their verifier version.
    """

    name: str
    prompt: Path
    verifier: str | None  # None: assisted-1's verification (not part of its hash)
    entity_checks: bool  # the party-name checks (entities.py)


ASSISTED_1 = Profile("assisted-1", PROMPTS / "assisted-1.md", None, False)
ASSISTED_2 = Profile("assisted-2", PROMPTS / "assisted-2.md", "2", True)
PROFILES = {profile.name: profile for profile in (ASSISTED_1, ASSISTED_2)}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ProposedMention(_Strict):
    page: int
    quote: str
    raw: str


class Proposal(_Strict):
    """The model's answer: one list of mentions per field (validated, not yet trusted)."""

    provider_name: Annotated[list[ProposedMention], Field(max_length=MAX_MENTIONS_PER_FIELD)]
    customer_name: Annotated[list[ProposedMention], Field(max_length=MAX_MENTIONS_PER_FIELD)]
    effective_date: Annotated[list[ProposedMention], Field(max_length=MAX_MENTIONS_PER_FIELD)]
    end_date: Annotated[list[ProposedMention], Field(max_length=MAX_MENTIONS_PER_FIELD)]
    governing_law: Annotated[list[ProposedMention], Field(max_length=MAX_MENTIONS_PER_FIELD)]
    contract_value: Annotated[list[ProposedMention], Field(max_length=MAX_MENTIONS_PER_FIELD)]
    notice_period_days: Annotated[list[ProposedMention], Field(max_length=MAX_MENTIONS_PER_FIELD)]

    def mentions(self, key: FieldKey) -> list[ProposedMention]:
        mentions: list[ProposedMention] = getattr(self, key)
        return mentions


@dataclass(frozen=True)
class CallRecord:
    """One document's model call: counts and measurements only, never document text."""

    status: CallStatus
    requested: tuple[FieldKey, ...]
    request_sha256: str | None = None
    model: str | None = None
    proposed: int = 0
    rejected: int = 0
    model_fields: tuple[FieldKey, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0
    latency_seconds: float | None = None
    cost_usd: float = 0.0
    error: str | None = None
    entity_rejected: int = 0  # of `rejected`: party names refused by the entity checks


def prompt_text(profile: Profile = ASSISTED_1) -> str:
    return profile.prompt.read_text(encoding="utf-8")


def prompt_sha256(profile: Profile = ASSISTED_1) -> str:
    return hashlib.sha256(profile.prompt.read_bytes()).hexdigest()


def config_sha256(profile: Profile = ASSISTED_1) -> str:
    """The prompt, the configuration and the verifier version, hashed: what the version pins."""
    record = {"prompt": profile.name, "prompt_sha256": prompt_sha256(profile), **configuration()}
    if profile.verifier is not None:
        record["verifier"] = profile.verifier
    return hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()


def extractor_version(profile: Profile = ASSISTED_1) -> str:
    return f"{profile.name}+{config_sha256(profile)[:12]}"


def verify(
    proposal: Proposal, pages: dict[int, str], requested: Iterable[FieldKey]
) -> tuple[dict[FieldKey, list[_Mention]], int]:
    """The mentions that survive every check, per requested field, and the rejected count.

    Mentions of fields that were not requested are rejected (the model may not
    touch a field the rules decided).
    """
    asked = set(requested)
    accepted: dict[FieldKey, list[_Mention]] = {key: [] for key in asked}
    rejected = 0
    for key in FIELDS:
        for proposed in proposal.mentions(key):
            mention = _verified(key, proposed, pages) if key in asked else None
            if mention is None:
                rejected += 1
            elif all(m.raw_at != mention.raw_at for m in accepted[key]):
                accepted[key].append(mention)
    return accepted, rejected


def check_parties(
    accepted: dict[FieldKey, list[_Mention]], pages: dict[int, str], rules: ExtractionResult
) -> tuple[dict[FieldKey, list[_Mention]], int]:
    """assisted-2's party-name checks (entities.py): reject-only, never a new value.

    A proposed provider or customer name is rejected if the document introduces
    it as a defined term or alias, if it is anywhere continued into a longer
    name, or if it equals the other party's name (from the rules or the model).
    """
    texts = list(pages.values())
    rejected = 0
    kept = dict(accepted)
    for key in _NAME_FIELDS & accepted.keys():
        complete = []
        for mention in accepted[key]:
            name = str(mention.value) if mention.value is not None else mention.raw
            if is_defined_term(name, texts) or is_truncated(mention.raw, texts):
                rejected += 1
            else:
                complete.append(mention)
        kept[key] = complete
    names = {
        key: [str(c.value) for c in rules.fields[key].candidates if c.value is not None]
        + [str(m.value) for m in kept.get(key, []) if m.value is not None]
        for key in _NAME_FIELDS
    }
    for key in _NAME_FIELDS & kept.keys():
        (other,) = _NAME_FIELDS - {key}
        distinct = [
            m
            for m in kept[key]
            if m.value is None or not any(same_party(str(m.value), n) for n in names[other])
        ]
        rejected += len(kept[key]) - len(distinct)
        kept[key] = distinct
    return kept, rejected


def combine(
    rules: ExtractionResult, accepted: dict[FieldKey, list[_Mention]], version: str
) -> ExtractionResult:
    """The rules' result with model-sourced fields only where the rules found nothing."""
    fields: dict[FieldKey, FieldResult] = dict(rules.fields)
    for key, mentions in accepted.items():
        if mentions and rules.fields[key].reason == "not_found":
            fields[key] = _field(sorted(mentions, key=lambda m: m.raw_at))
    return ExtractionResult.model_validate(
        {
            "schema": SCHEMA,
            "extractor": version,
            "outcome": derive_outcome(fields),
            "fields": fields,
        }
    )


@dataclass
class AssistedExtractor:
    """Callable like `extract`; records one `CallRecord` per document in `calls`."""

    proposer: Proposer
    profile: Profile = ASSISTED_1
    calls: list[CallRecord] = field(default_factory=list)

    def __call__(self, pages: Iterable[Page]) -> ExtractionResult:
        page_list = list(pages)
        texts = {page.number: page.text for page in page_list}
        version = extractor_version(self.profile)
        rules = extract(page_list)
        unchanged = rules.model_copy(update={"extractor": version})
        requested = [key for key in FIELDS if rules.fields[key].reason == "not_found"]
        if not requested:
            self.calls.append(CallRecord(status="not_needed", requested=()))
            return unchanged
        if sum(len(text) for text in texts.values()) > MAX_INPUT_CHARS:
            self.calls.append(CallRecord(status="input_too_long", requested=tuple(requested)))
            return unchanged
        request = build_request(prompt_text(self.profile), requested, texts)
        response = self.proposer.propose(request)
        record = _record(response, requested, request_sha256(request))
        if response.status != "ok" or response.text is None:
            self.calls.append(record)
            return unchanged
        try:
            proposal = Proposal.model_validate_json(response.text)
        except ValidationError:
            self.calls.append(replace(record, status="invalid"))
            return unchanged
        accepted, rejected = verify(proposal, texts, requested)
        entity_rejected = 0
        if self.profile.entity_checks:
            accepted, entity_rejected = check_parties(accepted, texts, rules)
        proposed = sum(len(proposal.mentions(key)) for key in FIELDS)
        result = combine(rules, accepted, version)
        if evidence_violations(result, texts):  # unreachable by construction; fail closed
            self.calls.append(replace(record, status="invalid", proposed=proposed))
            return unchanged
        model_fields = tuple(key for key in requested if result.fields[key].candidates)
        self.calls.append(
            replace(
                record,
                proposed=proposed,
                rejected=rejected + entity_rejected,
                entity_rejected=entity_rejected,
                model_fields=model_fields,
            )
        )
        return result


def _verified(key: FieldKey, proposed: ProposedMention, pages: dict[int, str]) -> _Mention | None:
    text = pages.get(proposed.page)
    quote = proposed.quote
    if text is None or not quote.strip() or len(quote) > MAX_QUOTE_CHARS:
        return None
    start = text.find(quote)
    if start < 0:
        return None
    raw = proposed.raw.strip()
    if key not in _NAME_FIELDS and raw.endswith("."):
        raw = raw[:-1].rstrip()  # as the rules: a full stop is not part of a date or amount
    offset = quote.find(raw) if raw else -1
    if offset < 0:
        return None
    page = Page(proposed.page, text)
    return _mention(page, key, raw, start + offset, start, start + len(quote))


def _record(response: ProviderResponse, requested: list[FieldKey], sha256: str) -> CallRecord:
    return CallRecord(
        status=response.status,
        requested=tuple(requested),
        request_sha256=sha256,
        model=response.model,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        latency_seconds=response.latency_seconds,
        cost_usd=response.cost_usd,
        error=response.error,
    )
