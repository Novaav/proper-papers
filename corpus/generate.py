"""Build and verify the DI-1 synthetic corpus (standard library only).

    python3 corpus/generate.py check              # regenerate in memory; compare with manifest.json
    python3 corpus/generate.py build <out_dir>    # check, then write <id>.pdf files
    python3 corpus/generate.py manifest           # rewrite manifest.json (a reviewed change)
    python3 corpus/generate.py expected           # write the generated cases' expected outputs

The generated PDFs are not committed: the committed manifest pins every case's
SHA-256, so any change to the generator or a case shows up as a manifest diff.
"""

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases import CASES, DATASET, VERSION
from generated import generate as generated_cases

MANIFEST = Path(__file__).resolve().parent / "manifest.json"
EXTRACT = Path(__file__).resolve().parent / "expected" / "extract"


def expected_text(expected: dict[str, object]) -> str:
    return json.dumps(expected, indent=2, ensure_ascii=False) + "\n"


def check_expected() -> None:
    """The generated cases' committed expected outputs equal their generated truth."""
    for case in generated_cases():
        path = EXTRACT / f"{case.id}.json"
        if not path.exists() or path.read_text() != expected_text(case.expected):
            raise SystemExit(f"corpus: {path.name} differs from its generated expected output")


def generate() -> dict[str, bytes]:
    ids = [case.id for case in CASES]
    if len(ids) != len(set(ids)):
        raise SystemExit("corpus: duplicate case ids")
    first = {case.id: case.make() for case in CASES}
    second = {case.id: case.make() for case in CASES}
    if first != second:
        raise SystemExit("corpus: generation is not deterministic")
    return first


def manifest(files: dict[str, bytes]) -> dict[str, object]:
    return {
        "dataset": DATASET,
        "version": VERSION,
        "cases": [
            {
                "id": case.id,
                "split": case.split,
                "category": case.category,
                "description": case.description,
                "bytes": len(files[case.id]),
                "sha256": hashlib.sha256(files[case.id]).hexdigest(),
            }
            for case in CASES
        ],
    }


def check(files: dict[str, bytes]) -> None:
    expected = json.loads(MANIFEST.read_text())
    actual = manifest(files)
    if expected != actual:
        raise SystemExit(
            "corpus: generated files differ from corpus/manifest.json "
            "(review the change, then run `python3 corpus/generate.py manifest`)"
        )


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else ""
    files = generate()
    if command == "manifest":
        MANIFEST.write_text(json.dumps(manifest(files), indent=2) + "\n")
        print(f"corpus: wrote manifest for {len(files)} cases")
    elif command == "check":
        check(files)
        check_expected()
        print(f"corpus: {len(files)} cases match the manifest (dataset {DATASET} v{VERSION})")
    elif command == "expected":
        for case in generated_cases():
            (EXTRACT / f"{case.id}.json").write_text(expected_text(case.expected))
        print("corpus: wrote the generated cases' expected outputs")
    elif command == "build" and len(argv) == 3:
        check(files)
        out = Path(argv[2])
        out.mkdir(parents=True, exist_ok=True)
        for case_id, data in files.items():
            (out / f"{case_id}.pdf").write_bytes(data)
        print(f"corpus: wrote {len(files)} PDFs to {out}")
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
