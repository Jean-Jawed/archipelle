"""Pool de sous-processus, cache page par page et accès aux documents."""

from __future__ import annotations

import os
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any

import pytest

from archipelle.cache import keys
from archipelle.cache.store import CachedDocument, ExtractionCache
from archipelle.core import system
from archipelle.core.cancel import CancelToken
from archipelle.core.outcome import Cancelled
from archipelle.extraction import tasks
from archipelle.extraction.types import ErrorCode, ExtractionError, ExtractionMethod, PageText
from archipelle.tools.context import Stop
from archipelle.tools.documents import (
    DocumentAccess,
    page_batches,
)
from archipelle.tools.process_pool import ExtractionDeadline, ExtractionPool
from tests.corpus.make_corpus import LONG_PDF_PAGES, Corpus
from tests.tools.conftest import OCR_CONFIG, Progress, requires_tesseract


def _stop(seconds: float = 60, cancel: CancelToken | None = None) -> Stop:
    return Stop(cancel or CancelToken(), time.monotonic() + seconds)


# --- pool ----------------------------------------------------------------------------


def test_pool_runs_tasks_and_propagates_errors(pool: ExtractionPool, corpus: Corpus) -> None:
    assert (
        pool.run(tasks.pdf_page_count, str(corpus.root / "contrats/bail_2022.pdf"), stop=_stop())
        == 3
    )
    with pytest.raises(ExtractionError) as info:
        pool.run(tasks.pdf_page_count, str(corpus.root / "contrats/protege.pdf"), stop=_stop())
    assert info.value.code is ErrorCode.PASSWORD


def test_pool_deadline_kills_task_and_recovers(pool: ExtractionPool) -> None:
    started = time.monotonic()
    with pytest.raises(ExtractionDeadline):
        pool.run(tasks.sleep_for_tests, 30, stop=_stop(0.5))
    assert time.monotonic() - started < 5
    assert pool.run(tasks.sleep_for_tests, 0, stop=_stop()) == "fini"


def test_pool_cancel_kills_task(pool: ExtractionPool) -> None:
    token = CancelToken()
    threading.Timer(0.3, token.cancel).start()
    started = time.monotonic()
    with pytest.raises(Cancelled):
        pool.run(tasks.sleep_for_tests, 30, stop=_stop(cancel=token))
    assert time.monotonic() - started < 5
    assert pool.run(tasks.sleep_for_tests, 0, stop=_stop()) == "fini"


def test_page_batches() -> None:
    assert page_batches([1, 2, 3, 5, 6, 9], 2) == [(1, 2), (3, 3), (5, 6), (9, 9)]
    assert page_batches(list(range(1, 46)), 20) == [(1, 20), (21, 40), (41, 45)]
    assert page_batches([], 20) == []


# --- cache ---------------------------------------------------------------------------


def _fp(path: Path) -> keys.Fingerprint:
    return keys.Fingerprint.of(path.stat())


def test_doc_key_normalization(monkeypatch: pytest.MonkeyPatch) -> None:
    nfc = Path(unicodedata.normalize("NFC", "/a/Été.pdf"))
    nfd = Path(unicodedata.normalize("NFD", "/a/Été.pdf"))
    assert keys.doc_key(nfc) == keys.doc_key(nfd)
    monkeypatch.setattr(system, "CASE_INSENSITIVE_FS", True)
    assert keys.doc_key(Path("/a/ÉTÉ.PDF")) == keys.doc_key(nfc)
    monkeypatch.setattr(system, "CASE_INSENSITIVE_FS", False)
    assert keys.doc_key(Path("/a/ÉTÉ.PDF")) != keys.doc_key(nfc)


def test_cache_pages_roundtrip_and_invalidation(cache: ExtractionCache, tmp_path: Path) -> None:
    source = tmp_path / "doc.pdf"
    source.write_bytes(b"v1")
    doc = cache.open_document(source, _fp(source))
    cache.set_page_count(doc, 2)
    cache.write_pages(doc, [PageText(1, "page un é", ExtractionMethod.NATIVE)])
    cache.write_pages(doc, [PageText(2, "", ExtractionMethod.PENDING)])
    again = cache.open_document(source, _fp(source))
    assert not again.invalidated
    assert again.page_count == 2
    assert again.has_page(1) and again.has_page(2) and not again.has_page(2, need_ocr=True)
    assert cache.read_page(again.key, 1) == "page un é"
    cache.write_pages(again, [PageText(2, "texte ocr", ExtractionMethod.OCR)])
    assert cache.open_document(source, _fp(source)).has_page(2, need_ocr=True)
    assert cache.size_bytes() == len("page un é".encode()) + len(b"texte ocr")

    source.write_bytes(b"version 2")  # taille différente : entrée invalidée
    changed = cache.open_document(source, _fp(source))
    assert changed.invalidated and not changed.pages and changed.page_count is None
    assert cache.read_page(changed.key, 1) is None
    assert cache.size_bytes() == 0


