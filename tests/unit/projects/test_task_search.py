"""Global search folding follows the client's supported Foundation behavior."""

import unicodedata

import pytest

from octop.infra.projects.task_search import _contains, _fold


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Café", "cafe"),
        ("Cafe\u0301", "cafe"),
        ("ＧｉｔＨｕｂ", "github"),
        ("Ｐｒｏｄｕｃｔ　调研", "product 调研"),
        ("Straße", "strasse"),
        ("Σσς", "σσσ"),
        ("ØŁĐÆŒı", "øłđæœı"),
        ("α\u0301\u0323", "α"),
        ("и\u0301\u0323", "и"),
        ("a\u0345", "a"),
        ("α\u0345", "α"),
        ("\u0345", "ι"),
        ("カ\u0301", "カ"),
        ("一\u0301", "一"),
        ("ا\u0301", "ا"),
        ("क\u0301", "क"),
        ("a\u3099\u064f\u093c", "a"),
        ("a\u034f\ufe0f\u20dd", "a"),
        ("\u0301a", "\u0301a"),
        ("\u034f", "\u034f"),
        ("😀\ufe0f", "😀\ufe0f"),
        ("产品调研 %_!\"'", "产品调研 %_!\"'"),
    ],
)
def test_fold_matches_supported_client_examples(text: str, expected: str) -> None:
    assert _fold(text) == expected


@pytest.mark.parametrize(
    ("text", "different"),
    [
        ("①", "1"),
        ("Ⅳ", "iv"),
        ("ĳ", "ij"),
        ("ª", "a"),
        ("²", "2"),
        ("ガ", "カ"),
        ("كُ", "ك"),
        ("שָ", "ש"),
        ("क़", "क"),
        ("ø", "o"),
        ("ł", "l"),
        ("đ", "d"),
        ("æ", "ae"),
        ("œ", "oe"),
        ("ı", "i"),
    ],
)
def test_fold_preserves_distinctions_the_client_does_not_remove(text: str, different: str) -> None:
    assert _fold(text) != _fold(different)


@pytest.mark.parametrize("text", ["ガ", "क़", "é", "Ά", "Й"])
def test_fold_treats_canonical_forms_equally(text: str) -> None:
    assert _fold(text) == _fold(unicodedata.normalize("NFD", text))


@pytest.mark.parametrize(
    ("text", "incomplete"),
    [
        ("ガ", "カ"),
        ("مُ", "م"),
        ("שָ", "ש"),
        ("क़", "क"),
        ("한", "ᄒ"),
    ],
)
def test_contains_preserves_composite_character_boundaries(text: str, incomplete: str) -> None:
    assert not _contains(_fold(text), _fold(incomplete))
    assert _contains(_fold("前 " + text + " 后"), _fold(text))
