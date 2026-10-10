"""PDF bytes -> normalized per-page text and content flags, or a fixed failure code.

Runs only inside the isolated parser container (DI-1). Only the page text layer
is read. Embedded files, attachments and rich media are never extracted: their
presence is a failure (`embedded_content`, decision F4). JavaScript and XFA are
never executed and only flagged. Parsing is strict: a document that needs
cross-reference repair, or declares encryption, is refused rather than
reconstructed.
"""

import io
from collections.abc import Iterator
from dataclasses import dataclass
from importlib.metadata import version
from typing import Any

from pdfminer import settings as pdfminer_settings
from pdfminer.converter import PDFPageAggregator
from pdfminer.layout import (
    LAParams,
    LTAnno,
    LTChar,
    LTComponent,
    LTContainer,
    LTPage,
    LTTextBox,
)
from pdfminer.pdfcolor import PDFColorSpace
from pdfminer.pdfdevice import PDFTextSeq
from pdfminer.pdfdocument import PDFDocument, PDFEncryptionError
from pdfminer.pdffont import PDFFont, PDFUnicodeNotDefined
from pdfminer.pdfinterp import (
    PDFGraphicState,
    PDFPageInterpreter,
    PDFResourceManager,
    PDFTextState,
)
from pdfminer.pdfpage import PDFPage
from pdfminer.pdfparser import PDFParser
from pdfminer.pdftypes import resolve1
from pdfminer.psparser import PSLiteral
from pdfminer.utils import Matrix

from docparser import __version__
from docparser.limits import DEFAULT_LIMITS, Limits
from docparser.normalize import NORMALIZER_VERSION, normalize

SCHEMA = "docparser.output.v1"
# Text rendering modes that paint nothing (PDF 32000-1, 9.3.6): 3 invisible,
# 7 clip only. Such text is excluded and counted, never extracted.
INVISIBLE_RENDER_MODES = frozenset({3, 7})
# Fixed layout analysis parameters: part of the pipeline version.
LAYOUT = LAParams(
    line_overlap=0.5,
    char_margin=2.0,
    line_margin=0.5,
    word_margin=0.1,
    boxes_flow=0.5,
    detect_vertical=False,
    all_texts=True,
)
# Bound on nested dictionaries followed while looking for actions.
MAX_ACTION_DEPTH = 8

FAILURE_CODES = frozenset(
    {
        "input_too_large",
        "parse_failed",
        "encrypted_pdf",
        "embedded_content",
        "too_many_pages",
        "text_too_large",
        "no_text_layer",
        "memory_exceeded",
    }
)


class ParseFailure(Exception):
    def __init__(self, code: str) -> None:
        assert code in FAILURE_CODES, code
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Page:
    number: int
    text: str
    off_page_chars: int
    invisible_chars: int
    undefined_chars: int
    control_chars_removed: int


@dataclass(frozen=True)
class Features:
    javascript: bool
    xfa: bool


@dataclass(frozen=True)
class Parsed:
    pages: list[Page]
    features: Features


def parser_versions() -> dict[str, str]:
    return {
        "docparser": __version__,
        "pdfminer.six": version("pdfminer.six"),
        "normalizer": NORMALIZER_VERSION,
    }


class _Char(LTChar):
    render_mode: int = 0


class _Aggregator(PDFPageAggregator):
    """Records each character's text rendering mode and counts undefined glyphs."""

    def __init__(self, rsrcmgr: PDFResourceManager) -> None:
        super().__init__(rsrcmgr, laparams=LAYOUT)
        self._render_mode = 0
        self.undefined_chars = 0

    def render_string(
        self,
        textstate: PDFTextState,
        seq: PDFTextSeq,
        ncs: PDFColorSpace,
        graphicstate: PDFGraphicState,
    ) -> None:
        self._render_mode = textstate.render
        super().render_string(textstate, seq, ncs, graphicstate)

    def render_char(
        self,
        matrix: Matrix,
        font: PDFFont,
        fontsize: float,
        scaling: float,
        rise: float,
        cid: int,
        ncs: PDFColorSpace,
        graphicstate: PDFGraphicState,
    ) -> float:
        # As PDFLayoutAnalyzer.render_char, with the rendering mode recorded.
        try:
            text = font.to_unichr(cid)
        except PDFUnicodeNotDefined:
            text = self.handle_undefined_char(font, cid)
        item = _Char(
            matrix,
            font,
            fontsize,
            scaling,
            rise,
            text,
            font.char_width(cid),
            font.char_disp(cid),
            ncs,
            graphicstate,
        )
        item.render_mode = self._render_mode
        self.cur_item.add(item)
        return item.adv

    def handle_undefined_char(self, font: PDFFont, cid: int) -> str:
        self.undefined_chars += 1
        return "�"


def parse(data: bytes, limits: Limits = DEFAULT_LIMITS) -> Parsed:
    """Parse one PDF; raises ParseFailure with a fixed code on any refusal."""
    if len(data) > limits.max_input_bytes:
        raise ParseFailure("input_too_large")
    if not data.startswith(b"%PDF-"):
        raise ParseFailure("parse_failed")
    try:
        return _parse(data, limits)
    except ParseFailure:
        raise
    except MemoryError:
        raise ParseFailure("memory_exceeded") from None
    except PDFEncryptionError:
        raise ParseFailure("encrypted_pdf") from None
    except Exception:
        # Any other parser error (syntax, broken structure, recursion) on
        # untrusted input: refuse, without detail.
        raise ParseFailure("parse_failed") from None


