"""Model-assisted extraction (AI1, B'): verification and combination (synthetic text only).

The model is a scripted fake: these tests prove what the code does with any
answer, including fabricated, misplaced, oversized and malformed ones.
"""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from api.extraction import assisted
from api.extraction.assisted import (
    MAX_INPUT_CHARS,
    MAX_MENTIONS_PER_FIELD,
    MAX_QUOTE_CHARS,
    AssistedExtractor,
    Proposal,
    combine,
    extractor_version,
    verify,
)
from api.extraction.contract import FIELDS, Evidence, evidence_violations
from api.extraction.extract import Page, extract
from api.extraction.proposer import MODEL, OUTPUT_SCHEMA
from tests.fake_proposer import FakeProposer, answer, response

PAGE_1 = (
    "MASTER SERVICES AGREEMENT\n"
    "\n"
    "Supplier: Acme Widgets Ltd\n"
    "Buyer: Beta Foods plc\n"
    "Effective Date: 1 March 2026\n"
    "Fees: EUR 12,000.00 in total.\n"
)
PAGE_2 = "Termination: either party may end this agreement with one month's notice.\n"
PAGES = [Page(1, PAGE_1), Page(2, PAGE_2)]
TEXTS = {1: PAGE_1, 2: PAGE_2}
ASKED = [
    "provider_name",
    "customer_name",
    "end_date",
    "governing_law",
    "contract_value",
    "notice_period_days",
]


def mention(page: int, quote: str, raw: str) -> dict[str, Any]:
    return {"page": page, "quote": quote, "raw": raw}


SUPPLIER = mention(1, "Supplier: Acme Widgets Ltd", "Acme Widgets Ltd")


def run(*responses: Any, pages: list[Page] = PAGES) -> tuple[Any, AssistedExtractor, FakeProposer]:
    proposer = FakeProposer(*responses)
    extractor = AssistedExtractor(proposer)
    return extractor(pages), extractor, proposer


def test_only_fields_the_rules_left_not_found_are_asked() -> None:
    rules = extract(PAGES)
    assert rules.fields["effective_date"].status == "found"
    _, extractor, proposer = run(response(answer()))
    request = proposer.requests[0]
    assert '"requested_fields"' in request["messages"][0]["content"]
    assert list(extractor.calls[0].requested) == ASKED
    assert "effective_date" not in request["messages"][0]["content"].split('"pages"')[0]


def test_a_verified_mention_becomes_a_found_field_with_computed_offsets() -> None:
    result, extractor, _ = run(response(answer(provider_name=[SUPPLIER])))
    field = result.fields["provider_name"]
    assert field.status == "found"
    candidate = field.candidates[0]
    assert candidate.value == "Acme Widgets Ltd"
    start = PAGE_1.index("Supplier: Acme Widgets Ltd")
    assert candidate.evidence == [
        Evidence(page=1, start=start, end=start + 26, quote="Supplier: Acme Widgets Ltd")
    ]
    assert evidence_violations(result, TEXTS) == []
    call = extractor.calls[0]
    assert (call.status, call.proposed, call.rejected) == ("ok", 1, 0)
    assert call.model_fields == ("provider_name",)
    assert result.extractor == extractor_version()


@pytest.mark.parametrize(
    "bad",
    [
        mention(1, "Supplier: Acme Widgets Limited", "Acme Widgets Limited"),  # not in the text
        mention(2, "Supplier: Acme Widgets Ltd", "Acme Widgets Ltd"),  # wrong page
        mention(3, "Supplier: Acme Widgets Ltd", "Acme Widgets Ltd"),  # no such page
        mention(0, "Supplier: Acme Widgets Ltd", "Acme Widgets Ltd"),
        mention(1, "Supplier: Acme Widgets Ltd", "Gamma Ltd"),  # raw not inside the quote
        mention(1, "Supplier: Acme Widgets Ltd", "   "),  # empty raw
        mention(1, "Supplier:  Acme Widgets Ltd", "Acme Widgets Ltd"),  # whitespace changed
        mention(1, "Supplier: Acme Widgets Ltd Buyer: Beta Foods plc", "Acme Widgets Ltd"),
        mention(1, "", "Acme"),
        mention(1, "   ", " "),
        mention(1, "supplier: acme widgets ltd", "acme widgets ltd"),  # case changed
    ],
)
def test_a_mention_failing_any_check_is_rejected_and_never_a_claim(bad: dict[str, Any]) -> None:
    result, extractor, _ = run(response(answer(provider_name=[bad])))
    assert result.fields["provider_name"].status == "unknown"
    assert result.fields["provider_name"].reason == "not_found"
    assert result.model_dump(exclude={"extractor"}) == extract(PAGES).model_dump(
        exclude={"extractor"}
    )
    assert (extractor.calls[0].rejected, extractor.calls[0].model_fields) == (1, ())


