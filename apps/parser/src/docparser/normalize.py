"""Deterministic page-text normalization (versioned).

Evidence offsets always refer to text normalized by one version of this
function, so any change to it must bump NORMALIZER_VERSION.
"""

import re
import unicodedata
from dataclasses import dataclass

NORMALIZER_VERSION = "1"

_SPACES = re.compile("[ \t\f\v\u00a0]+")
_TRAILING = re.compile(r" +\n")
_LEADING = re.compile(r"\n +")
_BLANK_LINES = re.compile(r"\n{3,}")


@dataclass(frozen=True)
class Normalized:
    text: str
    # Format (Cf: zero-width, bidi controls, soft hyphen) and control (Cc)
    # characters removed, excluding newline and tab.
    control_chars_removed: int


def normalize(raw: str) -> Normalized:
    """NFKC; drop format/control characters; unify line breaks and spaces."""
    text = unicodedata.normalize("NFKC", raw.replace("\r\n", "\n").replace("\r", "\n"))
    kept: list[str] = []
    removed = 0
    for char in text:
        if char in "\n\t":
            kept.append(char)
        elif unicodedata.category(char) in ("Cf", "Cc"):
            removed += 1
        else:
            kept.append(char)
    text = _SPACES.sub(" ", "".join(kept))
    text = _TRAILING.sub("\n", text)
    text = _LEADING.sub("\n", text)
    text = _BLANK_LINES.sub("\n\n", text)
    return Normalized(text=text.strip(), control_chars_removed=removed)