def _parse(data: bytes, limits: Limits) -> Parsed:
    # Strict mode is what refuses documents that need repair (for example wrong
    # object offsets); disabling the fallback cross-reference scan is defence in depth.
    pdfminer_settings.STRICT = True
    document = PDFDocument(PDFParser(io.BytesIO(data)), fallback=False)
    if document.encryption is not None:
        raise ParseFailure("encrypted_pdf")
    catalog = _dict(document.catalog)
    names = _dict(catalog.get("Names"))
    if "EmbeddedFiles" in names:
        raise ParseFailure("embedded_content")
    javascript = (
        "JavaScript" in names
        or _action_has_javascript(catalog.get("OpenAction"))
        or _actions_have_javascript(catalog.get("AA"))
    )
    xfa = "XFA" in _dict(catalog.get("AcroForm"))

    pages: list[PDFPage] = []
    for page in PDFPage.create_pages(document):
        pages.append(page)
        if len(pages) > limits.max_pages:
            raise ParseFailure("too_many_pages")
    for page in pages:
        for annotation in _list(page.annots):
            annotation = _dict(annotation)
            if _name(annotation.get("Subtype")) in ("FileAttachment", "RichMedia"):
                raise ParseFailure("embedded_content")
            javascript = (
                javascript
                or _action_has_javascript(annotation.get("A"))
                or _actions_have_javascript(annotation.get("AA"))
            )
    javascript = javascript or any(_actions_have_javascript(page.attrs.get("AA")) for page in pages)

    resources = PDFResourceManager(caching=False)
    result: list[Page] = []
    total = 0
    for number, page in enumerate(pages, start=1):
        device = _Aggregator(resources)
        PDFPageInterpreter(resources, device).process_page(page)
        layout = device.get_result()
        raw, off_page, invisible = _page_text(layout, limits)
        normalized = normalize(raw)
        if len(normalized.text) > limits.max_page_chars:
            raise ParseFailure("text_too_large")
        total += len(normalized.text)
        if total > limits.max_total_chars:
            raise ParseFailure("text_too_large")
        result.append(
            Page(
                number=number,
                text=normalized.text,
                off_page_chars=off_page,
                invisible_chars=invisible,
                undefined_chars=device.undefined_chars,
                control_chars_removed=normalized.control_chars_removed,
            )
        )
    if total == 0:
        raise ParseFailure("no_text_layer")
    return Parsed(pages=result, features=Features(javascript=javascript, xfa=xfa))


def _page_text(layout: LTPage, limits: Limits) -> tuple[str, int, int]:
    """Visible, on-page characters in layout order; text boxes end with a newline."""
    x0, y0, x1, y1 = layout.bbox
    parts: list[str] = []
    size = off_page = invisible = 0
    # Normalization only shrinks text apart from rare NFKC expansions, so twice
    # the page limit bounds memory without rejecting any page that fits.
    raw_limit = 2 * limits.max_page_chars
    for item in _walk(layout):
        if isinstance(item, LTChar):
            if isinstance(item, _Char) and item.render_mode in INVISIBLE_RENDER_MODES:
                invisible += 1
                continue
            cx, cy = (item.x0 + item.x1) / 2, (item.y0 + item.y1) / 2
            if not (x0 <= cx <= x1 and y0 <= cy <= y1):
                off_page += 1
                continue
            text = item.get_text()
        elif isinstance(item, LTAnno):
            text = item.get_text()
        else:
            text = "\n"  # end of a text box
        size += len(text)
        if size > raw_limit:
            raise ParseFailure("text_too_large")
        parts.append(text)
    return "".join(parts), off_page, invisible


def _walk(container: LTContainer[Any]) -> Iterator[LTChar | LTAnno | LTTextBox]:
    """Characters and virtual spaces in layout order, then each box's end marker."""
    for item in container:
        if isinstance(item, (LTChar, LTAnno)):
            yield item
        elif isinstance(item, LTContainer):
            yield from _walk(item)
            if isinstance(item, LTTextBox):
                yield item
        elif isinstance(item, LTComponent):
            continue  # images, curves, rectangles: no text


def _action_has_javascript(value: object, depth: int = 0) -> bool:
    """Whether an action or its chain (`Next`: one action or a list) runs JavaScript.

    Follows only `Next`, never other references (pages, resources, destinations),
    with bounded depth and breadth.
    """
    if depth > MAX_ACTION_DEPTH:
        return True  # unusually deep chain: flag rather than follow
    action = resolve1(value)
    if not isinstance(action, dict) or "S" not in action:
        return False  # a destination or anything else that is not an action
    if _name(action.get("S")) == "JavaScript" or "JS" in action:
        return True
    following = resolve1(action.get("Next"))
    chain = following[:64] if isinstance(following, list) else [following]
    return any(_action_has_javascript(item, depth + 1) for item in chain)


def _actions_have_javascript(value: object) -> bool:
    """Whether an additional-actions dictionary (trigger -> action) runs JavaScript."""
    return any(_action_has_javascript(action) for action in list(_dict(value).values())[:64])


def _dict(value: object) -> dict[str, Any]:
    value = resolve1(value)
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[Any]:
    value = resolve1(value)
    return value if isinstance(value, list) else []


def _name(value: object) -> str | None:
    value = resolve1(value)
    return value.name if isinstance(value, PSLiteral) and isinstance(value.name, str) else None
