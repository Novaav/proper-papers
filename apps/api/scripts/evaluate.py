"""DI-1 extraction evaluation through the real parser sandbox (make eval).

    make eval                     # PARSER_IMAGE=<image> uv run --locked python -m scripts.evaluate
    make eval-baseline SPLIT=<s>  # also rewrite that held-out split's baseline (reviewed)

`--extractor NAME` chooses the extractor to score (default and only: `rules`).
The model-assisted extractor (AI1) needs a model call per document and is
scored by scripts/eval_assisted.py (make eval-assisted) on the same corpus,
through this module, with the same gates.

For every corpus case: build the PDF (corpus/cases.py, byte-checked against
corpus/manifest.json), parse it in the isolated sandbox (never in process),
check the parsed text against corpus/expected/parse (parser drift stops the
run), extract, and score against corpus/expected/extract. Metrics are
reported per split (dev, heldout, heldout-2, assist-dev, heldout-3) with the
dataset version and case counts, and the gates in scripts/eval_scoring.py
decide the exit status. An
unavailable sandbox stops the run: it is an infrastructure error, not a score.

Expected outputs and baselines are never written by `make eval`; a held-out
split's baseline is rewritten only by `make eval-baseline SPLIT=<split>`, and
only when every hard gate passes. All are committed, so any change is a
reviewed diff. heldout-2's expected outputs are also frozen by its
pre-registration (corpus/eval/heldout-2-preregistration.json).
"""

import hashlib
import json
import os
import sys
from collections.abc import Callable, Collection, Iterable
from pathlib import Path
from typing import Any

from api.extraction.contract import ExtractionResult
from api.extraction.extract import EXTRACTOR_VERSION, Page, extract
from api.sandbox import ParseFailed, SandboxSettings, parse_in_sandbox
from scripts.eval_scoring import Actual, CaseScore, gate, score_case, summarize

ROOT = Path(__file__).resolve().parents[3]
CORPUS = ROOT / "corpus"
sys.path.insert(0, str(CORPUS))

from cases import CASES, DATASET, VERSION  # noqa: E402

# assist-dev (development, integrity-gated only) and heldout-3 (pre-registered for
# assisted-2) are generated from corpus/eval/heldout-3-spec.json.
SPLITS = ("dev", "heldout", "heldout-2", "assist-dev", "heldout-3")
BASELINES = {
    "heldout": CORPUS / "eval" / "heldout-baseline.json",
    "heldout-2": CORPUS / "eval" / "heldout-2-baseline.json",
    "heldout-3": CORPUS / "eval" / "heldout-3-baseline.json",
}

type Extractor = Callable[[Iterable[Page]], ExtractionResult]
# Name -> (version recorded in reports and baselines, extractor).
EXTRACTORS: dict[str, tuple[str, Extractor]] = {"rules": (EXTRACTOR_VERSION, extract)}


def run(
    settings: SandboxSettings,
    extractor: Extractor = extract,
    only: Collection[str] | None = None,
) -> tuple[list[CaseScore], dict[str, str]]:
    """Score every corpus case, or only the case ids in `only`."""
    manifest = {c["id"]: c for c in json.loads((CORPUS / "manifest.json").read_text())["cases"]}
    if set(manifest) != {case.id for case in CASES}:
        raise SystemExit("eval: corpus/manifest.json and corpus/cases.py list different cases")
    scores: list[CaseScore] = []
    versions: dict[str, str] = {}
    for case in CASES:
        if only is not None and case.id not in only:
            continue
        data = case.make()
        if hashlib.sha256(data).hexdigest() != manifest[case.id]["sha256"]:
            raise SystemExit(f"eval: {case.id} differs from corpus/manifest.json")
        expected = json.loads((CORPUS / "expected" / "extract" / f"{case.id}.json").read_text())
        if expected["case"] != case.id or expected["split"] != case.split:
            raise SystemExit(f"eval: {case.id}: expected output has the wrong case or split")
        outcome = parse_in_sandbox(data, settings)
        if isinstance(outcome, ParseFailed):
            if outcome.transient:
                raise SystemExit(f"eval: sandbox unavailable ({outcome.code}); nothing was scored")
            actual = Actual(failure_code=outcome.code, result=None, pages={})
        else:
            versions = outcome.versions.model_dump(by_alias=True)
            pages = {page.number: page.text for page in outcome.pages}
            _check_no_parser_drift(case.id, pages)
            result = extractor(Page(number, text) for number, text in pages.items())
            actual = Actual(
                failure_code=None, result=result.model_dump(by_alias=True, mode="json"), pages=pages
            )
        scores.append(score_case(expected, actual))
    return scores, versions


