"""Accès au texte d'un document : cache, puis extraction par lots, puis mise en cache.

Logique unique partagée par ``read_file`` et ``search_fulltext`` (CDC §4bis) :

- pages manquantes demandées au pool par lots de 20 pages 🔧, écrites dès réception ;
- en mode Exploration, pages scannées complétées par OCR, une page par tâche ;
- une interruption (échéance, bouton stop) conserve toutes les pages déjà écrites.

Les fichiers texte (.txt, .md, .csv) ne passent pas par ici : ils ne sont pas mis en cache.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from archipelle.cache.keys import Fingerprint
from archipelle.cache.store import CachedDocument, ExtractionCache
from archipelle.core.outcome import Cancelled
from archipelle.extraction import tasks
from archipelle.extraction.errors import from_os_error
from archipelle.extraction.formats import FormatKind, kind_of
from archipelle.extraction.types import (
    DocumentNote,
    ErrorCode,
    ExtractionError,
    ExtractionMethod,
    NoteEntry,
    OcrConfig,
    PageText,
    WholeDocument,
)
from archipelle.tools.context import ProgressSink, Stop, StopReason
from archipelle.tools.paths import io_path
from archipelle.tools.process_pool import ExtractionDeadline, ExtractionPool, unwrap


@dataclass
class DocumentLoad:
    """Pages disponibles d'un document pour une demande donnée."""

    kind: FormatKind
    page_count: int
    requested: list[int]
    pages: dict[int, PageText] = field(default_factory=dict[int, PageText])
    missing: list[int] = field(default_factory=list[int])  # non extraites (interruption, erreur)
    notes: list[NoteEntry] = field(default_factory=list[NoteEntry])
    extracted: bool = False  # au moins une extraction a été lancée
    stop_reason: StopReason | None = None
    ocr_unavailable: bool = False
    ocr_failed: list[int] = field(default_factory=list[int])
    error: ExtractionError | None = None  # erreur survenue après des pages déjà obtenues

    @property
    def pending_ocr(self) -> list[int]:
        return [n for n, p in sorted(self.pages.items()) if p.method is ExtractionMethod.PENDING]

    @property
    def from_cache(self) -> bool:
        return not self.extracted

    @property
    def complete(self) -> bool:
        """Toutes les pages demandées sont disponibles. Un chargement interrompu avant même
        de connaître le nombre de pages n'est jamais complet."""
        if self.stop_reason is not None and not self.requested:
            return False
        return not self.missing

    def has_note(self, note: DocumentNote) -> bool:
        return any(entry.note is note for entry in self.notes)


def _stat(abs_path: Path) -> Fingerprint:
    try:
        return Fingerprint.of(os.stat(io_path(abs_path)))  # noqa: PTH116
    except OSError as exc:
        raise from_os_error(exc) from exc


def page_batches(pages: list[int], size: int) -> list[tuple[int, int]]:
    """Regroupe des numéros de pages triés en intervalles contigus de ``size`` pages au plus."""
    batches: list[tuple[int, int]] = []
    for number in pages:
        if batches and number == batches[-1][1] + 1 and number - batches[-1][0] < size:
            batches[-1] = (batches[-1][0], number)
        else:
            batches.append((number, number))
    return batches