def test_a_quote_over_the_length_cap_is_rejected_even_if_verbatim() -> None:
    long_page = "Supplier: " + "A" * MAX_QUOTE_CHARS + " Ltd\n"
    quote = long_page.rstrip("\n")
    result, extractor, _ = run(
        response(answer(provider_name=[mention(1, quote, "A" * MAX_QUOTE_CHARS + " Ltd")])),
        pages=[Page(1, long_page)],
    )
    assert result.fields["provider_name"].reason == "not_found"
    assert extractor.calls[0].rejected == 1


def test_the_model_can_never_change_a_field_the_rules_decided() -> None:
    planted = mention(1, "Effective Date: 1 March 2026", "1 March 2026")
    other = mention(2, "Termination: either party", "either party")
    result, extractor, _ = run(response(answer(effective_date=[planted], customer_name=[other])))
    assert result.fields["effective_date"] == extract(PAGES).fields["effective_date"]
    assert extractor.calls[0].rejected == 1  # the effective_date mention
    assert result.fields["customer_name"].status == "found"  # requested, verbatim: accepted


def test_combine_ignores_mentions_for_fields_the_rules_found() -> None:
    rules = extract(PAGES)
    proposal = Proposal.model_validate(
        answer(provider_name=[SUPPLIER], effective_date=[mention(1, "Buyer: Beta", "Beta")])
    )
    accepted, rejected = verify(proposal, TEXTS, FIELDS)  # as if every field were asked
    assert rejected == 0
    result = combine(rules, accepted, "assisted-test")
    assert result.fields["effective_date"] == rules.fields["effective_date"]
    assert result.fields["provider_name"].status == "found"


def test_different_values_are_a_conflict_and_nothing_is_chosen() -> None:
    page = "Supplier: Acme Widgets Ltd\nThe services are provided by Delta Systems GmbH.\n"
    second = mention(1, "provided by Delta Systems GmbH", "Delta Systems GmbH")
    result, _, _ = run(response(answer(provider_name=[SUPPLIER, second])), pages=[Page(1, page)])
    field = result.fields["provider_name"]
    assert field.status == "conflict"
    assert [c.value for c in field.candidates] == ["Acme Widgets Ltd", "Delta Systems GmbH"]
    assert result.outcome == "needs_review"


def test_an_unnormalizable_value_stays_unknown_with_its_evidence() -> None:
    notice = mention(2, "with one month's notice", "one month")
    result, _, _ = run(response(answer(notice_period_days=[notice])))
    field = result.fields["notice_period_days"]
    assert (field.status, field.reason) == ("unknown", "unparseable")
    assert field.candidates[0].value is None
    assert field.candidates[0].evidence[0].quote == "with one month's notice"


def test_values_come_from_our_normalizers_only() -> None:
    fees = mention(1, "Fees: EUR 12,000.00 in total.", "EUR 12,000.00")
    result, _, _ = run(response(answer(contract_value=[fees])))
    value = result.fields["contract_value"].candidates[0].value
    assert value is not None and not isinstance(value, str | int)
    assert (value.amount, value.currency) == ("12000.00", "EUR")