def _check_no_parser_drift(case_id: str, pages: dict[int, str]) -> None:
    golden = json.loads((CORPUS / "expected" / "parse" / f"{case_id}.json").read_text())
    if not golden["ok"] or {p["number"]: p["text"] for p in golden["pages"]} != pages:
        raise SystemExit(f"eval: {case_id}: parser output differs from corpus/expected/parse")


def report(
    scores: list[CaseScore], versions: dict[str, str], extractor: str = EXTRACTOR_VERSION
) -> dict[str, Any]:
    return {
        "dataset": DATASET,
        "dataset_version": VERSION,
        "extractor": extractor,
        "parser": versions,
        "metrics": {split: summarize([s for s in scores if s.split == split]) for split in SPLITS},
        "problems": {s.case: s.problems for s in scores if s.problems},
    }


def main(argv: list[str]) -> int:
    image = os.environ.get("PARSER_IMAGE", "")
    if not image:
        print("eval: set PARSER_IMAGE (make parser-image)", file=sys.stderr)
        return 2
    name = _option(argv, "--extractor") or "rules"
    if name not in EXTRACTORS:
        print(f"eval: unknown extractor {name!r} (known: {', '.join(EXTRACTORS)})", file=sys.stderr)
        return 2
    write = _option(argv, "--write-baseline")
    if write is not None and write not in BASELINES:
        print(f"eval: --write-baseline needs one of {', '.join(BASELINES)}", file=sys.stderr)
        return 2
    version, extractor = EXTRACTORS[name]
    settings = SandboxSettings(image=image, docker=os.environ.get("PARSER_DOCKER", "docker"))
    scores, versions = run(settings, extractor)
    summary = report(scores, versions, version)
    baselines: dict[str, dict[str, Any] | None] = {}
    for split, path in BASELINES.items():
        baseline = json.loads(path.read_text()) if path.exists() else None
        cases = sorted(s.case for s in scores if s.split == split)
        if baseline is not None and (
            baseline["dataset"] != DATASET
            or baseline["dataset_version"] != VERSION
            or baseline["split"] != split
            or baseline["cases"] != cases
        ):
            print(
                f"eval: the {split} baseline is for a different dataset or case set",
                file=sys.stderr,
            )
            return 1
        baselines[split] = None if split == write else baseline

    print(f"eval: {DATASET} v{VERSION}, extractor {summary['extractor']}, parser {versions}")
    names = list(summary["metrics"]["dev"])
    print(f"  {'metric':24} " + " ".join(f"{split:>10}" for split in SPLITS))
    for metric in names:
        row = [summary["metrics"][split][metric] for split in SPLITS]
        print(f"  {metric:24} " + " ".join(f"{'n/a' if v is None else v!s:>10}" for v in row))
    for case, problems in summary["problems"].items():
        for problem in problems:
            print(f"  {case}: {problem}")

    failures = gate(summary["metrics"], baselines)
    # The split being written has no baseline to compare with; every hard gate still applies.
    failures = [f for f in failures if write is None or f != f"{write}: no committed baseline"]
    if write is not None:
        if failures:
            print("eval: gates fail; baseline not written", file=sys.stderr)
        else:
            path = BASELINES[write]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "dataset": DATASET,
                        "dataset_version": VERSION,
                        "split": write,
                        "cases": sorted(s.case for s in scores if s.split == write),
                        "extractor": summary["extractor"],
                        "metrics": summary["metrics"][write],
                    },
                    indent=2,
                )
                + "\n"
            )
            print(f"eval: wrote {path.relative_to(ROOT)}")
    for failure in failures:
        print(f"  GATE FAILED {failure}", file=sys.stderr)
    print("eval: all gates passed" if not failures else f"eval: {len(failures)} gate(s) failed")
    return 1 if failures else 0


def _option(argv: list[str], name: str) -> str | None:
    """The value of `--name value` or `--name=value`, or None."""
    for index, arg in enumerate(argv):
        if arg == name:
            return argv[index + 1] if index + 1 < len(argv) else ""
        if arg.startswith(f"{name}="):
            return arg.split("=", 1)[1]
    return None


if __name__ == "__main__":
    sys.exit(main(sys.argv))
