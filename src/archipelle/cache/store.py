"""Cache d'extraction : texte des pages sur disque, index SQLite (CDC §4bis).

Écrit uniquement par le processus principal. Les écritures de fichiers sont atomiques
(fichier temporaire puis renommage) ; l'index est mis à jour après chaque page.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path

from archipelle.cache.index import CacheIndex, DocumentRecord, PageRecord
from archipelle.cache.keys import Fingerprint, doc_key
from archipelle.extraction.types import (
    ErrorCode,
    ExtractionError,
    ExtractionMethod,
    NoteEntry,
    PageText,
)
from archipelle.persistence.db import Database

_log = logging.getLogger(__name__)
DEFAULT_MAX_BYTES = 500 * 1024 * 1024  # 🔧
# Échecs qui se reproduiraient à l'identique tant que le fichier ne change pas.
PERMANENT_FAILURES = frozenset({ErrorCode.PASSWORD, ErrorCode.CORRUPT})


@dataclass
class CachedDocument:
    """Vue d'un document en cache, valide pour l'empreinte du fichier sur disque."""

    key: str
    page_count: int | None
    notes: list[NoteEntry]
    pages: dict[int, PageRecord] = field(default_factory=dict[int, PageRecord])
    invalidated: bool = False  # une ancienne entrée a été écartée (fichier modifié)
    failure: ExtractionError | None = None  # échec permanent mémorisé

    def has_page(self, page: int, *, need_ocr: bool = False) -> bool:
        record = self.pages.get(page)
        if record is None:
            return False
        return not (need_ocr and record.method is ExtractionMethod.PENDING)


class ExtractionCache:
    def __init__(self, root: Path, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self.root = root
        self.docs_dir = root / "docs"
        self.max_bytes = max_bytes
        self._lock = threading.RLock()
        self._in_use: dict[str, int] = {}
        self._open()

    def _open(self) -> None:
        self.docs_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(self.root / "index.db", "cache")
        self.db.migrate()
        self.index = CacheIndex(self.db)

    def close(self) -> None:
        self.db.close_all()

    # --- consultation ---------------------------------------------------------------

    def open_document(self, abs_path: Path, fingerprint: Fingerprint) -> CachedDocument:
        """Entrée du document, créée ou remise à zéro si le fichier a changé."""
        key = doc_key(abs_path)
        with self._lock:
            record = self.index.get(key)
            invalidated = False
            if record is None or record.fingerprint != fingerprint:
                invalidated = record is not None
                if invalidated:
                    self._remove_files(key)
                record = self.index.create(key, str(abs_path), fingerprint)
            else:
                self.index.touch(key)
            return self._view(record, invalidated)

    @staticmethod
    def _view(record: DocumentRecord, invalidated: bool) -> CachedDocument:
        return CachedDocument(
            key=record.doc_key,
            page_count=record.page_count,
            notes=list(record.notes),
            pages=dict(record.pages),
            invalidated=invalidated,
            failure=ExtractionError(*record.failure) if record.failure else None,
        )

    def read_page(self, key: str, page: int) -> str | None:
        try:
            return self._page_file(key, page).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    # --- écriture -------------------------------------------------------------------

    def acquire(self, key: str) -> None:
        """Protège un document de l'éviction pendant qu'il est utilisé."""
        with self._lock:
            self._in_use[key] = self._in_use.get(key, 0) + 1

    def release(self, key: str) -> None:
        with self._lock:
            count = self._in_use.get(key, 0) - 1
            if count <= 0:
                self._in_use.pop(key, None)
            else:
                self._in_use[key] = count

    def set_page_count(self, doc: CachedDocument, count: int) -> None:
        with self._lock:
            self.index.set_page_count(doc.key, count)
            doc.page_count = count

    def set_notes(self, doc: CachedDocument, notes: list[NoteEntry]) -> None:
        with self._lock:
            self.index.set_notes(doc.key, notes)
            doc.notes = list(notes)

    def remember_failure(self, doc: CachedDocument, error: ExtractionError) -> None:
        """Mémorise un échec permanent ; les autres (fichier verrouillé…) sont ignorés."""
        if error.code not in PERMANENT_FAILURES:
            return
        with self._lock:
            self.index.set_failure(doc.key, error.code, error.detail)
            doc.failure = error

    def write_pages(self, doc: CachedDocument, pages: list[PageText]) -> None:
        with self._lock:
            for page in pages:
                path = self._page_file(doc.key, page.page)
                previous = path.stat().st_size if path.exists() else 0
                data = page.text.encode("utf-8")
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
                tmp.write_bytes(data)
                tmp.replace(path)
                self.index.save_page(
                    doc.key, page.page, page.method, len(page.text), len(data) - previous
                )
                doc.pages[page.page] = PageRecord(page.method, len(page.text))
            self._evict()

    # --- taille, éviction, purge ----------------------------------------------------

    def size_bytes(self) -> int:
        return self.index.total_bytes()

    def _evict(self) -> None:
        total = self.index.total_bytes()
        if total <= self.max_bytes:
            return
        for key, size in self.index.least_recently_used():
            if total <= self.max_bytes:
                break
            if key in self._in_use:
                continue
            self._remove_files(key)
            self.index.delete(key)
            total -= size
            _log.info("Cache : document évincé (%d octets)", size)

    def purge(self) -> None:
        """Supprime tout le cache (CDC §12) puis le recrée vide."""
        with self._lock:
            self.db.close_all()
            shutil.rmtree(self.root, ignore_errors=True)
            self._open()

    def _doc_dir(self, key: str) -> Path:
        return self.docs_dir / key[:2] / key

    def _page_file(self, key: str, page: int) -> Path:
        return self._doc_dir(key) / f"p{page:05d}.txt"

    def _remove_files(self, key: str) -> None:
        shutil.rmtree(self._doc_dir(key), ignore_errors=True)
