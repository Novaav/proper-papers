"""Synthetic PDFs for scanner tests (standard library only; nothing real or confidential).

Structurally valid files (objects, cross-reference table, trailer) so that the
scanner's PDF parser treats them as PDFs: clean, carrying the test-only
signature marker, padded to a size, with a stream that expands when
decompressed, and encrypted with the standard security handler (RC4, R2).
"""

import hashlib
import struct
import zlib

# Matched only by the test-only signature in tests/clamav/test.ndb.
MARKER = b"PRODUCTION-AI-FLAGSHIP-SYNTHETIC-SCANNER-TEST-MARKER-2026"

_PAD = bytes.fromhex("28BF4E5E4E758A4164004E56FFFA01082E2E00B6D0683E802F0CA9FE6453697A")
_ID = hashlib.md5(b"production-ai-flagship synthetic").digest()


def eicar() -> bytes:
    """The standard EICAR anti-malware test string, assembled at run time (never stored)."""
    parts = ["X5O!P%@AP[4\\PZX54(P^)7CC)7}$", "EICAR-STANDARD-ANTIVIRUS-", "TEST-FILE!$H+H*"]
    return "".join(parts).encode()


def _build(objects: list[bytes], trailer_extra: bytes = b"", padding: int = 0) -> bytes:
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    if padding:
        out += b"%" + b"x" * padding + b"\n"
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R%s >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        trailer_extra,
        xref,
    )
    return bytes(out)


def _page(stream: bytes, stream_dict: bytes = b"") -> list[bytes]:
    return [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d%s >>\nstream\n" % (len(stream), stream_dict) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]


def _text(line: bytes) -> bytes:
    return b"BT /F1 12 Tf 72 760 Td (" + line + b") Tj ET"


def clean_pdf() -> bytes:
    return _build(_page(_text(b"Synthetic scanner test document, no real data")))


def marker_pdf() -> bytes:
    return _build(_page(_text(MARKER)))


def padded_pdf(size: int) -> bytes:
    """A clean PDF of about `size` bytes (a comment line pads it)."""
    base = clean_pdf()
    return _build(_page(_text(b"Synthetic padded document")), padding=max(0, size - len(base)))


def expanding_pdf(expanded: int) -> bytes:
    """A small PDF whose content stream decompresses (Flate) to `expanded` bytes."""
    compressed = zlib.compress(b" " * expanded, 9)
    return _build(_page(compressed, b" /Filter /FlateDecode"))


def _rc4(key: bytes, data: bytes) -> bytes:
    state = list(range(256))
    j = 0
    for i in range(256):
        j = (j + state[i] + key[i % len(key)]) % 256
        state[i], state[j] = state[j], state[i]
    out = bytearray()
    i = j = 0
    for byte in data:
        i = (i + 1) % 256
        j = (j + state[i]) % 256
        state[i], state[j] = state[j], state[i]
        out.append(byte ^ state[(state[i] + state[j]) % 256])
    return bytes(out)


def encrypted_pdf(
    user_password: bytes = b"",
    owner_password: bytes = b"owner",
    line: bytes = b"Synthetic encrypted document",
) -> bytes:
    """Encrypted with the standard security handler, revision 2 (40-bit RC4).

    An empty user password opens without a prompt ("permissions only"); a
    non-empty one needs the password to read anything.
    """
    permissions = -44
    owner_key = hashlib.md5((owner_password + _PAD)[:32]).digest()[:5]
    owner_entry = _rc4(owner_key, (user_password + _PAD)[:32])
    key = hashlib.md5(
        (user_password + _PAD)[:32] + owner_entry + struct.pack("<i", permissions) + _ID
    ).digest()[:5]
    user_entry = _rc4(key, _PAD)
    stream = _text(line)
    object_key = hashlib.md5(key + (4).to_bytes(3, "little") + (0).to_bytes(2, "little")).digest()
    objects = _page(_rc4(object_key[:10], stream))
    objects.append(
        b"<< /Filter /Standard /V 1 /R 2 /O <%s> /U <%s> /P %d >>"
        % (owner_entry.hex().encode(), user_entry.hex().encode(), permissions)
    )
    trailer = b" /Encrypt 6 0 R /ID [<%s> <%s>]" % (_ID.hex().encode(), _ID.hex().encode())
    return _build(objects, trailer)
