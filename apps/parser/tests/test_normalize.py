from docparser.normalize import normalize


def test_nfkc_folds_compatibility_characters() -> None:
    assert normalize("\ufb01nal \u2460").text == "final 1"


def test_format_and_control_characters_are_removed_and_counted() -> None:
    result = normalize("Effective\u200b Da\u200dte\u202e:\x07 1\u00adMarch")
    assert result.text == "Effective Date: 1March"
    assert result.control_chars_removed == 5


def test_newlines_and_tabs_are_kept_not_counted() -> None:
    result = normalize("a\tb\nc")
    assert result.text == "a b\nc"
    assert result.control_chars_removed == 0


def test_line_endings_spaces_and_blank_lines_are_unified() -> None:
    raw = "  Title \r\n\r\n\r\n\r\nA  \u00a0 line \rnext\n\n\n"
    assert normalize(raw).text == "Title\n\nA line\nnext"


def test_normalization_is_idempotent() -> None:
    once = normalize("x\u200b  y\r\n\n\n\nz").text
    assert normalize(once).text == once
