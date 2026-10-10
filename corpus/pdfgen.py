"""A minimal, deterministic PDF writer for the synthetic corpus (standard library only).

Output is byte-identical on every run and platform: no timestamps, no document
IDs (unless a case needs one), no compression, fixed object order. It writes
only what the corpus needs: text lines in the standard Helvetica font, optional
text rendering modes and positions, a raw image, and the structural features
the adversarial cases carry (an embedded file, a file attachment annotation, a
JavaScript open action, a declared encryption dictionary).
"""

from dataclasses import dataclass, field

PAGE_WIDTH, PAGE_HEIGHT = 595, 842  # A4 in points
# The second font maps two otherwise unused codes to zero-width characters
# through a ToUnicode CMap, so they reach the text layer like real ones do.
ZERO_WIDTH_CODES = {"​": b"\x01", "‍": b"\x02"}
_TO_UNICODE = b"""/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CMapName /Synthetic-ZeroWidth def
/CMapType 2 def
1 begincodespacerange
<00> <FF>
endcodespacerange
2 beginbfchar
<01> <200B>
<02> <200D>
endbfchar
endcmap
CMapName currentdict /CMap defineresource pop
end
end"""


@dataclass(frozen=True)
class Line:
    text: str
    y: float
    x: float = 72
    size: float = 11
    render: int = 0  # PDF text rendering mode (3 = invisible)
    zero_width_font: bool = False


@dataclass(frozen=True)
class Page:
    lines: tuple[Line, ...] = ()
    image: bool = False
    file_attachment: bool = False


@dataclass(frozen=True)
class Document:
    pages: tuple[Page, ...]
    embedded_file: bool = False
    javascript_open_action: bool = False
    declared_encryption: bool = False
    xfa_form: bool = False
    image_side: int = 8  # pixels per side of the (shared) grey image


def text_page(*paragraphs: str, top: float = 770, leading: float = 15) -> Page:
    """Lines top-down; an empty string leaves a paragraph gap."""
    lines: list[Line] = []
    y = top
    for text in paragraphs:
        if text:
            lines.append(Line(text, y))
        y -= leading if text else leading * 0.8
    return Page(tuple(lines))


def pdf_string(text: str, zero_width_font: bool = False) -> bytes:
    if zero_width_font:
        encoded = b"".join(ZERO_WIDTH_CODES.get(c) or c.encode("cp1252") for c in text)
    else:
        encoded = text.encode("cp1252")  # WinAnsiEncoding
    escaped = encoded.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")
    return b"(" + escaped + b")"


@dataclass
class _Writer:
    bodies: list[bytes] = field(default_factory=list)

    def reserve(self) -> int:
        self.bodies.append(b"")
        return len(self.bodies)

    def set(self, number: int, body: bytes) -> None:
        self.bodies[number - 1] = body

    def add(self, body: bytes) -> int:
        number = self.reserve()
        self.set(number, body)
        return number

    def stream(self, entries: bytes, data: bytes) -> int:
        return self.add(
            b"<< "
            + entries
            + b" /Length "
            + str(len(data)).encode()
            + b" >>\nstream\n"
            + data
            + b"\nendstream"
        )

    def output(self, root: int, trailer_extra: bytes = b"") -> bytes:
        out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for number, body in enumerate(self.bodies, start=1):
            offsets.append(len(out))
            out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(self.bodies) + 1}\n0000000000 65535 f \n".encode()
        for offset in offsets:
            out += f"{offset:010d} 00000 n \n".encode()
        out += (
            f"trailer\n<< /Size {len(self.bodies) + 1} /Root {root} 0 R ".encode()
            + trailer_extra
            + f">>\nstartxref\n{xref}\n%%EOF\n".encode()
        )
        return bytes(out)


def build(document: Document) -> bytes:
    w = _Writer()
    catalog = w.reserve()
    pages_node = w.reserve()
    helvetica = w.add(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    )
    to_unicode = w.stream(b"", _TO_UNICODE)
    zero_width = w.add(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding << /BaseEncoding "
        b"/WinAnsiEncoding /Differences [1 /uni200B 2 /uni200D] >> /ToUnicode "
        + f"{to_unicode} 0 R".encode()
        + b" >>"
    )
    side = document.image_side
    image = w.stream(
        f"/Type /XObject /Subtype /Image /Width {side} /Height {side} /ColorSpace /DeviceGray "
        "/BitsPerComponent 8".encode(),
        (bytes(range(0, 256, 4)) * (side * side // 64 + 1))[: side * side],
    )
    filespec = None
    if document.embedded_file or any(page.file_attachment for page in document.pages):
        attachment = w.stream(
            b"/Type /EmbeddedFile /Subtype /text#2Fplain",
            b"Synthetic attachment used only to test that embedded files are refused.\n",
        )
        filespec = w.add(
            b"<< /Type /Filespec /F (note.txt) /UF (note.txt) /EF << /F "
            + f"{attachment} 0 R".encode()
            + b" >> >>"
        )

    page_refs = []
    for page in document.pages:
        content = bytearray()
        for line in page.lines:
            font = b"/F2" if line.zero_width_font else b"/F1"
            content += (
                b"BT "
                + font
                + f" {line.size:g} Tf {line.render} Tr {line.x:g} {line.y:g} Td ".encode()
                + pdf_string(line.text, line.zero_width_font)
                + b" Tj ET\n"
            )
        if page.image:
            content += b"q 200 0 0 200 100 500 cm /Im1 Do Q\n"
        contents = w.stream(b"", bytes(content))
        annots = b""
        if page.file_attachment:
            assert filespec is not None
            annotation = w.add(
                b"<< /Type /Annot /Subtype /FileAttachment /Rect [72 700 92 720] /FS "
                + f"{filespec} 0 R".encode()
                + b" /Contents (Synthetic attachment) >>"
            )
            annots = f" /Annots [{annotation} 0 R]".encode()
        page_refs.append(
            w.add(
                f"<< /Type /Page /Parent {pages_node} 0 R "
                f"/MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
                f"/Resources << /Font << /F1 {helvetica} 0 R /F2 {zero_width} 0 R >> "
                f"/XObject << /Im1 {image} 0 R >> >> /Contents {contents} 0 R".encode()
                + annots
                + b" >>"
            )
        )
    kids = " ".join(f"{ref} 0 R" for ref in page_refs)
    w.set(pages_node, f"<< /Type /Pages /Kids [{kids}] /Count {len(page_refs)} >>".encode())

    extra = b""
    if document.embedded_file:
        assert filespec is not None
        extra += (
            b" /Names << /EmbeddedFiles << /Names [(note.txt) "
            + f"{filespec} 0 R".encode()
            + b"] >> >>"
        )
    if document.javascript_open_action:
        extra += b' /OpenAction << /S /JavaScript /JS (console.println\\("synthetic"\\);) >>'
    if document.xfa_form:
        extra += b" /AcroForm << /Fields [] /XFA (synthetic) >>"
    w.set(catalog, f"<< /Type /Catalog /Pages {pages_node} 0 R".encode() + extra + b" >>")

    trailer = b""
    if document.declared_encryption:
        # Declared encryption only (the content is not encrypted): the parser must refuse
        # any document that declares /Encrypt, as the upload check does.
        encrypt = w.add(
            b"<< /Filter /Standard /V 1 /R 2 /P -44 /O <"
            + b"00" * 32
            + b"> /U <"
            + b"00" * 32
            + b"> >>"
        )
        trailer = f"/Encrypt {encrypt} 0 R /ID [<{'11' * 16}> <{'11' * 16}>] ".encode()
    return w.output(catalog, trailer)
