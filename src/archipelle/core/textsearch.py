"""Normalisation de recherche commune à ``search_files`` et ``search_fulltext`` (CDC §4).

Règles appliquées identiquement aux mots-clés et au texte fouillé :

- insensible à la casse et aux accents (« échéance » trouve « ECHEANCE ») ;
- ligatures et formes de compatibilité dépliées (« ﬁ » → « fi », « œ » → « oe ») ;
- suites d'espaces, tabulations et retours à la ligne réduites à une espace ;
- coupure de mot en fin de ligne (lettre, ``-``, retour à la ligne, lettre) supprimée ;
- trait d'union conditionnel (U+00AD) supprimé ;
- texte simple, sans expressions régulières.

La normalisation produit une table de correspondance : pour chaque caractère du texte
normalisé, sa position dans le texte d'origine. Les positions renvoyées au modèle sont
toujours celles du texte d'origine de la page, utilisables telles quelles par
``read_file``.
"""

from __future__ import annotations

import unicodedata
from array import array
from dataclasses import dataclass
from functools import lru_cache

from archipelle.core.outcome import AppError

MIN_KEYWORD_LENGTH = 3
DEFAULT_SNIPPET_CHARS = 200

_SOFT_HYPHEN = "\u00ad"
_HYPHENS = frozenset("-\u2010\u2011")  # trait d'union, trait d'union insécable
_NEWLINES = frozenset("\n\r\u2028\u2029\x0b\x0c\x85")
# Lettres que la décomposition Unicode ne sépare pas.
_EXTRA_FOLDS = {
    "œ": "oe",
    "æ": "ae",
    "ø": "o",
    "ł": "l",
    "đ": "d",
    "ı": "i",
    "ß": "ss",
    "\u2019": "'",  # apostrophes typographiques
    "\u2018": "'",
    "\u02bc": "'",
}


class KeywordTooShort(AppError):
    def __init__(self, keyword: str) -> None:
        super().__init__("errors.search.keyword_too_short", keyword=keyword, min=MIN_KEYWORD_LENGTH)


class NoUsableKeyword(AppError):
    def __init__(self) -> None:
        super().__init__("errors.search.no_keyword")


@lru_cache(maxsize=4096)
def _fold_char(char: str) -> str:
    """Forme normalisée d'un caractère non blanc (peut être vide ou multiple)."""
    if char.isascii():
        return char.lower()
    folded: list[str] = []
    for piece in unicodedata.normalize("NFKD", char):
        if unicodedata.combining(piece):
            continue
        lowered = piece.casefold()
        for sub in lowered:
            folded.append(_EXTRA_FOLDS.get(sub, sub))
    return "".join(folded)


@dataclass(frozen=True)
class NormalizedText:
    text: str
    origin: array[int]  # origin[i] : position dans l'original du caractère normalisé i
    source: str  # texte d'origine (référence, pas de copie)

    def to_source_span(self, start: int, end: int) -> tuple[int, int]:
        """Intervalle d'origine couvrant les caractères normalisés ``[start, end[``."""
        if end <= start:
            position = self.origin[start] if start < len(self.origin) else len(self.source)
            return position, position
        source_end = self.origin[end - 1] + 1
        # Inclut les accents combinants qui suivent (texte d'origine en NFD).
        while source_end < len(self.source) and unicodedata.combining(self.source[source_end]):
            source_end += 1
        return self.origin[start], source_end


def _is_word_char(char: str) -> bool:
    # Lettres seulement : « 2023-⏎2024 » n'est pas une coupure de mot.
    return char.isalpha() or unicodedata.combining(char) > 0


def _hyphen_break_end(raw: str, index: int) -> int:
    """Si ``raw[index]`` est une coupure de fin de ligne, position de la lettre qui suit la
    coupure ; sinon -1. Forme reconnue : lettre, tiret, [blancs], saut de ligne, [blancs],
    lettre."""
    if index == 0 or not _is_word_char(raw[index - 1]):
        return -1
    length = len(raw)
    cursor = index + 1
    while cursor < length and raw[cursor] in " \t":
        cursor += 1
    if cursor >= length or raw[cursor] not in _NEWLINES:
        return -1
    while cursor < length and raw[cursor].isspace():
        cursor += 1
    if cursor < length and _is_word_char(raw[cursor]):
        return cursor
    return -1


