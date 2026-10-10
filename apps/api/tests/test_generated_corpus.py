"""The generated splits' expected outputs (assist-dev, heldout-3) are sound.

They are written by corpus/generated.py from its own parameters; these checks
hold them to the parser's actual text and to the contract, independently of
any extractor: every evidence occurs on its page, every value is what our
normalizers make of the text it cites, statuses and reasons are consistent,
and the outcome follows from the fields.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from api.extraction.contract import FIELDS, REQUIRED, Money
from api.extraction.extract import NORMALIZERS

CORPUS = Path(__file__).resolve().parents[3] / "corpus"
GENERATED_SPLITS = {"assist-dev", "heldout-3"}
CASES = sorted(
    case["id"]
    for case in json.loads((CORPUS / "manifest.json").read_text())["cases"]
    if case["split"] in GENERATED_SPLITS
)


def _occurs(text: str, needle: str, occurrence: int) -> bool:
    position = -1
    for _ in range(occurrence):
        position = text.find(needle, position + 1)
        if position < 0:
            return False
    return True


def test_both_generated_splits_are_present() -> None:
    assert len([c for c in CASES if c.startswith("ad-")]) == 10
    assert len([c for c in CASES if c.startswith("h3-")]) == 24


@pytest.mark.parametrize("case_id", CASES)
def test_generated_expectation_is_sound(case_id: str) -> None:
    expected: dict[str, Any] = json.loads(
        (CORPUS / "expected" / "extract" / f"{case_id}.json").read_text()
    )
    parsed = json.loads((CORPUS / "expected" / "parse" / f"{case_id}.json").read_text())
    assert parsed["ok"] is True
    pages = {page["number"]: page["text"] for page in parsed["pages"]}
    assert all(page["off_page_chars"] == 0 for page in parsed["pages"])
    assert set(expected["fields"]) == set(FIELDS)
    for key, field in expected["fields"].items():
        status, reason, candidates = field["status"], field["reason"], field["candidates"]
        values = [c["value"] for c in candidates]
        if status == "found":
            assert reason is None and len(candidates) == 1 and values[0] is not None, key
        elif status == "conflict":
            assert reason is None and len(candidates) >= 2, key
            assert len({json.dumps(v, sort_keys=True) for v in values}) == len(values), key
        else:
            assert status == "unknown", key
            if reason == "not_found":
                assert candidates == [], key
            else:
                assert reason in ("unparseable", "ambiguous") and values == [None], key
        for candidate in candidates:
            assert candidate["evidence"], key
            for evidence in candidate["evidence"]:
                text = pages[evidence["page"]]
                assert _occurs(text, evidence["text"], evidence["occurrence"]), (key, evidence)
                value, why = NORMALIZERS[key](evidence["text"])
                actual = value.model_dump() if isinstance(value, Money) else value
                assert actual == candidate["value"], (key, evidence["text"])
                assert (why or None) == (reason if candidate["value"] is None else None), key
    review = any(f["status"] == "conflict" for f in expected["fields"].values()) or any(
        expected["fields"][key]["status"] != "found" for key in REQUIRED
    )
    assert expected["outcome"] == ("needs_review" if review else "completed")
