"""Seules fonctions exécutées par le pool de sous-processus (docs/ARCHITECTURE.md §3).

Chaque tâche renvoie au plus un petit lot de pages, que le processus principal écrit
aussitôt dans le cache : un arrêt ne perd que le lot en cours. Les erreurs sont toujours
converties en ``ExtractionError``, sérialisable.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

from archipelle.extraction.formats import FormatKind, extension_of, kind_of
from archipelle.extraction.types import (
    ErrorCode,
    ExtractionError,
    OcrConfig,
    PageText,
    TextSearchResult,
    TextSlice,
    WholeDocument,
)


def worker_initializer() -> None:
    """Initialisation de chaque sous-processus."""
    os.environ["OMP_THREAD_LIMIT"] = "1"
    logging.getLogger().addHandler(logging.NullHandler())


def _guarded[T](action: Callable[[], T]) -> T:
    try:
        return action()
    except ExtractionError:
        raise
    except MemoryError as exc:
        raise ExtractionError(ErrorCode.CRASHED, "mémoire insuffisante") from exc
    except Exception as exc:  # fichier malformé : toute erreur de bibliothèque
        raise ExtractionError(ErrorCode.CORRUPT, f"{type(exc).__name__}: {exc}") from exc


def pdf_page_count(path: str) -> int:
    from archipelle.extraction import pdf

    return _guarded(lambda: pdf.page_count(path))


def pdf_native_pages(path: str, first: int, last: int) -> list[PageText]:
    from archipelle.extraction import pdf

    return _guarded(lambda: pdf.native_pages(path, first, last))


def pdf_ocr_page(path: str, page: int, config: OcrConfig) -> PageText:
    from archipelle.extraction import pdf

    return _guarded(lambda: pdf.ocr_page(path, page, config))


def extract_document(path: str) -> WholeDocument:
    from archipelle.extraction import mail_html, office

    extractors: dict[str, Callable[[str], WholeDocument]] = {
        "docx": office.extract_docx,
        "xlsx": office.extract_xlsx,
        "pptx": office.extract_pptx,
        "eml": mail_html.extract_eml,
        "html": mail_html.extract_html,
        "htm": mail_html.extract_html,
    }
    extractor = extractors.get(extension_of(path))
    if extractor is None or kind_of(path) is not FormatKind.DOCUMENT:
        raise ExtractionError(ErrorCode.UNSUPPORTED)
    return _guarded(lambda: extractor(path))


def ocr_image(path: str, config: OcrConfig) -> WholeDocument:
    from archipelle.extraction import images

    return _guarded(lambda: images.ocr_image(path, config))


def read_text_slice(path: str, offset: int, max_chars: int) -> TextSlice:
    from archipelle.extraction import text_files

    return _guarded(lambda: text_files.read_slice(path, offset, max_chars))


def search_text_file(
    path: str, needles: list[str], max_hits: int, snippet_chars: int
) -> TextSearchResult:
    from archipelle.extraction import text_files

    return _guarded(lambda: text_files.search_file(path, needles, max_hits, snippet_chars))


def sleep_for_tests(seconds: float) -> str:
    """Tâche factice utilisée par les tests d'interruption du pool."""
    import time

    time.sleep(seconds)
    return "fini"
