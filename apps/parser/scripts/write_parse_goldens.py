"""Write corpus/expected/parse/<case>.json from the current parser (a reviewed change).

    uv run --locked python scripts/write_parse_goldens.py

The files are the parse expectations checked by tests/test_corpus.py (on the
host) and by apps/api/tests/test_parser_sandbox.py (through the real sandbox).
Never run in CI: a changed expectation must appear as a diff for review.
"""

import json
import sys
from pathlib import Path

from docparser.output import failure, success
from docparser.parse import ParseFailure, parse

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "corpus"))

from cases import CASES  # noqa: E402

EXPECTED = ROOT / "corpus" / "expected" / "parse"


def expected(data: bytes) -> dict[str, object]:
    try:
        return success(parse(data))
    except ParseFailure as error:
        return failure(error.code)


def main() -> None:
    EXPECTED.mkdir(parents=True, exist_ok=True)
    for case in CASES:
        text = json.dumps(expected(case.make()), indent=2, ensure_ascii=False, sort_keys=True)
        (EXPECTED / f"{case.id}.json").write_text(text + "\n")
    print(f"wrote {len(CASES)} parse expectations to {EXPECTED}")


if __name__ == "__main__":
    main()
