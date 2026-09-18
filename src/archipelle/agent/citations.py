"""Sources citées par le modèle et vérifiées par le code (CDC §8).

Le modèle cite ``[[chemin]]``. Le code normalise le chemin, le compare aux fichiers
consultés du dossier de travail en vigueur, et marque comme « non vérifiée » toute source
qui n'en fait pas partie : un fichier seulement listé ne peut pas devenir une source.
"""

from __future__ import annotations

import re
import unicodedata

from archipelle.agent.history import Source

CITATION = re.compile(r"\[\[([^\[\]\n]{1,500})\]\]")


def normalize_path(raw: str) -> str:
    cleaned = unicodedata.normalize("NFC", raw).strip().replace("\\", "/")
    while "//" in cleaned:
        cleaned = cleaned.replace("//", "/")
    cleaned = cleaned.strip("/")
    return cleaned.removeprefix("./")


def extract(text: str) -> list[str]:
    """Chemins cités, normalisés, sans doublon, dans l'ordre d'apparition."""
    found: list[str] = []
    for match in CITATION.finditer(text):
        path = normalize_path(match.group(1))
        if path and path not in found:
            found.append(path)
    return found


def verify(text: str, consulted: set[str], workdir: str | None) -> list[Source]:
    consulted_normalized = {normalize_path(path) for path in consulted}
    return [Source(path, workdir, path in consulted_normalized) for path in extract(text)]
