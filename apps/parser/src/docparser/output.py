"""The parser's JSON result (schema docparser.output.v1), without run metrics."""

from typing import Any

from docparser.parse import SCHEMA, Parsed, parser_versions


def success(parsed: Parsed) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "ok": True,
        "versions": parser_versions(),
        "page_count": len(parsed.pages),
        "pages": [
            {
                "number": page.number,
                "text": page.text,
                "off_page_chars": page.off_page_chars,
                "invisible_chars": page.invisible_chars,
                "undefined_chars": page.undefined_chars,
                "control_chars_removed": page.control_chars_removed,
            }
            for page in parsed.pages
        ],
        "features": {"javascript": parsed.features.javascript, "xfa": parsed.features.xfa},
    }


def failure(code: str) -> dict[str, Any]:
    return {"schema": SCHEMA, "ok": False, "versions": parser_versions(), "code": code}
