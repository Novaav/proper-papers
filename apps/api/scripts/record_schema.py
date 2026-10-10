"""Write the committed JSON Schema of the approved_record.v1 export (R1).

    uv run --locked python -m scripts.record_schema

The schema is generated from api.review.ApprovedRecordV1; a test fails when the
committed file differs, so a change to the export format is always a reviewed diff.
"""

import json
from pathlib import Path

from api.review import record_json_schema

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "approved_record.v1.schema.json"


def render() -> str:
    return json.dumps(record_json_schema(), indent=2, sort_keys=True) + "\n"


def main() -> None:
    SCHEMA_PATH.write_text(render(), encoding="utf-8")
    print(f"wrote {SCHEMA_PATH.name}")


if __name__ == "__main__":
    main()
