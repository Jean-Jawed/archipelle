"""Types échangés entre les sous-processus d'extraction et le processus principal.

Tout ce qui est défini ici doit rester sérialisable par ``pickle`` : les résultats et les
erreurs traversent la frontière entre processus.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ExtractionMethod(StrEnum):
    NATIVE = "native"  # texte natif (« natif » du CDC)
    OCR = "ocr"
    PENDING = "pending"  # page scannée pas encore passée en OCR (« non_traitée »)


class ErrorCode(StrEnum):
    NOT_FOUND = "not_found"
    PERMISSION = "permission"
    OFFLINE = "offline"  # fichier cloud « en ligne uniquement »
    PASSWORD = "password"
    CORRUPT = "corrupt"
    UNSUPPORTED = "unsupported"
    OCR_UNAVAILABLE = "ocr_unavailable"
    OCR_TIMEOUT = "ocr_timeout"
    READ_ERROR = "read_error"
    CRASHED = "crashed"  # processus d'extraction mort (fichier malformé, mémoire…)


class ExtractionError(Exception):
    """Échec d'extraction. ``args`` reste sérialisable pour traverser les processus."""

    def __init__(self, code: ErrorCode | str, detail: str = "") -> None:
        super().__init__(str(code), detail)
        self.code = ErrorCode(code)
        self.detail = detail


class DocumentNote(StrEnum):
    """Avertissements attachés à un document, affichés à l'agent avec son contenu."""

    XLSX_MISSING_VALUES = "xlsx_missing_values"
    EML_ATTACHMENTS = "eml_attachments"
    TRUNCATED = "truncated"
    NO_TEXT = "no_text"


@dataclass(frozen=True)
class PageText:
    page: int  # numérotation à partir de 1
    text: str  # texte d'origine de la page ; pour une page en attente d'OCR, son texte natif
    method: ExtractionMethod


@dataclass(frozen=True)
class NoteEntry:
    note: DocumentNote
    detail: str = ""


@dataclass(frozen=True)
class WholeDocument:
    """Document non paginé : une seule page, numérotée 1."""

    text: str
    method: ExtractionMethod
    notes: list[NoteEntry] = field(default_factory=list[NoteEntry])

    def page(self) -> PageText:
        return PageText(1, self.text, self.method)


@dataclass(frozen=True)
class TextSlice:
    text: str
    offset: int
    total_chars: int


@dataclass(frozen=True)
class TextHit:
    offset: int
    length: int
    snippet: str


@dataclass(frozen=True)
class TextSearchResult:
    total: int
    hits: list[TextHit]  # les premières occurrences seulement


@dataclass(frozen=True)
class OcrConfig:
    """Réglages OCR transmis aux sous-processus (CDC §3)."""

    command: str
    languages: str = "fra+eng"
    dpi: int = 300
    page_timeout_s: float = 60.0


# Seuils de détection d'une page scannée (CDC §3) 🔧
SCAN_MIN_NATIVE_CHARS = 50
SCAN_IMAGE_COVERAGE = 0.80
SCAN_MAX_NATIVE_CHARS_WITH_IMAGE = 300
# Taille maximale du texte gardé pour un document non paginé 🔧
MAX_DOCUMENT_CHARS = 20_000_000
