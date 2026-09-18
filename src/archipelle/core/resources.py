"""Point unique de résolution des ressources embarquées.

Depuis les sources, les ressources sont dans le paquet ``archipelle``. Dans un binaire
figé par PyInstaller, elles sont décompressées sous ``sys._MEIPASS``. Tout accès à une
ressource (catalogue, traductions, prompts, frontend, Tesseract embarqué) passe par ce
module (CDC §15).
"""

from __future__ import annotations

import json
import sys
from functools import cache
from pathlib import Path
from typing import Any

_PACKAGE = "archipelle"


@cache
def package_root() -> Path:
    """Répertoire racine du paquet ``archipelle``, en source comme en binaire figé."""
    frozen_base = getattr(sys, "_MEIPASS", None)
    if frozen_base is not None:
        return Path(frozen_base) / _PACKAGE
    return Path(__file__).resolve().parent.parent


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False)) and hasattr(sys, "_MEIPASS")


def package_path(*parts: str) -> Path:
    """Chemin d'un fichier du paquet (par exemple ``ui/web/index.html``)."""
    return package_root().joinpath(*parts)


def resource_path(*parts: str) -> Path:
    """Chemin d'un fichier du répertoire ``resources``."""
    return package_path("resources", *parts)


def read_text(*parts: str) -> str:
    return resource_path(*parts).read_text(encoding="utf-8")


def load_json(*parts: str) -> Any:
    return json.loads(read_text(*parts))


def bundled_binary_dir() -> Path | None:
    """Répertoire des exécutables embarqués (Tesseract), présent seulement en binaire figé."""
    if not is_frozen():
        return None
    candidate = package_path("bin")
    return candidate if candidate.is_dir() else None
