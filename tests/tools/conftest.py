from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from archipelle.cache.store import ExtractionCache
from archipelle.core.cancel import CancelToken
from archipelle.extraction import ocr
from archipelle.extraction.types import OcrConfig
from archipelle.persistence.settings_store import ScanExclusions
from archipelle.tools.context import Mode, ToolContext, ToolLimits
from archipelle.tools.documents import DocumentAccess
from archipelle.tools.paths import Workspace
from archipelle.tools.process_pool import ExtractionPool
from tests.corpus.make_corpus import Corpus, build_corpus

OCR_CONFIG = ocr.detect()

requires_tesseract = pytest.mark.skipif(OCR_CONFIG is None, reason="Tesseract absent")


@pytest.fixture(scope="session")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Corpus:
    return build_corpus(tmp_path_factory.mktemp("corpus") / "dossier")


@pytest.fixture(scope="session")
def pool() -> Iterator[ExtractionPool]:
    extraction_pool = ExtractionPool(max_workers=2)
    yield extraction_pool
    extraction_pool.shutdown()


@pytest.fixture
def cache(tmp_path: Path) -> Iterator[ExtractionCache]:
    extraction_cache = ExtractionCache(tmp_path / "cache")
    yield extraction_cache
    extraction_cache.close()


@pytest.fixture
def documents(cache: ExtractionCache, pool: ExtractionPool) -> Iterator[DocumentAccess]:
    access = DocumentAccess(cache, pool, OCR_CONFIG)
    yield access
    access.shutdown()
    cache.close()


class Progress:
    """Collecte les événements de progression ; peut déclencher un arrêt."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.on_event: Callable[[str, dict[str, Any]], None] | None = None

    def __call__(self, key: str, params: dict[str, Any]) -> None:
        self.events.append((key, params))
        if self.on_event is not None:
            self.on_event(key, params)


type MakeContext = Callable[..., ToolContext]


@pytest.fixture
def make_ctx(corpus: Corpus, documents: DocumentAccess, pool: ExtractionPool) -> MakeContext:
    def factory(
        mode: Mode = "quick",
        *,
        scope: str | None = None,
        limits: ToolLimits | None = None,
        ocr_config: OcrConfig | str | None = "auto",
        cancel: CancelToken | None = None,
        seconds: float = 300,
        progress: Progress | None = None,
    ) -> ToolContext:
        config = OCR_CONFIG if ocr_config == "auto" else ocr_config
        assert not isinstance(config, str)
        documents.ocr = config
        return ToolContext(
            workspace=Workspace.open(corpus.root, scope),
            mode=mode,
            ocr=config,
            cancel=cancel or CancelToken(),
            turn_deadline=time.monotonic() + seconds,
            pool=pool,
            documents=documents,
            exclusions=ScanExclusions.defaults(),
            limits=limits or ToolLimits(),
            progress=progress or Progress(),
        )

    return factory
