"""Fichiers texte (.txt, .md, .csv) : décodage avec repli (CDC §3), lecture et fouille."""

from __future__ import annotations

from archipelle.core.textsearch import find_matches, normalize, snippet
from archipelle.extraction.errors import os_errors
from archipelle.extraction.types import TextHit, TextSearchResult, TextSlice

_FALLBACK_ENCODINGS = ("utf-8-sig", "cp1252")


def decode_bytes(data: bytes, preferred: str | None = None) -> str:
    """UTF-8 (avec ou sans BOM), puis cp1252, puis UTF-8 avec remplacement."""
    encodings = ((preferred,) if preferred else ()) + _FALLBACK_ENCODINGS
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def read_text(path: str) -> str:
    with os_errors(), open(path, "rb") as handle:  # noqa: PTH123
        data = handle.read()
    return normalize_newlines(decode_bytes(data))


def read_slice(path: str, offset: int, max_chars: int) -> TextSlice:
    text = read_text(path)
    start = max(0, min(offset, len(text)))
    return TextSlice(text=text[start : start + max_chars], offset=start, total_chars=len(text))


def search(text: str, needles: list[str], max_hits: int, snippet_chars: int) -> TextSearchResult:
    matches = find_matches(normalize(text), needles)
    hits = [
        TextHit(m.offset, m.length, snippet(text, m, snippet_chars)) for m in matches[:max_hits]
    ]
    return TextSearchResult(total=len(matches), hits=hits)


def search_file(
    path: str, needles: list[str], max_hits: int, snippet_chars: int
) -> TextSearchResult:
    return search(read_text(path), needles, max_hits, snippet_chars)
