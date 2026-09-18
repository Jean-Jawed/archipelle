"""Contexte et résultat communs à tous les outils (docs/ARCHITECTURE.md §4.3)."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

from archipelle.core.cancel import CancelToken
from archipelle.core.outcome import AppError, Cancelled
from archipelle.extraction.types import OcrConfig
from archipelle.persistence.settings_store import ScanExclusions
from archipelle.tools.paths import Workspace

if TYPE_CHECKING:
    from archipelle.tools.documents import DocumentAccess
    from archipelle.tools.process_pool import ExtractionPool

type Mode = Literal["quick", "explore"]
type StopReason = Literal["cancel", "deadline"]


@dataclass(frozen=True)
class ToolLimits:
    """Plafonds des outils (CDC §4, §4bis, §7) 🔧"""

    list_max: int = 500
    search_files_max: int = 100
    fulltext_files: int = 30
    fulltext_snippets: int = 3
    snippet_chars: int = 200
    fulltext_seconds: float = 30.0
    fulltext_new_files: int = 300
    read_max_chars: int = 20_000
    tool_timeout_s: float = 60.0
    pdf_batch_pages: int = 20
    walk_max_entries: int = 200_000
    journal_max_entries: int = 200


class Stop:
    """Condition d'arrêt : bouton stop (jeton) ou échéance (monotone)."""

    def __init__(self, cancel: CancelToken, deadline: float) -> None:
        self.cancel = cancel
        self.deadline = deadline

    def reason(self) -> StopReason | None:
        if self.cancel.cancelled:
            return "cancel"
        if time.monotonic() >= self.deadline:
            return "deadline"
        return None

    def check_cancel(self) -> None:
        self.cancel.raise_if_cancelled()

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def narrowed(self, seconds: float) -> Stop:
        return Stop(self.cancel, min(self.deadline, time.monotonic() + seconds))

    def sleep(self, seconds: float) -> None:
        """Attente interrompue par le bouton stop."""
        if self.cancel.wait(min(seconds, self.remaining())):
            raise Cancelled()


class IgnoredReason(StrEnum):
    """Motifs du journal des fichiers ignorés (CDC §3)."""

    UNSUPPORTED_FORMAT = "unsupported_format"
    READ_ERROR = "read_error"
    SCAN_NOT_PROCESSED = "scan_not_processed"
    OCR_UNAVAILABLE = "ocr_unavailable"
    NO_OCR_TEXT = "no_ocr_text"
    SEARCH_CAP = "search_cap"


@dataclass(frozen=True)
class IgnoredEntry:
    rel_path: str
    reason: IgnoredReason
    detail: str = ""


@dataclass(frozen=True)
class ConsultedFile:
    rel_path: str
    from_cache: bool


@dataclass
class ToolOutcome:
    ok: bool
    content: str
    consulted: list[ConsultedFile] = field(default_factory=list[ConsultedFile])
    ignored: list[IgnoredEntry] = field(default_factory=list[IgnoredEntry])
    ignored_overflow: int = 0  # entrées de journal non conservées (plafond)

    @property
    def size_chars(self) -> int:
        return len(self.content)


class ToolInputError(AppError):
    """Paramètre invalide : le message est renvoyé au modèle pour qu'il corrige son appel."""


type ProgressSink = Callable[[str, dict[str, Any]], None]


def _ignore_progress(key: str, params: dict[str, Any]) -> None:
    del key, params


@dataclass
class ToolContext:
    workspace: Workspace
    mode: Mode
    ocr: OcrConfig | None  # None : Tesseract absent
    cancel: CancelToken
    turn_deadline: float  # échéance monotone de la question en cours
    pool: ExtractionPool
    documents: DocumentAccess
    exclusions: ScanExclusions
    limits: ToolLimits = field(default_factory=ToolLimits)
    progress: ProgressSink = _ignore_progress

    @property
    def explore(self) -> bool:
        return self.mode == "explore"

    def stop(self, seconds: float | None = None) -> Stop:
        """Condition d'arrêt de l'outil : échéance de la question, éventuellement réduite."""
        stop = Stop(self.cancel, self.turn_deadline)
        return stop.narrowed(seconds) if seconds is not None else stop
