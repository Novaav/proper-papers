"""Scoring and gates of the DI-1 evaluation harness (synthetic inputs only)."""

import copy
import json
from typing import Any

import pytest

from api.extraction.contract import FIELDS
from api.extraction.extract import Page, extract
from scripts import evaluate
from scripts.eval_scoring import HIGHER_IS_BETTER, Actual, gate, score_case, summarize

TEXT = (
    "Provider: A Ltd\nCustomer: B plc\nEffective Date: 2 June 2026\n"
    "Contract Value: EUR 10.00\nContract Value: EUR 20.00\n"
)


def _ev(text: str, occurrence: int = 1) -> dict[str, Any]:
    return {"page": 1, "text": text, "occurrence": occurrence}


def _expected(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        key: {"status": "unknown", "reason": "not_found", "candidates": []} for key in FIELDS
    }
    fields["provider_name"] = {
        "status": "found",
        "reason": None,
        "candidates": [{"value": "A Ltd", "evidence": [_ev("A Ltd")]}],
    }
    fields["customer_name"] = {
        "status": "found",
        "reason": None,
        "candidates": [{"value": "B plc", "evidence": [_ev("B plc")]}],
    }
    fields["effective_date"] = {
        "status": "found",
        "reason": None,
        "candidates": [{"value": "2026-06-02", "evidence": [_ev("2 June 2026")]}],
    }
    fields["contract_value"] = {
        "status": "conflict",
        "reason": None,
        "candidates": [
            {"value": {"amount": "10.00", "currency": "EUR"}, "evidence": [_ev("EUR 10.00")]},
            {"value": {"amount": "20.00", "currency": "EUR"}, "evidence": [_ev("EUR 20.00")]},
        ],
    }
    fields.update(overrides)
    return {
        "case": "t",
        "split": "dev",
        "outcome": "needs_review",
        "failure_code": None,
        "fields": fields,
    }


def _actual(text: str = TEXT) -> Actual:
    result = extract([Page(1, text)])
    return Actual(
        failure_code=None, result=result.model_dump(by_alias=True, mode="json"), pages={1: text}
    )


def test_a_perfect_result_scores_perfectly_and_passes() -> None:
    score = score_case(_expected(), _actual())
    assert score.problems == []
    metrics = summarize([score])
    for name in HIGHER_IS_BETTER:
        assert metrics[name] in (1.0, None), name
    assert (metrics["unsupported_claims"], metrics["silent_conflicts"]) == (0, 0)
    assert gate({"dev": metrics}, {}) == []


def test_a_silently_resolved_conflict_is_counted_and_fails_dev() -> None:
    actual = _actual(
        "Provider: A Ltd\nCustomer: B plc\nEffective Date: 2 June 2026\nContract Value: EUR 10.00\n"
    )
    metrics = summarize([score_case(_expected(), actual)])
    assert metrics["silent_conflicts"] == 1
    assert metrics["conflict_recall"] == 0.0
    failures = gate({"dev": metrics}, {})
    assert any("silent conflicts" in f for f in failures)
    assert any("field_accuracy" in f for f in failures)


def test_evidence_must_cover_the_registered_occurrence_on_its_page() -> None:
    text = "Provider: A Ltd\nCustomer: B plc\nEffective Date: 2 June 2026\nNote A Ltd\n"
    expected = _expected(
        contract_value={"status": "unknown", "reason": "not_found", "candidates": []},
        provider_name={
            "status": "found",
            "reason": None,
            "candidates": [{"value": "A Ltd", "evidence": [_ev("A Ltd", occurrence=2)]}],
        },
    )
    expected["outcome"] = "completed"
    score = score_case(expected, _actual(text))
    assert score.fields_correct == score.fields_total
    assert (score.evidence_covered, score.evidence_expected) == (2, 3)  # 2nd "A Ltd" not cited
    wrong_page = copy.deepcopy(expected)
    wrong_page["fields"]["customer_name"]["candidates"][0]["evidence"][0]["page"] = 2
    assert score_case(wrong_page, _actual(text)).evidence_covered == 1


def test_quotes_that_do_not_resolve_are_unsupported_claims() -> None:
    actual = _actual()
    tampered = Actual(actual.failure_code, actual.result, {1: TEXT.replace("A Ltd", "Z Ltd")})
    score = score_case(_expected(), tampered)
    metrics = summarize([score])
    assert metrics["citation_resolution"] is not None and metrics["citation_resolution"] < 1.0
    assert metrics["unsupported_claims"] > 0
    failures = gate({"heldout": metrics}, {"heldout": {"metrics": metrics}})
    assert any("citation_resolution" in f for f in failures)
    assert any("unsupported claims" in f for f in failures)