def test_a_final_full_stop_is_not_part_of_a_date_or_amount() -> None:
    page = "This agreement expires on 31 December 2027.\n"
    end = mention(1, "expires on 31 December 2027.", "31 December 2027.")
    result, _, _ = run(response(answer(end_date=[end])), pages=[Page(1, page)])
    assert result.fields["end_date"].candidates[0].value == "2027-12-31"
    assert result.fields["end_date"].candidates[0].raw == "31 December 2027"


def test_the_same_mention_twice_counts_once_and_one_value_keeps_every_span() -> None:
    page = "Supplier: Acme Widgets Ltd\nSigned for Acme Widgets Ltd.\n"
    again = mention(1, "Signed for Acme Widgets Ltd", "Acme Widgets Ltd")
    result, extractor, _ = run(
        response(answer(provider_name=[SUPPLIER, SUPPLIER, again])), pages=[Page(1, page)]
    )
    field = result.fields["provider_name"]
    assert field.status == "found"
    assert [e.quote for e in field.candidates[0].evidence] == [
        "Supplier: Acme Widgets Ltd",
        "Signed for Acme Widgets Ltd",
    ]
    assert extractor.calls[0].rejected == 0


@pytest.mark.parametrize("status", ["refused", "truncated", "invalid", "unavailable"])
def test_no_usable_answer_leaves_the_rules_result_and_is_recorded(status: Any) -> None:
    result, extractor, _ = run(response(status=status, error="APITimeoutError"))
    assert result.model_dump(exclude={"extractor"}) == extract(PAGES).model_dump(
        exclude={"extractor"}
    )
    assert extractor.calls[0].status == status


@pytest.mark.parametrize("status", ["refused", "truncated", "invalid", "unavailable"])
def test_text_with_a_failed_status_is_never_used(status: Any) -> None:
    failed = replace(response(answer(provider_name=[SUPPLIER])), status=status)
    assert failed.text is not None
    result, extractor, _ = run(failed)
    assert result.fields["provider_name"].reason == "not_found"
    assert extractor.calls[0].status == status


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        '{"provider_name": []}',  # fields missing
        '{"provider_name": [], "extra": 1}',
        json.dumps({**answer(), "provider_name": "none"}),  # wrong type
        '{"provider_name": [{"page": "1", "quote": "Supplier", "raw": "S"}]}',
    ],
)
def test_a_malformed_answer_is_invalid_as_a_whole(text: str) -> None:
    result, extractor, _ = run(response(text))
    assert extractor.calls[0].status == "invalid"
    assert result.fields["provider_name"].reason == "not_found"


def test_too_many_mentions_for_a_field_invalidate_the_whole_answer() -> None:
    good = answer(customer_name=[mention(1, "Buyer: Beta Foods plc", "Beta Foods plc")])
    good["provider_name"] = [SUPPLIER] * (MAX_MENTIONS_PER_FIELD + 1)
    result, extractor, _ = run(response(good))
    assert extractor.calls[0].status == "invalid"
    assert result.fields["customer_name"].reason == "not_found"  # no partial use


def test_a_mention_with_an_unknown_key_invalidates_the_answer() -> None:
    bad = answer(provider_name=[{**SUPPLIER, "start": 12}])
    _, extractor, _ = run(response(bad))
    assert extractor.calls[0].status == "invalid"


def test_no_call_when_nothing_is_missing_or_the_document_is_too_long() -> None:
    complete = Page(
        1,
        "Provider: Acme Ltd\nCustomer: Beta plc\nEffective Date: 1 March 2026\n"
        "End Date: 1 March 2027\nGoverning Law: England and Wales\n"
        "Contract Value: EUR 1,000.00\nNotice Period: 30 days\n",
    )
    result, extractor, proposer = run(pages=[complete])
    assert proposer.requests == [] and extractor.calls[0].status == "not_needed"
    assert result.fields == extract([complete]).fields
    _, extractor, proposer = run(pages=[Page(1, "x" * (MAX_INPUT_CHARS + 1))])
    assert proposer.requests == [] and extractor.calls[0].status == "input_too_long"


