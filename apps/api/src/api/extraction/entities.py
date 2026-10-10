"""Party-name completeness for model-proposed names (assisted-2; reject-only).

A verbatim quote proves where a name came from, not that it is the party's
whole name: a defined short name or a trading name is quoted just as exactly
as the full legal name. These checks run on a model-proposed provider or
customer name after the quote and raw checks have passed, and can only reject
it; nothing is ever completed, resolved or replaced, so a rejected name leaves
the field not_found. The rules' own results are never checked here.

- Defined term or alias: the name appears in the document as a quoted term
  ("Name", or in curly quotes) or right after an alias phrase (trading as, hereinafter,
  known as, operating as, operates under the name, doing business as, t/a,
  d/b/a, referred to as).
- Truncation: somewhere in the document the name is directly continued on the
  same line by another capitalised word, "&" or a legal form (only a legal form
  after a name ending in a full stop), or on the next line by a legal form;
  then it is part of a longer name.
- Cross-role equality (in assisted.py): a provider name equal to a customer
  name, from the rules or the model, is rejected.

The checks read the document text, which is untrusted: a planted definition or
a planted longer form can only cause a rejection, never a value.
"""

import re
from collections.abc import Iterable

LEGAL_FORMS = (
    "Ltd", "Ltd.", "Limited", "plc", "PLC", "LLP", "LLC", "L.L.C.", "Inc", "Inc.", "Corp",
    "Corp.", "Corporation", "Co.", "GmbH", "AG", "KG", "AB", "B.V.", "BV", "N.V.", "NV",
    "S.A.", "SA", "S.A.S.", "SAS", "S.L.", "S.p.A.", "Pty", "Oy", "A/S", "AS", "ApS",
)  # fmt: skip
_FORM = (
    "(?:" + "|".join(re.escape(form) for form in sorted(LEGAL_FORMS, key=len, reverse=True)) + ")"
)
_ALIAS = (
    r"(?:trading\s+as|t/a|doing\s+business\s+as|d/b/a|operating\s+as|"
    r"operates\s+under\s+the\s+name|known\s+as|hereinafter(?:\s+referred\s+to\s+as)?|"
    r"referred\s+to\s+as)"
)
_OPEN_QUOTES = "\"'\u201c\u2018"  # straight, and curly opening quotes
_CLOSE_QUOTES = "\"'\u201d\u2019"  # straight, and curly closing quotes


def _name_pattern(name: str) -> str:
    """The name's words, separated by any whitespace (a name may wrap across lines)."""
    return r"\s+".join(re.escape(word) for word in name.split())


def is_defined_term(name: str, pages: Iterable[str]) -> bool:
    """Whether the document introduces the name as a quoted term or an alias."""
    words = _name_pattern(name)
    if not words:
        return False
    quoted = re.compile(rf"[{_OPEN_QUOTES}]{words}[{_CLOSE_QUOTES}]")
    alias = re.compile(rf"\b{_ALIAS}\s+(?:the\s+)?[{_OPEN_QUOTES}]?{words}(?![\w])", re.IGNORECASE)
    return any(quoted.search(text) or alias.search(text) for text in pages)


def is_truncated(name: str, pages: Iterable[str]) -> bool:
    """Whether the name is anywhere directly continued into a longer name."""
    words = _name_pattern(name)
    if not words:
        return False
    # After a name ending in a full stop ("Inc.") a capitalised word starts a new
    # sentence; only a legal form then continues the name.
    same_line = rf"{_FORM}(?![\w])" if name.rstrip().endswith(".") else rf"[A-Z]|&|{_FORM}(?![\w])"
    continued = re.compile(
        rf"(?<![\w]){words}(?:[ \t]+(?:{same_line})|[ \t]*\n[ \t]*{_FORM}(?![\w]))"
    )
    return any(continued.search(text) for text in pages)


def same_party(first: str, second: str) -> bool:
    """Whether two names are the same, ignoring case and spacing."""
    return " ".join(first.casefold().split()) == " ".join(second.casefold().split())