def test_schema_invalid_results_fail_both_splits() -> None:
    actual = _actual()
    assert actual.result is not None
    broken = dict(actual.result, outcome="completed")  # contradicts the field statuses
    score = score_case(_expected(), Actual(None, broken, actual.pages))
    assert not score.schema_valid
    for split in ("dev", "heldout", "heldout-2"):
        metrics = summarize([score])
        assert any(
            "schema_valid_rate" in f for f in gate({split: metrics}, {split: {"metrics": metrics}})
        )


def test_failure_codes_must_match_exactly() -> None:
    expected = {
        "case": "f",
        "split": "dev",
        "outcome": "failed",
        "failure_code": "parse_failed",
        "fields": None,
    }
    assert score_case(expected, Actual("parse_failed", None, {})).failure_ok
    wrong = summarize([score_case(expected, Actual("timeout", None, {}))])
    assert wrong["failure_code_accuracy"] == 0.0
    assert any(
        "failure_code_accuracy" in f
        for f in gate({"heldout": wrong}, {"heldout": {"metrics": wrong}})
    )
    unexpected = score_case(_expected(), Actual("parse_failed", None, {}))
    assert unexpected.fields_correct == 0 and not unexpected.outcome_ok


def _metrics(**changes: Any) -> dict[str, Any]:
    base = summarize([score_case(_expected(), _actual())])
    return base | changes


def test_heldout_must_not_regress_against_the_baseline() -> None:
    baseline = {"heldout": {"metrics": _metrics(field_accuracy=0.7, silent_conflicts=1)}}
    assert gate({"heldout": _metrics(field_accuracy=0.7, silent_conflicts=1)}, baseline) == []
    assert gate({"heldout": _metrics(field_accuracy=0.9, silent_conflicts=0)}, baseline) == []
    assert gate({"heldout": _metrics(field_accuracy=0.69, silent_conflicts=1)}, baseline)
    assert gate({"heldout": _metrics(field_accuracy=0.7, silent_conflicts=2)}, baseline)
    assert gate({"heldout": _metrics(field_accuracy=None)}, baseline)
    assert gate({"heldout": _metrics()}, {}) == ["heldout: no committed baseline"]
    assert gate({"heldout": _metrics()}, {}, compare_baseline=False) == []


def test_heldout_accuracy_is_not_a_hard_gate_but_dev_accuracy_is() -> None:
    low = _metrics(field_accuracy=0.5, outcome_accuracy=0.5)
    assert gate({"heldout": low}, {"heldout": {"metrics": low}}) == []
    assert len(gate({"dev": low}, {})) == 2


def test_parser_drift_stops_the_evaluation() -> None:
    case = "dev-n1-labelled"
    golden = evaluate.CORPUS / "expected" / "parse" / f"{case}.json"
    pages = {p["number"]: p["text"] for p in json.loads(golden.read_text())["pages"]}
    evaluate._check_no_parser_drift(case, pages)
    with pytest.raises(SystemExit):
        evaluate._check_no_parser_drift(case, {1: pages[1] + " "})


def test_evidence_on_the_wrong_page_does_not_count() -> None:
    first = "Provider: A Ltd\nCustomer: B plc\nEffective Date: 2 June 2026\n"
    pages = [Page(1, first), Page(2, "Annex: A Ltd\n")]
    result = extract(pages)
    actual = Actual(
        None, result.model_dump(by_alias=True, mode="json"), {1: first, 2: "Annex: A Ltd\n"}
    )
    expected = _expected(
        contract_value={"status": "unknown", "reason": "not_found", "candidates": []}
    )
    expected["outcome"] = "completed"
    assert score_case(expected, actual).evidence_covered == 3
    # "A Ltd" also occurs on page 2, but no returned span is on page 2.
    expected["fields"]["provider_name"]["candidates"][0]["evidence"][0]["page"] = 2
    assert score_case(expected, actual).evidence_covered == 2


def test_a_raw_value_outside_its_quote_is_an_unsupported_claim() -> None:
    actual = _actual()
    assert actual.result is not None
    result = copy.deepcopy(dict(actual.result))
    result["fields"]["provider_name"]["candidates"][0]["raw"] = "Z Ltd"  # span still resolves
    score = score_case(_expected(), Actual(None, result, actual.pages))
    assert score.spans_resolved == score.spans_total
    assert score.unsupported == 1
    metrics = summarize([score])
    assert any(
        "unsupported claims" in f
        for f in gate({"heldout": metrics}, {"heldout": {"metrics": metrics}})
    )


