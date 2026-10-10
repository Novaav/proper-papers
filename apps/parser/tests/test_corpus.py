"""Every corpus case parses to its committed expectation (host run of the same code)."""

import hashlib
import json
from pathlib import Path

import pytest
from cases import CASES, Case

from docparser.output import failure, success
from docparser.parse import ParseFailure, parse

EXPECTED = Path(__file__).resolve().parents[3] / "corpus" / "expected" / "parse"


def _result(data: bytes) -> dict[str, object]:
    try:
        return success(parse(data))
    except ParseFailure as error:
        return failure(error.code)


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_case_matches_expectation(case: Case) -> None:
    expected = json.loads((EXPECTED / f"{case.id}.json").read_text())
    assert _result(case.make()) == expected


def test_every_expectation_belongs_to_a_case() -> None:
    assert sorted(path.stem for path in EXPECTED.glob("*.json")) == sorted(c.id for c in CASES)


def test_both_splits_and_all_planned_categories_are_present() -> None:
    assert {c.split for c in CASES} == {"dev", "heldout", "heldout-2", "assist-dev", "heldout-3"}
    assert {c.category for c in CASES} >= {
        "normal",
        "duplicate",
        "missing",
        "ambiguous",
        "unparseable",
        "conflict",
        "malformed",
        "adversarial",
    }


def test_heldout_2_matches_its_preregistration() -> None:
    """heldout-2 is frozen (AI1): its cases, coverage and expected outputs never change."""
    root = Path(__file__).resolve().parents[3] / "corpus"
    record = json.loads((root / "eval" / "heldout-2-preregistration.json").read_text())
    cases = sorted(c.id for c in CASES if c.split == "heldout-2")
    assert cases == record["cases"]
    assert len(cases) == record["case_count"] == 16
    by_category: dict[str, list[str]] = {}
    for case in CASES:
        if case.split == "heldout-2":
            by_category.setdefault(case.category, []).append(case.id)
    assert {k: sorted(v) for k, v in by_category.items()} == record["coverage"]
    lines = ""
    for case_id in cases:
        data = (root / "expected" / "extract" / f"{case_id}.json").read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        assert digest == record["expected_extract_sha256"][case_id], case_id
        lines += f"{case_id}:{digest}\n"
    assert hashlib.sha256(lines.encode()).hexdigest() == record["expected_extract_digest"]


# The pre-registration itself (thresholds, protocol, coverage) is frozen too: changing either
# file needs a reviewed change of these digests.
PREREGISTRATION_SHA256 = {
    "heldout-2-preregistration.json": (
        "2f2d2b6294686bfcfe30803e8c452efea87b1d1f69cba84bd8c7c3823d284149"
    ),
    "heldout-2-preregistration.md": (
        "74fdfa9ea7b4166cc2ae22caae8cebf25f14fb4aec5c07375485aa10bf0e7845"
    ),
}


def test_heldout_2_preregistration_is_unchanged() -> None:
    root = Path(__file__).resolve().parents[3] / "corpus" / "eval"
    for name, digest in PREREGISTRATION_SHA256.items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest, name


def test_heldout_3_matches_its_preregistration() -> None:
    """heldout-3 is frozen (assisted-2): its cases, coverage, PDFs and expected outputs."""
    root = Path(__file__).resolve().parents[3] / "corpus"
    record = json.loads((root / "eval" / "heldout-3-preregistration.json").read_text())
    cases = sorted(c.id for c in CASES if c.split == "heldout-3")
    assert cases == record["cases"]
    assert len(cases) == record["case_count"] == 24
    by_category: dict[str, list[str]] = {}
    for case in CASES:
        if case.split == "heldout-3":
            by_category.setdefault(case.category, []).append(case.id)
            assert hashlib.sha256(case.make()).hexdigest() == record["pdf_sha256"][case.id]
    assert {k: sorted(v) for k, v in by_category.items()} == record["coverage"]
    lines = ""
    for case_id in cases:
        data = (root / "expected" / "extract" / f"{case_id}.json").read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        assert digest == record["expected_extract_sha256"][case_id], case_id
        lines += f"{case_id}:{digest}\n"
    assert hashlib.sha256(lines.encode()).hexdigest() == record["expected_extract_digest"]
    assert record["assist_dev_cases"] == sorted(c.id for c in CASES if c.split == "assist-dev")


# The heldout-3 specification and pre-registration are frozen: changing any of these files
# needs a reviewed change of its digest.
HELDOUT_3_SHA256 = {
    "heldout-3-preregistration.json": (
        "4858a1e13027d5340cc55d3ad4402a10747a373f3c38b0c118363b91c5521d18"
    ),
    "heldout-3-preregistration.md": (
        "9566fbce60af4801f6b1f26b35a03ef974f91970aae4f9df4a3a605beaf17ae2"
    ),
    "heldout-3-spec.json": ("bbde95ea4ec0dae27222d252370f31497d6f267bab7fa1cd6ddcaecbd5bf2020"),
    "heldout-3-spec.md": ("694d63fd97e941c8e6e2a7d3920b57fe8babfb2243e0a0ffe57b58bbace14fba"),
}


def test_heldout_3_specification_and_preregistration_are_unchanged() -> None:
    root = Path(__file__).resolve().parents[3] / "corpus" / "eval"
    for name, digest in HELDOUT_3_SHA256.items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest, name
    record = json.loads((root / "heldout-3-preregistration.json").read_text())
    for name, digest in record["spec_sha256"].items():
        assert HELDOUT_3_SHA256[name] == digest, name
