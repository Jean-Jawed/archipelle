"""Clé d'un document dans le cache (CDC §4bis).

Le chemin absolu résolu est normalisé en NFC, et mis en minuscules sous Windows et macOS,
avant hachage : un même fichier n'a jamais deux entrées.
"""

from __future__ import annotations

import hashlib
import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from archipelle.core import system


def doc_key(abs_path: Path) -> str:
    normalized = unicodedata.normalize("NFC", str(abs_path))
    if system.CASE_INSENSITIVE_FS:
        normalized = normalized.casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Fingerprint:
    """Partie variable de la clé d'invalidation : date de modification et taille."""

    mtime_ns: int
    size: int

    @classmethod
    def of(cls, stat: os.stat_result) -> Fingerprint:
        return cls(mtime_ns=stat.st_mtime_ns, size=stat.st_size)