class DocumentAccess:
    def __init__(
        self,
        cache: ExtractionCache,
        pool: ExtractionPool,
        ocr: OcrConfig | None,
        batch_pages: int = 20,
    ) -> None:
        self.cache = cache
        self.pool = pool
        self.ocr = ocr
        self.batch_pages = batch_pages
        # Threads durables de la fouille parallèle : leurs connexions SQLite sont réutilisées.
        self.threads = ThreadPoolExecutor(
            max_workers=pool.max_workers, thread_name_prefix="documents"
        )

    def shutdown(self) -> None:
        self.threads.shutdown(wait=True, cancel_futures=True)

    def needs_extraction(self, abs_path: Path, *, want_ocr: bool) -> bool:
        """Vrai si une lecture complète du document lancerait une extraction."""
        kind = kind_of(abs_path)
        if kind is None or kind is FormatKind.TEXT:
            return False
        doc = self.cache.open_document(abs_path, _stat(abs_path))
        if doc.failure is not None:
            return False
        if kind is FormatKind.IMAGE:
            return want_ocr and self.ocr is not None and not doc.has_page(1, need_ocr=True)
        if kind is FormatKind.DOCUMENT:
            return not doc.has_page(1)
        if doc.page_count is None:
            return True
        needed = [n for n in range(1, doc.page_count + 1) if not doc.has_page(n)]
        if needed:
            return True
        return (
            want_ocr
            and self.ocr is not None
            and any(r.method is ExtractionMethod.PENDING for r in doc.pages.values())
        )

    def load(
        self,
        abs_path: Path,
        *,
        want_ocr: bool,
        stop: Stop,
        first_page: int = 1,
        max_pages: int | None = None,
        progress: ProgressSink | None = None,
        rel_path: str = "",
    ) -> DocumentLoad:
        """Charge les pages demandées. Lève ``Cancelled`` si le bouton stop est pressé
        (les pages obtenues restent en cache), ``ExtractionError`` si rien n'a pu être lu."""
        kind = kind_of(abs_path)
        if kind is None or kind is FormatKind.TEXT:
            raise ExtractionError(ErrorCode.UNSUPPORTED)
        doc = self.cache.open_document(abs_path, _stat(abs_path))
        if doc.failure is not None:
            raise doc.failure
        self.cache.acquire(doc.key)
        try:
            job = _LoadJob(self, doc, abs_path, kind, stop, progress, rel_path)
            if kind is FormatKind.PDF:
                result = job.load_pdf(first_page, max_pages, want_ocr)
            else:
                result = job.load_single(want_ocr)
        except ExtractionError as error:
            self.cache.remember_failure(doc, error)
            raise
        finally:
            self.cache.release(doc.key)
        if result.stop_reason == "cancel":
            raise Cancelled()
        return result


