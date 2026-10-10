"""Failure codes, limits and content flags of the parser (host run; the sandbox adds isolation)."""

from dataclasses import replace

import pytest
from cases import LABELLED
from pdfgen import Document, Page, build, text_page
from pdfminer.pdfdocument import PDFDocument
from pdfminer.pdfparser import PDFParser
from pdfminer.psparser import LIT

from docparser import parse as parse_module
from docparser.limits import DEFAULT_LIMITS, Limits
from docparser.parse import FAILURE_CODES, ParseFailure, parse


def _code(data: bytes, limits: Limits = DEFAULT_LIMITS) -> str:
    with pytest.raises(ParseFailure) as error:
        parse(data, limits)
    return error.value.code


def test_a_labelled_agreement_parses() -> None:
    parsed = parse(build(Document((LABELLED,))))
    assert [page.number for page in parsed.pages] == [1]
    assert "Effective Date: 1 March 2026" in parsed.pages[0].text


@pytest.mark.parametrize(
    "data",
    [b"", b"not a pdf", b"%PDF-1.7\n", b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"],
)
def test_non_pdf_and_structureless_input_is_parse_failed(data: bytes) -> None:
    assert _code(data) == "parse_failed"


def test_a_broken_cross_reference_table_is_refused_not_repaired() -> None:
    data = build(Document((LABELLED,)))
    startxref = data.rindex(b"startxref\n") + len(b"startxref\n")
    broken = data[:startxref] + b"17" + data[data.index(b"\n", startxref) :]
    assert _code(broken) == "parse_failed"


def test_wrong_object_offsets_are_refused_not_repaired() -> None:
    """Strict mode is the control: without it pdfminer silently repairs this file."""
    data = build(Document((LABELLED,)))
    xref = data.index(b"xref\n")
    lines = data[xref:].split(b"\n")
    lines[4] = b"0000000011 00000 n "  # object 2 points into object 1
    assert _code(data[:xref] + b"\n".join(lines)) == "parse_failed"


def test_any_declared_encryption_is_refused_even_if_it_decrypts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The explicit check, independent of whether pdfminer can decrypt the document."""

    class DecryptedDocument(PDFDocument):
        def __init__(self, parser: PDFParser, fallback: bool = True) -> None:
            super().__init__(parser, fallback=fallback)
            self.encryption = (b"id", {"Filter": "Standard"})

    monkeypatch.setattr(parse_module, "PDFDocument", DecryptedDocument)
    assert _code(build(Document((LABELLED,)))) == "encrypted_pdf"


def test_input_over_the_limit_is_refused_before_parsing() -> None:
    data = build(Document((LABELLED,)))
    assert _code(data, Limits(max_input_bytes=len(data) - 1)) == "input_too_large"
    assert parse(data, Limits(max_input_bytes=len(data))).pages


def test_page_limit() -> None:
    three = build(Document((LABELLED, LABELLED, LABELLED)))
    assert _code(three, Limits(max_pages=2)) == "too_many_pages"
    assert len(parse(three, Limits(max_pages=3)).pages) == 3


def test_page_and_total_character_limits() -> None:
    data = build(Document((LABELLED, LABELLED)))
    per_page = len(parse(data).pages[0].text)
    assert _code(data, Limits(max_page_chars=per_page - 1)) == "text_too_large"
    assert _code(data, Limits(max_total_chars=2 * per_page - 1)) == "text_too_large"
    assert parse(data, Limits(max_page_chars=per_page, max_total_chars=2 * per_page)).pages


def test_raw_text_is_bounded_before_normalization() -> None:
    # 20 on-page lines of "a", 60 spaces, "b": about 1 260 raw characters that
    # normalize to 79. Raw text over twice the page limit is refused.
    spaces = build(Document((text_page(*["a" + " " * 60 + "b"] * 20),)))
    assert len(parse(spaces).pages[0].text) < 100
    assert _code(spaces, Limits(max_page_chars=100)) == "text_too_large"


def test_no_text_layer() -> None:
    assert _code(build(Document((Page(image=True), Page())))) == "no_text_layer"


def test_embedded_files_and_attachments_are_refused() -> None:
    assert _code(build(Document((LABELLED,), embedded_file=True))) == "embedded_content"
    attached = replace(LABELLED, file_attachment=True)
    assert _code(build(Document((attached,)))) == "embedded_content"


def test_declared_encryption_is_refused() -> None:
    assert _code(build(Document((LABELLED,), declared_encryption=True))) == "encrypted_pdf"


def test_javascript_and_xfa_are_flagged_not_refused() -> None:
    plain = parse(build(Document((LABELLED,))))
    assert (plain.features.javascript, plain.features.xfa) == (False, False)
    scripted = parse(build(Document((LABELLED,), javascript_open_action=True)))
    assert scripted.features.javascript
    assert scripted.pages == plain.pages
    assert parse(build(Document((LABELLED,), xfa_form=True))).features.xfa


def test_javascript_detection_follows_action_chains_only() -> None:
    js = {"S": LIT("JavaScript"), "JS": "x"}
    uri = {"S": LIT("URI"), "URI": "https://example.invalid"}
    check = parse_module._action_has_javascript
    assert check(js)
    assert not check(uri)
    assert check({**uri, "Next": js})
    assert check({**uri, "Next": [uri, js]})
    # A destination (array) or a page-like dictionary is never followed.
    assert not check([{"S": LIT("JavaScript")}])
    assert not check({"Type": LIT("Page"), "AA": {"O": js}})
    assert parse_module._actions_have_javascript({"O": uri, "C": js})
    assert not parse_module._actions_have_javascript({"O": uri})


def test_overlong_action_chains_are_flagged() -> None:
    chain: dict[str, object] = {"S": LIT("URI")}
    for _ in range(parse_module.MAX_ACTION_DEPTH + 2):
        chain = {"S": LIT("URI"), "Next": chain}
    assert parse_module._action_has_javascript(chain)


def test_unexpected_parser_errors_become_parse_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(data: bytes, limits: Limits) -> None:
        raise RecursionError("deep")

    monkeypatch.setattr(parse_module, "_parse", boom)
    assert _code(build(Document((LABELLED,)))) == "parse_failed"


def test_memory_errors_become_memory_exceeded(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(data: bytes, limits: Limits) -> None:
        raise MemoryError

    monkeypatch.setattr(parse_module, "_parse", boom)
    assert _code(build(Document((LABELLED,)))) == "memory_exceeded"


def test_failure_codes_are_a_closed_set() -> None:
    with pytest.raises(AssertionError):
        ParseFailure("anything_else")
    assert "parse_failed" in FAILURE_CODES
