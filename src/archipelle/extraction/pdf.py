"""PDF via pypdfium2 : texte natif page par page, détection des scans, rendu pour l'OCR.

Chaque sous-processus n'exécute qu'une tâche à la fois : pypdfium2 n'est jamais appelé
de façon concurrente dans un même processus (CDC §7bis).
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from archipelle.extraction import ocr
from archipelle.extraction.errors import os_errors
from archipelle.extraction.text_files import normalize_newlines
from archipelle.extraction.types import (
    SCAN_IMAGE_COVERAGE,
    SCAN_MAX_NATIVE_CHARS_WITH_IMAGE,
    SCAN_MIN_NATIVE_CHARS,
    ErrorCode,
    ExtractionError,
    ExtractionMethod,
    OcrConfig,
    PageText,
)

_PDFIUM_PASSWORD_ERRORS = {4, 5}  # FPDF_ERR_PASSWORD, FPDF_ERR_SECURITY


@contextmanager
def open_document(path: str) -> Generator[pdfium.PdfDocument]:
    with os_errors():
        try:
            document = pdfium.PdfDocument(path)
        except pdfium.PdfiumError as exc:
            code: Any = getattr(exc, "err_code", None)
            if code in _PDFIUM_PASSWORD_ERRORS:
                raise ExtractionError(ErrorCode.PASSWORD) from exc
            raise ExtractionError(ErrorCode.CORRUPT, str(exc)) from exc
    try:
        yield document
    finally:
        document.close()


def page_count(path: str) -> int:
    with open_document(path) as document:
        return len(document)


def _meaningful_chars(text: str) -> int:
    return sum(1 for char in text if not char.isspace())


def _image_coverage(pdf_page: pdfium.PdfPage) -> float:
    """Part approximative de la page couverte par des images (somme des surfaces, bornée)."""
    page: Any = pdf_page  # pypdfium2 n'est que partiellement typé
    width, height = (float(v) for v in page.get_size())
    page_area = width * height
    if page_area <= 0:
        return 0.0
    covered = 0.0
    for obj in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE]):
        left, bottom, right, top = (float(v) for v in obj.get_bounds())
        clipped_w = max(0.0, min(right, width) - max(left, 0.0))
        clipped_h = max(0.0, min(top, height) - max(bottom, 0.0))
        covered += clipped_w * clipped_h
        if covered >= page_area:
            return 1.0
    return covered / page_area


def is_scanned(native_text: str, coverage: float) -> bool:
    chars = _meaningful_chars(native_text)
    if chars < SCAN_MIN_NATIVE_CHARS:
        return True
    return coverage > SCAN_IMAGE_COVERAGE and chars < SCAN_MAX_NATIVE_CHARS_WITH_IMAGE


def _native_page(document: pdfium.PdfDocument, number: int) -> PageText:
    page = document[number - 1]
    try:
        textpage = page.get_textpage()
        try:
            text = normalize_newlines(textpage.get_text_range())
        finally:
            textpage.close()
        chars = _meaningful_chars(text)
        coverage = _image_coverage(page) if chars < SCAN_MAX_NATIVE_CHARS_WITH_IMAGE else 0.0
    finally:
        page.close()
    method = ExtractionMethod.PENDING if is_scanned(text, coverage) else ExtractionMethod.NATIVE
    return PageText(number, text, method)


def native_pages(path: str, first: int, last: int) -> list[PageText]:
    """Texte natif des pages ``first`` à ``last`` incluses (numérotées à partir de 1)."""
    with open_document(path) as document:
        last = min(last, len(document))
        return [_native_page(document, number) for number in range(first, last + 1)]


def ocr_page(path: str, number: int, config: OcrConfig) -> PageText:
    with open_document(path) as document:
        if not 1 <= number <= len(document):
            raise ExtractionError(ErrorCode.READ_ERROR, f"page {number}")
        page: Any = document[number - 1]
        try:
            bitmap: Any = page.render(scale=config.dpi / 72)
            try:
                image: Any = bitmap.to_pil()
            finally:
                bitmap.close()
        finally:
            page.close()
    return PageText(number, ocr.image_to_text(image, config), ExtractionMethod.OCR)