def normalize(raw: str) -> NormalizedText:
    out: list[str] = []
    origin = array("I")
    length = len(raw)
    index = 0
    pending_space = -1  # position du premier blanc d'une suite en cours
    while index < length:
        char = raw[index]
        if char.isspace():
            if pending_space < 0:
                pending_space = index
            index += 1
            continue
        if char in _HYPHENS:
            resume = _hyphen_break_end(raw, index)
            if resume >= 0:
                index = resume  # les blancs de la coupure disparaissent avec elle
                pending_space = -1
                continue
        if char == _SOFT_HYPHEN:
            index += 1
            continue
        folded = _fold_char(char)
        if not folded:
            index += 1
            continue
        if pending_space >= 0:
            out.append(" ")
            origin.append(pending_space)
            pending_space = -1
        for piece in folded:
            if piece.isspace():  # forme de compatibilité contenant une espace
                if out and out[-1] == " ":
                    continue
                piece = " "
            out.append(piece)
            origin.append(index)
        index += 1
    if pending_space >= 0:
        out.append(" ")
        origin.append(pending_space)
    return NormalizedText(text="".join(out), origin=origin, source=raw)


def normalize_keyword(keyword: str) -> str:
    """Mot-clé normalisé, sans espaces aux extrémités. Refusé sous 3 caractères utiles."""
    normalized = normalize(keyword).text.strip()
    if len(normalized.replace(" ", "")) < MIN_KEYWORD_LENGTH:
        raise KeywordTooShort(keyword.strip())
    return normalized


def prepare_keywords(keywords: list[str]) -> list[str]:
    """Normalise une liste de mots-clés (combinés en OU), sans doublons.

    Lève ``KeywordTooShort`` au premier mot-clé trop court, et ``NoUsableKeyword`` si la
    liste est vide.
    """
    prepared: list[str] = []
    for keyword in keywords:
        normalized = normalize_keyword(keyword)
        if normalized not in prepared:
            prepared.append(normalized)
    if not prepared:
        raise NoUsableKeyword()
    return prepared


@dataclass(frozen=True)
class Match:
    offset: int  # position dans le texte d'origine
    length: int  # longueur dans le texte d'origine
    keyword: str  # mot-clé normalisé qui a produit l'occurrence


def find_matches(normalized: NormalizedText, needles: list[str]) -> list[Match]:
    """Toutes les occurrences des mots-clés (déjà normalisés), triées par position.

    Deux mots-clés trouvés au même endroit ne comptent qu'une fois (le plus long est gardé).
    """
    found: dict[int, Match] = {}
    haystack = normalized.text
    for needle in needles:
        start = haystack.find(needle)
        while start >= 0:
            end = start + len(needle)
            source_start, source_end = normalized.to_source_span(start, end)
            match = Match(source_start, source_end - source_start, needle)
            previous = found.get(source_start)
            if previous is None or previous.length < match.length:
                found[source_start] = match
            start = haystack.find(needle, start + 1)
    return [found[key] for key in sorted(found)]


def find(raw: str, needles: list[str]) -> list[Match]:
    return find_matches(normalize(raw), needles)


def contains_any(raw: str, needles: list[str]) -> bool:
    """Test rapide sur un nom de fichier ou un court texte."""
    text = normalize(raw).text
    return any(needle in text for needle in needles)


def snippet(raw: str, match: Match, width: int = DEFAULT_SNIPPET_CHARS) -> str:
    """Court extrait autour d'une occurrence, blancs réduits, avec points de suspension."""
    margin = max(0, (width - match.length) // 2)
    start = max(0, match.offset - margin)
    end = min(len(raw), match.offset + match.length + margin)
    # Évite de couper un mot en début ou en fin d'extrait quand c'est possible.
    if start > 0:
        space = raw.find(" ", start, match.offset)
        if 0 <= space < match.offset and space - start < 20:
            start = space + 1
    if end < len(raw):
        space = raw.rfind(" ", match.offset + match.length, end)
        if space > match.offset + match.length and end - space < 20:
            end = space
    body = " ".join(raw[start:end].split())
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(raw) else ""
    return f"{prefix}{body}{suffix}"