class _LoadJob:
    def __init__(
        self,
        access: DocumentAccess,
        doc: CachedDocument,
        abs_path: Path,
        kind: FormatKind,
        stop: Stop,
        progress: ProgressSink | None,
        rel_path: str,
    ) -> None:
        self.access = access
        self.cache = access.cache
        self.pool = access.pool
        self.doc = doc
        self.path = io_path(abs_path)
        self.kind = kind
        self.stop = stop
        self.progress = progress
        self.rel_path = rel_path

    def _report(self, key: str, **params: object) -> None:
        if self.progress is not None:
            self.progress(key, {"path": self.rel_path, **params})

    # --- documents non paginés ------------------------------------------------------

    def load_single(self, want_ocr: bool) -> DocumentLoad:
        result = DocumentLoad(kind=self.kind, page_count=1, requested=[1])
        ocr_needed = self.kind is FormatKind.IMAGE
        cached = self.doc.has_page(1, need_ocr=ocr_needed)
        if not cached:
            if ocr_needed and not want_ocr:
                result.pages[1] = PageText(1, "", ExtractionMethod.PENDING)
                return result
            if ocr_needed and self.access.ocr is None:
                result.ocr_unavailable = True
                result.pages[1] = PageText(1, "", ExtractionMethod.PENDING)
                return result
            result.extracted = True
            if ocr_needed:
                assert self.access.ocr is not None
                self._report("progress.ocr_image")
                whole = self._run(result, tasks.ocr_image, self.path, self.access.ocr)
            else:
                self._report("progress.extracting")
                whole = self._run(result, tasks.extract_document, self.path)
            if whole is None:
                result.missing = [1]
                return result
            self.cache.write_pages(self.doc, [whole.page()])
            self.cache.set_notes(self.doc, whole.notes)
        result.notes = list(self.doc.notes)
        self._read_pages(result, [1])
        return result

    def _run(
        self, result: DocumentLoad, function: Callable[..., WholeDocument], *args: object
    ) -> WholeDocument | None:
        try:
            return self.pool.run(function, *args, stop=self.stop)
        except Cancelled:
            result.stop_reason = "cancel"
            return None
        except ExtractionDeadline:
            result.stop_reason = self.stop.reason() or "deadline"
            return None

    # --- PDF ------------------------------------------------------------------------

    def load_pdf(self, first_page: int, max_pages: int | None, want_ocr: bool) -> DocumentLoad:
        if self.doc.page_count is None:
            try:
                count = self.pool.run(tasks.pdf_page_count, self.path, stop=self.stop)
            except ExtractionDeadline:
                return DocumentLoad(
                    kind=self.kind,
                    page_count=0,
                    requested=[],
                    extracted=True,
                    stop_reason=self.stop.reason() or "deadline",
                )
            except Cancelled:
                return DocumentLoad(self.kind, 0, [], extracted=True, stop_reason="cancel")
            self.cache.set_page_count(self.doc, count)
        count = self.doc.page_count or 0
        last = count if max_pages is None else min(count, first_page + max_pages - 1)
        requested = list(range(max(1, first_page), last + 1))
        result = DocumentLoad(kind=self.kind, page_count=count, requested=requested)

        missing = [n for n in requested if not self.doc.has_page(n)]
        if missing:
            result.extracted = True
            self._extract_native(result, missing)
        if want_ocr and result.stop_reason is None and result.error is None:
            pending = [
                n
                for n in requested
                if n in self.doc.pages and self.doc.pages[n].method is ExtractionMethod.PENDING
            ]
            if pending and self.access.ocr is None:
                result.ocr_unavailable = True
            elif pending:
                result.extracted = True
                self._extract_ocr(result, pending)

        available = [n for n in requested if n in self.doc.pages]
        result.missing = [n for n in requested if n not in self.doc.pages]
        self._read_pages(result, available)
        return result

    def _extract_native(self, result: DocumentLoad, missing: list[int]) -> None:
        batches = page_batches(missing, self.access.batch_pages)
        futures = [self.pool.submit(tasks.pdf_native_pages, self.path, a, b) for a, b in batches]
        total = len(missing)
        for future in self.pool.completed(futures, self.stop):
            try:
                pages = unwrap(future)
            except ExtractionDeadline:
                continue
            except ExtractionError as exc:
                if not self.doc.pages:
                    self._cancel(futures)
                    raise
                result.error = exc
                continue
            self.cache.write_pages(self.doc, pages)
            done = sum(1 for n in missing if n in self.doc.pages)
            self._report("progress.pdf_pages", done=done, total=total)
        result.stop_reason = result.stop_reason or self.stop.reason()

    def _extract_ocr(self, result: DocumentLoad, pending: list[int]) -> None:
        config = self.access.ocr
        assert config is not None
        futures = {self.pool.submit(tasks.pdf_ocr_page, self.path, n, config): n for n in pending}
        done = 0
        for future in self.pool.completed(list(futures), self.stop):
            try:
                page = unwrap(future)
            except ExtractionDeadline:
                continue
            except ExtractionError as exc:
                if exc.code is ErrorCode.OCR_UNAVAILABLE:
                    result.ocr_unavailable = True
                result.ocr_failed.append(futures[future])
                continue
            self.cache.write_pages(self.doc, [page])
            done += 1
            self._report("progress.ocr_pages", done=done, total=len(pending))
        result.ocr_failed.sort()
        result.stop_reason = result.stop_reason or self.stop.reason()

    @staticmethod
    def _cancel(futures: Sequence[Future[Any]]) -> None:
        for future in futures:
            future.cancel()

    def _read_pages(self, result: DocumentLoad, numbers: list[int]) -> None:
        for number in numbers:
            record = self.doc.pages.get(number)
            text = self.cache.read_page(self.doc.key, number) if record else None
            if record is None or text is None:
                result.missing.append(number)
                continue
            result.pages[number] = PageText(number, text, record.method)
        result.missing.sort()
