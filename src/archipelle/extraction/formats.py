"""Formats explorés (CDC §3). Module léger : importable sans les bibliothèques d'extraction.

Cette table sert aussi de liste autorisée pour l'ouverture des sources (CDC §9).
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import PurePath


class FormatKind(StrEnum):
    TEXT = "text"  # lecture directe, pas de cache
    PDF = "pdf"  # paginé, cache page par page
    DOCUMENT = "document"  # non paginé, extrait en une fois
    IMAGE = "image"  # OCR


DOCUMENT_EXTENSIONS: dict[str, FormatKind] = {
    "txt": FormatKind.TEXT,
    "md": FormatKind.TEXT,
    "csv": FormatKind.TEXT,
    "pdf": FormatKind.PDF,
    "docx": FormatKind.DOCUMENT,
    "xlsx": FormatKind.DOCUMENT,
    "pptx": FormatKind.DOCUMENT,
    "eml": FormatKind.DOCUMENT,
    "html": FormatKind.DOCUMENT,
    "htm": FormatKind.DOCUMENT,
}

IMAGE_EXTENSIONS: dict[str, FormatKind] = {
    "png": FormatKind.IMAGE,
    "jpg": FormatKind.IMAGE,
    "jpeg": FormatKind.IMAGE,
    "tiff": FormatKind.IMAGE,
    "tif": FormatKind.IMAGE,
}

# Code et configuration : lus tels quels, sans mise en forme. Les fichiers .html restent
# traités comme des pages web (texte visible seulement) : c'est l'usage le plus courant.
CODE_EXTENSIONS: dict[str, FormatKind] = dict.fromkeys(
    (
        "py", "js", "jsx", "ts", "tsx", "css", "scss", "json", "yaml", "yml", "toml",
        "ini", "cfg", "sql", "sh", "bat", "ps1", "java", "c", "h", "cpp", "hpp", "cs",
        "rs", "go", "rb", "php", "swift", "kt", "xml", "log",
    ),
    FormatKind.TEXT,
)  # fmt: skip

EXPLORED: dict[str, FormatKind] = {
    **DOCUMENT_EXTENSIONS,
    **IMAGE_EXTENSIONS,
    **CODE_EXTENSIONS,
}

# Groupes utilisables à la place d'une liste d'extensions dans les outils (CDC §4).
EXTENSION_GROUPS: dict[str, frozenset[str]] = {
    "documents": frozenset(DOCUMENT_EXTENSIONS),
    "code": frozenset(CODE_EXTENSIONS),
    "images": frozenset(IMAGE_EXTENSIONS),
}

# Extensions qu'un double-clic exécute sur au moins un système (Windows lance .js par
# WSH, .py par l'interpréteur, .sh et .bat par le shell). Elles sont lues, jamais ouvertes
# avec l'application par défaut : une source citée de ce type s'affiche sans lien (CDC §9).
EXECUTABLE_EXTENSIONS = frozenset(
    {"sh", "bash", "zsh", "bat", "cmd", "ps1", "py", "rb", "php", "js", "jsx", "mjs", "vbs"}
)

OPENABLE_EXTENSIONS = frozenset(EXPLORED) - EXECUTABLE_EXTENSIONS


def extension_of(name: str | PurePath) -> str:
    """Extension en minuscules, sans le point (``""`` si absente)."""
    suffix = PurePath(name).suffix
    return suffix[1:].lower() if suffix else ""


def normalize_extension(value: str) -> str:
    """« .PDF », « pdf », « *.pdf » → « pdf »."""
    return value.strip().lstrip("*").lstrip(".").lower()


def expand_extensions(values: list[str]) -> set[str]:
    """Développe les groupes (« code », « documents », « images ») et normalise le reste."""
    found: set[str] = set()
    for value in values:
        name = value.strip().lower()
        group = EXTENSION_GROUPS.get(name)
        if group is not None:
            found |= group
            continue
        extension = normalize_extension(value)
        if extension:
            found.add(extension)
    return found


def kind_of(name: str | PurePath) -> FormatKind | None:
    return EXPLORED.get(extension_of(name))


def is_explored(name: str | PurePath) -> bool:
    return kind_of(name) is not None