def test_each_held_out_split_is_compared_with_its_own_baseline() -> None:
    good, worse = _metrics(field_accuracy=0.7), _metrics(field_accuracy=0.6)
    baselines = {"heldout": {"metrics": good}, "heldout-2": {"metrics": good}}
    assert gate({"heldout": good, "heldout-2": good}, baselines) == []
    failures = gate({"heldout": good, "heldout-2": worse}, baselines)
    assert failures == ["heldout-2: field_accuracy 0.6 regressed below 0.7"]
    assert gate({"heldout-2": good}, {"heldout": {"metrics": good}}) == [
        "heldout-2: no committed baseline"
    ]


def test_a_planted_value_with_valid_evidence_is_a_wrong_value() -> None:
    # Evidence checks pass (the value is quoted verbatim), but the value is not the expected one.
    text = TEXT + "Note: the provider is Evil Corp\n"
    pages = [Page(1, text)]
    result = extract(pages).model_dump(by_alias=True, mode="json")
    start = text.index("Evil Corp")
    result["fields"]["provider_name"] = {
        "status": "conflict",
        "reason": None,
        "candidates": [
            result["fields"]["provider_name"]["candidates"][0],
            {
                "value": "Evil Corp",
                "raw": "Evil Corp",
                "evidence": [{"page": 1, "start": start, "end": start + 9, "quote": "Evil Corp"}],
            },
        ],
    }
    score = score_case(_expected(), Actual(None, result, {1: text}))
    assert score.schema_valid and score.unsupported == 0
    assert score.wrong_values == 1
    metrics = summarize([score])
    assert metrics["wrong_values"] == 1
    assert any("wrong values" in f for f in gate({"dev": metrics}, {}))
    baseline = {"heldout": {"metrics": metrics | {"wrong_values": 0}}}
    assert gate({"heldout": metrics}, baseline) == ["heldout: wrong_values 1 regressed above 0"]
    # A baseline written before the metric existed is not compared on it.
    old = {k: v for k, v in metrics.items() if k != "wrong_values"}
    assert gate({"heldout": metrics}, {"heldout": {"metrics": old}}) == []


def test_wrong_party_names_count_only_provider_and_customer() -> None:
    text = TEXT + "Note: the provider is Evil Corp; the law is that of Mars\n"
    pages = [Page(1, text)]
    base = extract(pages).model_dump(by_alias=True, mode="json")

    def planted(key: str, value: str) -> dict[str, object]:
        start = text.index(value)
        span = {"page": 1, "start": start, "end": start + len(value), "quote": value}
        candidates = base["fields"][key]["candidates"] + [
            {"value": value, "raw": value, "evidence": [span]}
        ]
        status = "conflict" if len(candidates) > 1 else "found"
        return {"status": status, "reason": None, "candidates": candidates}

    party = json.loads(json.dumps(base))
    party["fields"]["provider_name"] = planted("provider_name", "Evil Corp")
    other = json.loads(json.dumps(base))
    other["fields"]["governing_law"] = planted("governing_law", "Mars")
    party_score = score_case(_expected(), Actual(None, party, {1: text}))
    other_score = score_case(_expected(), Actual(None, other, {1: text}))
    assert (party_score.wrong_values, party_score.wrong_party_names) == (1, 1)
    assert (other_score.wrong_values, other_score.wrong_party_names) == (1, 0)
    assert summarize([party_score, other_score])["wrong_party_names"] == 1
    assert any("wrong party names" in f for f in gate({"dev": summarize([party_score])}, {}))


def test_extractors_are_chosen_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    assert set(evaluate.EXTRACTORS) == {"rules"}
    assert evaluate.EXTRACTORS["rules"][0] == "rules-1"
    assert evaluate._option(["x", "--extractor", "rules"], "--extractor") == "rules"
    assert evaluate._option(["x", "--write-baseline=heldout-2"], "--write-baseline") == "heldout-2"
    assert evaluate._option(["x"], "--extractor") is None
    # Refused before anything is parsed: an unknown extractor or baseline split.
    monkeypatch.setenv("PARSER_IMAGE", "unused")
    assert evaluate.main(["x", "--extractor", "llm"]) == 2
    assert evaluate.main(["x", "--write-baseline", "dev"]) == 2