def test_the_request_is_the_pinned_configuration_with_the_document_as_data() -> None:
    _, _, proposer = run(response(answer()))
    request = proposer.requests[0]
    assert request["model"] == MODEL == "claude-sonnet-5-5"
    assert request["output_config"]["effort"] == "low"
    assert request["output_config"]["format"] == {"type": "json_schema", "schema": OUTPUT_SCHEMA}
    assert request["inference_geo"] == "global"
    assert not {"temperature", "top_p", "top_k", "tools", "tool_choice"} & set(request)
    assert request["system"] == assisted.ASSISTED_1.prompt.read_text()
    assert [m["role"] for m in request["messages"]] == ["user"]
    document = json.loads(request["messages"][0]["content"])
    assert document == {
        "requested_fields": ASKED,
        "pages": [{"page": 1, "text": PAGE_1}, {"page": 2, "text": PAGE_2}],
    }


def test_the_output_schema_is_strict_and_names_every_field() -> None:
    assert OUTPUT_SCHEMA["additionalProperties"] is False
    assert OUTPUT_SCHEMA["required"] == list(FIELDS)
    item = OUTPUT_SCHEMA["properties"]["provider_name"]["items"]
    assert item["additionalProperties"] is False
    assert item["required"] == ["page", "quote", "raw"]


def test_a_call_record_holds_no_document_text() -> None:
    _, extractor, _ = run(response(answer(provider_name=[SUPPLIER])))
    record = repr(extractor.calls[0])
    assert "Acme" not in record and "Supplier" not in record


def test_the_version_pins_the_prompt(tmp_path: Path) -> None:
    version = extractor_version()
    assert version == "assisted-1+beb688b3d3c9"  # historical: assisted-1 never changes
    assert version.startswith("assisted-1+") and len(version) <= 40
    changed = tmp_path / "assisted-1.md"
    changed.write_text(assisted.ASSISTED_1.prompt.read_text() + "\n")
    assert extractor_version(replace(assisted.ASSISTED_1, prompt=changed)) != version


# ---------------------------------------------------------------- assisted-2 party checks

DEFINED = (
    'MASTER AGREEMENT\n\nAshgrove Analytics Ltd ("Ashgrove") and Birchwood Foods plc '
    '("Birchwood") agree.\n\n1. Ashgrove will provide the services to Birchwood.\n'
)
WRAPPED = "AGREEMENT\n\nThis agreement is between Calderwood Health\nGmbH and Dunmore plc.\n"


def run2(page: str, **mentions: list[dict[str, Any]]) -> tuple[Any, AssistedExtractor]:
    proposer = FakeProposer(response(answer(**mentions)))
    extractor = AssistedExtractor(proposer, assisted.ASSISTED_2)
    return extractor([Page(1, page)]), extractor


def test_assisted_2_rejects_a_defined_short_name_that_assisted_1_accepts() -> None:
    short = mention(1, "Ashgrove will provide the services", "Ashgrove")
    full = mention(1, 'Ashgrove Analytics Ltd ("Ashgrove")', "Ashgrove Analytics Ltd")
    proposer = FakeProposer(response(answer(provider_name=[short])))
    old = AssistedExtractor(proposer)([Page(1, DEFINED)])
    assert old.fields["provider_name"].candidates[0].value == "Ashgrove"  # assisted-1, as measured
    result, extractor = run2(DEFINED, provider_name=[short])
    assert result.fields["provider_name"].reason == "not_found"
    assert (extractor.calls[0].rejected, extractor.calls[0].entity_rejected) == (1, 1)
    result, extractor = run2(DEFINED, provider_name=[full, short])
    assert result.fields["provider_name"].status == "found"
    assert result.fields["provider_name"].candidates[0].value == "Ashgrove Analytics Ltd"
    assert extractor.calls[0].entity_rejected == 1
    assert result.extractor.startswith("assisted-2+")


def test_assisted_2_rejects_a_truncated_name_and_keeps_the_wrapped_full_name() -> None:
    truncated = mention(1, "between Calderwood Health", "Calderwood Health")
    result, _ = run2(WRAPPED, provider_name=[truncated])
    assert result.fields["provider_name"].reason == "not_found"
    full = mention(1, "Calderwood Health\nGmbH", "Calderwood Health\nGmbH")
    result, extractor = run2(WRAPPED, provider_name=[full])
    assert result.fields["provider_name"].candidates[0].value == "Calderwood Health GmbH"
    assert extractor.calls[0].entity_rejected == 0