def test_cache_detects_mtime_change(cache: ExtractionCache, tmp_path: Path) -> None:
    source = tmp_path / "doc.docx"
    source.write_bytes(b"abc")
    doc = cache.open_document(source, _fp(source))
    cache.write_pages(doc, [PageText(1, "x", ExtractionMethod.NATIVE)])
    os.utime(source, ns=(source.stat().st_atime_ns, source.stat().st_mtime_ns + 5_000_000_000))
    assert cache.open_document(source, _fp(source)).invalidated


def test_cache_lru_eviction_and_protection(tmp_path: Path) -> None:
    cache = ExtractionCache(tmp_path / "cache", max_bytes=250)
    try:
        docs: list[tuple[Path, CachedDocument]] = []
        for index in range(3):
            source = tmp_path / f"f{index}.pdf"
            source.write_bytes(b"x")
            docs.append((source, cache.open_document(source, _fp(source))))
            time.sleep(0.01)
        first_source, first = docs[0]
        cache.acquire(first.key)  # en cours d'utilisation : protégé
        for _, doc in docs:
            cache.write_pages(doc, [PageText(1, "a" * 100, ExtractionMethod.NATIVE)])
        assert cache.size_bytes() <= 250
        assert cache.read_page(first.key, 1) is not None
        cache.release(first.key)
        remaining = [d for _, d in docs if cache.read_page(d.key, 1) is not None]
        assert len(remaining) == 2
        assert cache.open_document(first_source, _fp(first_source)).has_page(1)
    finally:
        cache.close()


def test_cache_purge(cache: ExtractionCache, tmp_path: Path) -> None:
    source = tmp_path / "doc.pdf"
    source.write_bytes(b"x")
    doc = cache.open_document(source, _fp(source))
    cache.write_pages(doc, [PageText(1, "contenu", ExtractionMethod.NATIVE)])
    cache.purge()
    assert cache.size_bytes() == 0
    assert not cache.open_document(source, _fp(source)).pages


# --- accès aux documents -------------------------------------------------------------


def test_load_pdf_then_from_cache(documents: DocumentAccess, corpus: Corpus) -> None:
    path = corpus.root / "contrats/bail_2022.pdf"
    first = documents.load(path, want_ocr=False, stop=_stop())
    assert first.extracted and first.complete and first.page_count == 3
    assert "résiliation" in first.pages[2].text
    second = documents.load(path, want_ocr=False, stop=_stop())
    assert second.from_cache and second.pages[2].text == first.pages[2].text
    partial = documents.load(path, want_ocr=False, stop=_stop(), first_page=2, max_pages=1)
    assert partial.requested == [2] and list(partial.pages) == [2]


def test_load_pdf_inpage_batches_with_progress(documents: DocumentAccess, corpus: Corpus) -> None:
    progress = Progress()
    result = documents.load(
        corpus.root / "gros/long.pdf", want_ocr=False, stop=_stop(), progress=progress
    )
    assert result.page_count == LONG_PDF_PAGES and result.complete
    counts = [p["done"] for key, p in progress.events if key == "progress.pdf_pages"]
    assert len(counts) == 3 and counts[-1] == LONG_PDF_PAGES


def test_stop_keeps_extracted_pages(documents: DocumentAccess, corpus: Corpus) -> None:
    path = corpus.root / "gros/long.pdf"
    documents.batch_pages = 5
    token = CancelToken()
    progress = Progress()

    def cancel_after_first_batch(key: str, params: dict[str, Any]) -> None:
        if key == "progress.pdf_pages":
            token.cancel()

    progress.on_event = cancel_after_first_batch
    with pytest.raises(Cancelled):
        documents.load(path, want_ocr=False, stop=_stop(cancel=token), progress=progress)
    doc = documents.cache.open_document(path, _fp(path))
    kept = len(doc.pages)
    assert 5 <= kept < LONG_PDF_PAGES
    resumed = Progress()
    result = documents.load(path, want_ocr=False, stop=_stop(), progress=resumed)
    assert result.complete
    done = [p["done"] for key, p in resumed.events if key == "progress.pdf_pages"]
    assert done[-1] == LONG_PDF_PAGES - kept  # seules les pages manquantes sont extraites


def test_deadline_returns_partial_result(documents: DocumentAccess, corpus: Corpus) -> None:
    documents.batch_pages = 1
    result = documents.load(corpus.root / "gros/long.pdf", want_ocr=False, stop=_stop(0.0))
    assert result.stop_reason == "deadline"
    assert not result.complete


def test_quick_mode_marks_scanned_pages(documents: DocumentAccess, corpus: Corpus) -> None:
    result = documents.load(corpus.root / "contrats/mixte.pdf", want_ocr=False, stop=_stop())
    assert result.pending_ocr == [2]
    assert result.pages[1].method is ExtractionMethod.NATIVE


