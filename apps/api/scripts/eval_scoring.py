"""Scoring and gates for the DI-1 extraction evaluation (EVALS.md).

Pure functions: expected outputs (corpus/expected/extract/*.json) against what
the pipeline produced for the same case. Expected evidence is registered as
`{page, text, occurrence}` (the raw value as written, and which occurrence on
the page), so expectations never depend on an extractor's choice of quote: a
returned span covers it if it contains that occurrence.

Gates:
- Integrity, both splits (hard): schema-valid results, every span resolving to
  its page text, no unsupported claims, exact failure codes.
- Accuracy, dev (hard): outcome, field and evidence completeness 100 %,
  abstention and conflict precision/recall 1.0, no silent conflicts, no wrong
  values.
- Accuracy, each held-out split (`heldout`, `heldout-2`): no regression against
  its committed baseline. Held-out accuracy is measured, never gated to 100 %
  (that would force tuning on it).

`wrong_values` counts accepted values (of found or conflicting fields) that the
expected output does not list for that field, such as a planted or invented
value quoted verbatim: evidence checks cannot catch those. `wrong_party_names`
counts the wrong values of provider_name and customer_name (a subset of
`wrong_values`; heldout-3 pre-registers it at 0 for assisted-2).
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from api.extraction.contract import ExtractionResult, evidence_violations

HIGHER_IS_BETTER = (
    "outcome_accuracy",
    "field_accuracy",
    "evidence_completeness",
    "abstention_precision",
    "abstention_recall",
    "conflict_precision",
    "conflict_recall",
)
LOWER_IS_BETTER = ("silent_conflicts", "wrong_values", "wrong_party_names")
PARTY_FIELDS = frozenset({"provider_name", "customer_name"})


@dataclass(frozen=True)
class Actual:
    """What the pipeline produced: a failure code, or an extraction result and its pages."""

    failure_code: str | None
    result: Mapping[str, Any] | None
    pages: Mapping[int, str]


@dataclass
class CaseScore:
    case: str
    split: str
    outcome_ok: bool = False
    failure_expected: bool = False
    failure_ok: bool = False
    schema_checked: bool = False
    schema_valid: bool = False
    spans_total: int = 0
    spans_resolved: int = 0
    unsupported: int = 0
    fields_total: int = 0
    fields_correct: int = 0
    evidence_expected: int = 0
    evidence_covered: int = 0
    silent_conflicts: int = 0
    wrong_values: int = 0
    wrong_party_names: int = 0
    abstention: list[int] = field(default_factory=lambda: [0, 0, 0])  # tp, fp, fn
    conflict: list[int] = field(default_factory=lambda: [0, 0, 0])
    problems: list[str] = field(default_factory=list)


def score_case(expected: Mapping[str, Any], actual: Actual) -> CaseScore:
    score = CaseScore(case=expected["case"], split=expected["split"])
    if expected["outcome"] == "failed":
        score.failure_expected = True
        score.failure_ok = actual.failure_code == expected["failure_code"]
        score.outcome_ok = score.failure_ok
        if not score.failure_ok:
            got = actual.failure_code or "a result"
            score.problems.append(f"expected failure {expected['failure_code']}, got {got}")
        return score

    fields_expected: Mapping[str, Any] = expected["fields"]
    score.fields_total = len(fields_expected)
    for field_expected in fields_expected.values():
        for candidate in field_expected["candidates"]:
            score.evidence_expected += len(candidate["evidence"])
    if actual.result is None:
        score.problems.append(f"expected {expected['outcome']}, got failure {actual.failure_code}")
        for field_expected in fields_expected.values():
            _count_labels(score, field_expected["status"], None)
        return score

    score.schema_checked = True
    try:
        result = ExtractionResult.model_validate(json.loads(json.dumps(actual.result)))
    except ValidationError:
        score.problems.append("result does not match the service_agreement.v1 schema")
        return score
    score.schema_valid = True
    score.outcome_ok = result.outcome == expected["outcome"]
    if not score.outcome_ok:
        score.problems.append(f"outcome {result.outcome}, expected {expected['outcome']}")

    violations = evidence_violations(result, actual.pages)
    spans = [s for f in result.fields.values() for c in f.candidates for s in c.evidence]
    score.spans_total = len(spans)
    score.spans_resolved = sum(
        1 for s in spans if actual.pages.get(s.page, "")[s.start : s.end] == s.quote
    )
    score.unsupported = sum(1 for v in violations if "raw value not inside" in v) + (
        score.spans_total - score.spans_resolved
    )
    score.problems.extend(violations)

    for key, field_expected in fields_expected.items():
        field_actual = result.fields[key]  # type: ignore[index]
        actual_json = field_actual.model_dump(mode="json")
        if _field_matches(field_expected, actual_json):
            score.fields_correct += 1
        else:
            score.problems.append(
                f"{key}: expected {field_expected['status']}/{field_expected['reason']}, "
                f"got {field_actual.status}/{field_actual.reason}"
                + (
                    " (values differ)"
                    if (field_expected["status"], field_expected["reason"])
                    == (field_actual.status, field_actual.reason)
                    else ""
                )
            )
        if field_expected["status"] == "conflict" and field_actual.status == "found":
            score.silent_conflicts += 1
        allowed = set(_values(field_expected["candidates"]))
        for candidate in actual_json["candidates"]:
            value = json.dumps(candidate["value"], sort_keys=True)
            if candidate["value"] is not None and value not in allowed:
                score.wrong_values += 1
                score.wrong_party_names += key in PARTY_FIELDS
                score.problems.append(f"{key}: value not in the expected output")
        _count_labels(score, field_expected["status"], field_actual.status)
        score.evidence_covered += _covered(field_expected, actual_json, actual.pages)
    return score


def summarize(scores: Sequence[CaseScore]) -> dict[str, Any]:
    def ratio(numerator: int, denominator: int) -> float | None:
        return round(numerator / denominator, 4) if denominator else None

    def total(name: str) -> int:
        return sum(getattr(s, name) for s in scores)

    abstention = [sum(s.abstention[i] for s in scores) for i in range(3)]
    conflict = [sum(s.conflict[i] for s in scores) for i in range(3)]
    checked = [s for s in scores if s.schema_checked]
    return {
        "cases": len(scores),
        "failure_cases": sum(1 for s in scores if s.failure_expected),
        "outcome_accuracy": ratio(sum(s.outcome_ok for s in scores), len(scores)),
        "failure_code_accuracy": ratio(
            sum(s.failure_ok for s in scores), sum(s.failure_expected for s in scores)
        ),
        "schema_valid_rate": ratio(sum(s.schema_valid for s in checked), len(checked)),
        "citation_resolution": ratio(total("spans_resolved"), total("spans_total")),
        "unsupported_claims": total("unsupported"),
        "field_accuracy": ratio(total("fields_correct"), total("fields_total")),
        "evidence_completeness": ratio(total("evidence_covered"), total("evidence_expected")),
        "silent_conflicts": total("silent_conflicts"),
        "wrong_values": total("wrong_values"),
        "wrong_party_names": total("wrong_party_names"),
        "abstention_precision": ratio(abstention[0], abstention[0] + abstention[1]),
        "abstention_recall": ratio(abstention[0], abstention[0] + abstention[2]),
        "conflict_precision": ratio(conflict[0], conflict[0] + conflict[1]),
        "conflict_recall": ratio(conflict[0], conflict[0] + conflict[2]),
    }


def gate(
    metrics: Mapping[str, Mapping[str, Any]],
    baselines: Mapping[str, Mapping[str, Any] | None],
    *,
    compare_baseline: bool = True,
) -> list[str]:
    """Every failed gate, as a message; empty means the evaluation passes.

    `baselines` maps each held-out split to its committed baseline (or None).
    `compare_baseline=False` only when writing a new baseline: the hard gates
    still apply, the held-out comparison does not.
    """
    failures: list[str] = []
    for split, values in metrics.items():
        for name, required in (
            ("schema_valid_rate", 1.0),
            ("citation_resolution", 1.0),
            ("failure_code_accuracy", 1.0),
        ):
            if values[name] is not None and values[name] != required:
                failures.append(f"{split}: {name} {values[name]} (must be {required})")
        if values["unsupported_claims"] != 0:
            failures.append(
                f"{split}: {values['unsupported_claims']} unsupported claims (must be 0)"
            )
    dev = metrics.get("dev")
    if dev is not None:
        for name in HIGHER_IS_BETTER:
            if dev[name] is not None and dev[name] != 1.0:
                failures.append(f"dev: {name} {dev[name]} (must be 1.0)")
        for name in LOWER_IS_BETTER:
            if dev[name] != 0:
                failures.append(f"dev: {dev[name]} {name.replace('_', ' ')} (must be 0)")
    if not compare_baseline:
        return failures
    for split, values in metrics.items():
        if not split.startswith("heldout"):
            continue
        baseline = baselines.get(split)
        if baseline is None:
            failures.append(f"{split}: no committed baseline")
            continue
        reference = baseline["metrics"]
        for name in HIGHER_IS_BETTER:
            if reference.get(name) is not None and (
                values[name] is None or values[name] < reference[name]
            ):
                failures.append(f"{split}: {name} {values[name]} regressed below {reference[name]}")
        for name in LOWER_IS_BETTER:
            # A baseline written before a metric existed is not compared on it.
            if name in reference and values[name] > reference[name]:
                failures.append(f"{split}: {name} {values[name]} regressed above {reference[name]}")
    return failures


def _values(candidates: Sequence[Mapping[str, Any]]) -> list[str]:
    return sorted(json.dumps(c["value"], sort_keys=True) for c in candidates)


def _field_matches(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> bool:
    return (
        expected["status"] == actual["status"]
        and expected["reason"] == actual["reason"]
        and _values(expected["candidates"]) == _values(actual["candidates"])
    )


def _covered(
    expected: Mapping[str, Any], actual: Mapping[str, Any], pages: Mapping[int, str]
) -> int:
    covered = 0
    for candidate in expected["candidates"]:
        value = json.dumps(candidate["value"], sort_keys=True)
        spans = [
            span
            for other in actual["candidates"]
            if json.dumps(other["value"], sort_keys=True) == value
            for span in other["evidence"]
        ]
        for evidence in candidate["evidence"]:
            start = _occurrence(
                pages.get(evidence["page"], ""), evidence["text"], evidence["occurrence"]
            )
            end = start + len(evidence["text"])
            if start >= 0 and any(
                s["page"] == evidence["page"] and s["start"] <= start and end <= s["end"]
                for s in spans
            ):
                covered += 1
    return covered


def _occurrence(text: str, needle: str, occurrence: int) -> int:
    position = -1
    for _ in range(occurrence):
        position = text.find(needle, position + 1)
        if position < 0:
            return -1
    return position


def _count_labels(score: CaseScore, expected: str, actual: str | None) -> None:
    for counts, label in ((score.abstention, "unknown"), (score.conflict, "conflict")):
        if expected == label and actual == label:
            counts[0] += 1
        elif actual == label:
            counts[1] += 1
        elif expected == label:
            counts[2] += 1
