from __future__ import annotations

import time
import unicodedata

import pytest

from archipelle.core.textsearch import (
    KeywordTooShort,
    NoUsableKeyword,
    contains_any,
    find,
    normalize,
    normalize_keyword,
    prepare_keywords,
    snippet,
)


def _hits(raw: str, *keywords: str) -> list[str]:
    """Textes d'origine couverts par chaque occurrence."""
    needles = prepare_keywords(list(keywords))
    return [raw[m.offset : m.offset + m.length] for m in find(raw, needles)]


def test_case_and_accents() -> None:
    raw = "La date d'ÉCHÉANCE est fixée. Voir echeance."
    assert _hits(raw, "échéance") == ["ÉCHÉANCE", "echeance"]


def test_positions_point_into_original_text() -> None:
    raw = "Préambule\n\nLoyer   annuel : 12 000 €"
    [match] = find(raw, prepare_keywords(["loyer annuel"]))
    assert match.offset == raw.index("Loyer")
    assert raw[match.offset : match.offset + match.length] == "Loyer   annuel"


def test_nfd_source_text_is_fully_covered() -> None:
    raw = unicodedata.normalize("NFD", "Date d'échéance")
    assert _hits(raw, "ECHEANCE") == [unicodedata.normalize("NFD", "échéance")]


def test_whitespace_runs_and_line_breaks() -> None:
    raw = "montant\n\t  total\r\nTTC"
    assert _hits(raw, "montant total ttc") == [raw]


def test_hyphenated_line_break_is_removed() -> None:
    raw = "le calendrier d'échéan-\n   cier est joint"
    assert _hits(raw, "échéancier") == ["échéan-\n   cier"]


def test_hyphen_with_trailing_spaces_before_newline() -> None:
    assert _hits("rembour- \nsement", "remboursement") == ["rembour- \nsement"]


def test_real_hyphens_and_numbers_are_kept() -> None:
    assert normalize("état - major").text == "etat - major"
    assert normalize("période 2023-\n2024").text == "periode 2023- 2024"
    assert normalize("porte-monnaie").text == "porte-monnaie"


def test_ligatures_and_special_letters() -> None:
    assert _hits("Plan de ﬁnancement", "financement") == ["ﬁnancement"]
    assert _hits("Au cœur du sujet", "coeur") == ["cœur"]
    assert _hits("AU COEUR DU SUJET", "cœur") == ["COEUR"]
    assert normalize("Straße").text == "strasse"


def test_non_breaking_and_soft_hyphen() -> None:
    assert _hits("Total : 1\u00a0240\u202f€", "1 240 €") == ["1\u00a0240\u202f€"]
    assert _hits("les loy\u00aders impayés", "loyers") == ["loy\u00aders"]


def test_mapping_is_consistent_for_every_character() -> None:
    raw = "É\u0301tat  ﬁnal\n- Œuvre—test\u00a0x"
    normalized = normalize(raw)
    assert len(normalized.origin) == len(normalized.text)
    assert list(normalized.origin) == sorted(normalized.origin)
    assert all(0 <= position < len(raw) for position in normalized.origin)


@pytest.mark.parametrize("keyword", ["an", "  é ", "a b", "", "--"])
def test_short_keywords_are_refused(keyword: str) -> None:
    with pytest.raises(KeywordTooShort):
        normalize_keyword(keyword)


def test_keyword_normalization() -> None:
    assert normalize_keyword("  Échéance\tDU  bail ") == "echeance du bail"
    assert normalize_keyword("abc") == "abc"


def test_prepare_keywords_deduplicates_and_requires_one() -> None:
    assert prepare_keywords(["Loyer", "loyer", "LOYERS"]) == ["loyer", "loyers"]
    with pytest.raises(NoUsableKeyword):
        prepare_keywords([])


def test_overlapping_keywords_count_once() -> None:
    raw = "Les loyers et le loyer."
    matches = find(raw, prepare_keywords(["loyer", "loyers"]))
    assert [(raw[m.offset : m.offset + m.length]) for m in matches] == ["loyers", "loyer"]


def test_all_occurrences_are_found() -> None:
    raw = "bail " * 50
    assert len(find(raw, prepare_keywords(["bail"]))) == 50


def test_contains_any_on_file_names() -> None:
    needles = prepare_keywords(["échéance"])
    assert contains_any("Bail_ECHEANCE_2022.pdf", needles)
    assert not contains_any("facture.pdf", needles)


def test_snippet_is_short_and_centered() -> None:
    raw = ("mot " * 200) + "CLAUSE de résiliation anticipée " + ("mot " * 200)
    [match] = find(raw, prepare_keywords(["résiliation"]))
    text = snippet(raw, match)
    assert "résiliation" in text
    assert text.startswith("…") and text.endswith("…")
    assert len(text) <= 205


def test_snippet_at_text_boundaries() -> None:
    raw = "résiliation\nimmédiate"
    [match] = find(raw, prepare_keywords(["résiliation"]))
    assert snippet(raw, match) == "résiliation immédiate"


def test_empty_span() -> None:
    normalized = normalize("abc")
    assert normalized.to_source_span(3, 3) == (3, 3)


def test_normalization_speed() -> None:
    raw = ("Le locataire s'engage à régler l'échéance trimestrielle. " * 18_000)[:1_000_000]
    started = time.perf_counter()
    normalized = normalize(raw)
    elapsed = time.perf_counter() - started
    assert len(normalized.text) > 900_000
    assert elapsed < 10, f"normalisation trop lente : {elapsed:.2f} s pour 1 Mo"


def test_typographic_apostrophes() -> None:
    assert _hits("en milliers d’euros", "milliers d'euros") == ["milliers d’euros"]