@requires_tesseract
def test_explore_mode_completes_scans_without_reextracting(
    documents: DocumentAccess, corpus: Corpus
) -> None:
    path = corpus.root / "contrats/mixte.pdf"
    documents.load(path, want_ocr=False, stop=_stop())
    progress = Progress()
    result = documents.load(path, want_ocr=True, stop=_stop(), progress=progress)
    assert result.pending_ocr == []
    assert "LOYER MENSUEL" in result.pages[2].text
    keys_seen = {key for key, _ in progress.events}
    assert "progress.ocr_pages" in keys_seen and "progress.pdf_pages" not in keys_seen
    # Le mode Rapide profite ensuite des pages déjà passées en OCR.
    quick = documents.load(path, want_ocr=False, stop=_stop())
    assert quick.from_cache and quick.pages[2].method is ExtractionMethod.OCR


def test_ocr_unavailable_is_reported(documents: DocumentAccess, corpus: Corpus) -> None:
    documents.ocr = None
    result = documents.load(
        corpus.root / "contrats/scan_quittance.pdf", want_ocr=True, stop=_stop()
    )
    assert result.ocr_unavailable and result.pending_ocr == [1, 2]
    image = documents.load(corpus.root / "images/facture.png", want_ocr=True, stop=_stop())
    assert image.ocr_unavailable and image.pending_ocr == [1]
    documents.ocr = OCR_CONFIG


def test_single_documents_and_notes(documents: DocumentAccess, corpus: Corpus) -> None:
    result = documents.load(corpus.root / "bureau/budget.xlsx", want_ocr=False, stop=_stop())
    assert result.page_count == 1 and "Chauffage" in result.pages[1].text
    assert result.notes and result.notes[0].detail == "1"
    cached = documents.load(corpus.root / "bureau/budget.xlsx", want_ocr=False, stop=_stop())
    assert cached.from_cache and cached.notes == result.notes


def test_image_in_quick_mode_is_not_extracted(documents: DocumentAccess, corpus: Corpus) -> None:
    path = corpus.root / "images/facture.png"
    assert not documents.needs_extraction(path, want_ocr=False)
    result = documents.load(path, want_ocr=False, stop=_stop())
    assert result.pending_ocr == [1] and not result.extracted


def test_errors_are_raised(documents: DocumentAccess, corpus: Corpus) -> None:
    for name, code in (
        ("contrats/protege.pdf", ErrorCode.PASSWORD),
        ("contrats/corrompu.pdf", ErrorCode.CORRUPT),
        ("bureau/protege.docx", ErrorCode.PASSWORD),
        ("contrats/nexiste_pas.pdf", ErrorCode.NOT_FOUND),
    ):
        with pytest.raises(ExtractionError) as info:
            documents.load(corpus.root / name, want_ocr=False, stop=_stop())
        assert info.value.code is code, name


def test_needs_extraction(documents: DocumentAccess, corpus: Corpus) -> None:
    path = corpus.root / "contrats/mixte.pdf"
    assert documents.needs_extraction(path, want_ocr=False)
    documents.load(path, want_ocr=False, stop=_stop())
    assert not documents.needs_extraction(path, want_ocr=False)
    assert documents.needs_extraction(path, want_ocr=True) is (OCR_CONFIG is not None)
    assert not documents.needs_extraction(corpus.root / "notes.txt", want_ocr=True)


def test_permanent_failures_are_remembered(documents: DocumentAccess, tmp_path: Path) -> None:
    broken = tmp_path / "abime.pdf"
    broken.write_bytes(b"pas un pdf")
    assert documents.needs_extraction(broken, want_ocr=False)
    with pytest.raises(ExtractionError):
        documents.load(broken, want_ocr=False, stop=_stop())
    assert not documents.needs_extraction(broken, want_ocr=False)  # ne coûte plus rien
    with pytest.raises(ExtractionError) as info:
        documents.load(broken, want_ocr=False, stop=_stop())
    assert info.value.code is ErrorCode.CORRUPT
    # Un fichier réparé est ré-analysé.
    from tests.corpus.make_corpus import PdfPage, write_pdf

    write_pdf(broken, [PdfPage(["Texte réparé et désormais lisible par l'extraction native."])])
    assert documents.load(broken, want_ocr=False, stop=_stop()).complete


def test_transient_failures_are_not_remembered(cache: ExtractionCache, tmp_path: Path) -> None:
    source = tmp_path / "doc.pdf"
    source.write_bytes(b"x")
    doc = cache.open_document(source, _fp(source))
    cache.remember_failure(doc, ExtractionError(ErrorCode.PERMISSION))
    assert cache.open_document(source, _fp(source)).failure is None
    cache.remember_failure(doc, ExtractionError(ErrorCode.PASSWORD))
    failure = cache.open_document(source, _fp(source)).failure
    assert failure is not None and failure.code is ErrorCode.PASSWORD