def test_assisted_2_rejects_a_trading_name() -> None:
    page = (
        "AGREEMENT\n\nServices by Eastwick Media Ltd, trading as Eastwick Direct, to Foxley AB.\n"
    )
    trade = mention(1, "trading as Eastwick Direct", "Eastwick Direct")
    result, extractor = run2(page, provider_name=[trade])
    assert result.fields["provider_name"].reason == "not_found"
    assert extractor.calls[0].entity_rejected == 1


def test_assisted_2_keeps_a_one_word_name_that_has_no_longer_form() -> None:
    page = "STATEMENT OF WORK\n\nService provider: Greystone\nClient: Hartwell\n"
    result, extractor = run2(
        page,
        provider_name=[mention(1, "Service provider: Greystone", "Greystone")],
        customer_name=[mention(1, "Client: Hartwell", "Hartwell")],
    )
    assert result.fields["provider_name"].candidates[0].value == "Greystone"
    assert result.fields["customer_name"].candidates[0].value == "Hartwell"
    assert extractor.calls[0].rejected == 0


def test_assisted_2_rejects_a_name_equal_to_the_other_partys() -> None:
    labelled = (
        "AGREEMENT\n\nCustomer: Kelmscott Optics Ltd\nThe supplier is Kelmscott Optics Ltd.\n"
    )
    same = mention(1, "The supplier is Kelmscott Optics Ltd", "Kelmscott Optics Ltd")
    result, extractor = run2(labelled, provider_name=[same])  # equals the rules' customer
    assert result.fields["provider_name"].reason == "not_found"
    assert result.fields["customer_name"].status == "found"  # the rules' result, untouched
    assert extractor.calls[0].entity_rejected == 1
    both = "AGREEMENT\n\nLindale Foods AB supplies and Lindale Foods AB buys.\n"
    result, extractor = run2(
        both,
        provider_name=[mention(1, "Lindale Foods AB supplies", "Lindale Foods AB")],
        customer_name=[mention(1, "Lindale Foods AB buys", "Lindale Foods AB")],
    )
    assert result.fields["provider_name"].reason == "not_found"
    assert result.fields["customer_name"].reason == "not_found"
    assert extractor.calls[0].entity_rejected == 2


def test_assisted_2_checks_only_party_names() -> None:
    page = 'AGREEMENT\n\nThe services start on 1 March 2026 (the "Start").\n'
    start = mention(1, "start on 1 March 2026", "1 March 2026")
    result, extractor = run2(page, effective_date=[start])
    assert result.fields["effective_date"].candidates[0].value == "2026-03-01"
    assert extractor.calls[0].entity_rejected == 0


def test_assisted_2_sends_its_own_prompt_with_the_same_configuration() -> None:
    proposer = FakeProposer(response(answer()))
    AssistedExtractor(proposer, assisted.ASSISTED_2)(PAGES)
    request = proposer.requests[0]
    assert request["system"] == assisted.ASSISTED_2.prompt.read_text()
    first = FakeProposer(response(answer()))
    AssistedExtractor(first)(PAGES)
    assert {k: v for k, v in request.items() if k != "system"} == {
        k: v for k, v in first.requests[0].items() if k != "system"
    }
    one, two = assisted.ASSISTED_1.prompt.read_text(), request["system"]
    added = [p for p in two.split("\n\n") if p not in one.split("\n\n")]
    assert len(added) == 1 and added[0].startswith("For provider_name and customer_name")
    assert two.replace(added[0] + "\n\n", "") == one


def test_profile_versions_are_distinct_and_record_the_verifier() -> None:
    assert extractor_version(assisted.ASSISTED_1) == "assisted-1+beb688b3d3c9"
    assert extractor_version(assisted.ASSISTED_2).startswith("assisted-2+")
    unverified = replace(assisted.ASSISTED_2, verifier=None)
    assert extractor_version(unverified) != extractor_version(assisted.ASSISTED_2)
