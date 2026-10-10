"""assisted-2's party-name completeness checks (synthetic text only; reject-only)."""

import pytest

from api.extraction.entities import is_defined_term, is_truncated, same_party

# Curly quotation marks, built from code points so the source stays ASCII.
LDQ, RDQ, LSQ, RSQ = chr(0x201C), chr(0x201D), chr(0x2018), chr(0x2019)


@pytest.mark.parametrize(
    "text",
    [
        'Ashgrove Analytics Ltd ("Ashgrove") supplies the services.',
        'Ashgrove Analytics Ltd (the "Ashgrove") supplies the services.',
        f"Ashgrove Analytics Ltd ({LDQ}Ashgrove{RDQ}) supplies the services.",
        f"Ashgrove Analytics Ltd ({LSQ}Ashgrove{RSQ}) supplies the services.",
        'Ashgrove Analytics Ltd, hereinafter "Ashgrove", supplies the services.',
        'Ashgrove Analytics Ltd, hereinafter referred to as "Ashgrove", supplies them.',
        "Ashgrove Analytics Ltd trading as Ashgrove supplies the services.",
        'Ashgrove Analytics Ltd, trading as "Ashgrove", supplies the services.',
        "Ashgrove Analytics Ltd t/a Ashgrove supplies the services.",
        "Ashgrove Analytics Ltd, doing business as Ashgrove, supplies the services.",
        "Ashgrove Analytics Ltd, operating as Ashgrove, supplies the services.",
        "Ashgrove Analytics Ltd operates under the name Ashgrove.",
        "Ashgrove Analytics Ltd, also known as Ashgrove, supplies the services.",
        "Ashgrove Analytics Ltd, TRADING AS Ashgrove, supplies the services.",
    ],
)
def test_a_defined_term_or_alias_is_recognised(text: str) -> None:
    assert is_defined_term("Ashgrove", [text])


@pytest.mark.parametrize(
    "text",
    [
        'Ashgrove Analytics Ltd (the "Supplier") supplies the services.',
        "Ashgrove supplies the services to Birchwood.",
        "Ashgrove's services are described in Schedule 1.",
        'The "Services" are supplied by Ashgrove.',
        "Ashgrovex Ltd is not the same name.",
    ],
)
def test_an_ordinary_mention_is_not_a_defined_term(text: str) -> None:
    assert not is_defined_term("Ashgrove", [text])


def test_a_defined_term_is_found_on_any_page_and_across_line_breaks() -> None:
    assert is_defined_term("Ashgrove Analytics", ["Page one.", 'X Ltd ("Ashgrove\nAnalytics")'])


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("Ashgrove", "Ashgrove Analytics Ltd supplies the services."),
        ("Ashgrove Analytics", "Ashgrove Analytics Ltd supplies the services."),
        ("Ashgrove Analytics", "Ashgrove Analytics Limited supplies the services."),
        ("Ashgrove", "Ashgrove & Daughters Ltd supplies the services."),
        ("Ashgrove Analytics", "made between Ashgrove Analytics\nLtd and Birchwood plc"),
        ("Ashgrove Analytics", "made between Ashgrove Analytics\n  GmbH and Birchwood plc"),
        ("Ashgrove Inc.", "Signed for Ashgrove Inc. Ltd."),
    ],
)
def test_a_name_continued_into_a_longer_name_is_truncated(name: str, text: str) -> None:
    assert is_truncated(name, [text])


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("Ashgrove Analytics Ltd", "Ashgrove Analytics Ltd supplies the services."),
        ("Ashgrove Analytics Ltd", 'Ashgrove Analytics Ltd (the "Supplier") and Birchwood plc'),
        ("Ashgrove Analytics Ltd", "Provider: Ashgrove Analytics Ltd\nCustomer: Birchwood plc"),
        ("Ashgrove Analytics Ltd", "Signed: Ashgrove Analytics Ltd, London."),
        ("Ashgrove", "Vendor: Ashgrove\nPurchaser: Birchwood"),
        ("Ashgrove", "Ashgrove supplies the services."),
        ("Ashgrove Inc.", "The supplier is Ashgrove Inc. The client pays."),
        ("Ashgrove Analytics", "Ashgrove Analyticsx is another word."),
        ("Ashgrove", "xAshgrove Analytics Ltd is not this name."),
    ],
)
def test_a_complete_name_is_not_truncated(name: str, text: str) -> None:
    assert not is_truncated(name, [text])


def test_a_truncation_anywhere_in_the_document_counts() -> None:
    pages = ["Supplier: Ashgrove", "Annex.", "Ashgrove Analytics Ltd signs below."]
    assert is_truncated("Ashgrove", pages)


def test_empty_names_never_match() -> None:
    assert not is_defined_term("", ['"" ()'])
    assert not is_truncated("   ", ["A Ltd"])


def test_same_party_ignores_case_and_spacing_only() -> None:
    assert same_party("Ashgrove Analytics Ltd", "ashgrove  analytics\nLTD")
    assert not same_party("Ashgrove Analytics Ltd", "Ashgrove Analytics Limited")
