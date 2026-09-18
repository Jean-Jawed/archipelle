from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from archipelle.agent.history import History, Mode
from archipelle.agent.loop import LoopLimits, TurnRequest, TurnRunner
from archipelle.cache.store import ExtractionCache
from archipelle.core.cancel import CancelToken
from archipelle.core.events import Event
from archipelle.persistence.db import Database
from archipelle.persistence.settings_store import ScanExclusions
from archipelle.providers.base import Provider
from archipelle.providers.catalog import ModelInfo, load_catalog
from archipelle.providers.retry import RetryPolicy
from archipelle.tools.context import ToolContext, ToolLimits
from archipelle.tools.documents import DocumentAccess
from archipelle.tools.paths import Workspace
from archipelle.tools.process_pool import ExtractionPool
from tests.corpus.make_corpus import Corpus, build_corpus

FAST_RETRY = RetryPolicy(network_delays_s=(0.01,), rate_limit_delays_s=(0.01,))


@pytest.fixture(scope="session")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Corpus:
    return build_corpus(tmp_path_factory.mktemp("corpus-agent") / "dossier")


@pytest.fixture(scope="session")
def pool() -> Iterator[ExtractionPool]:
    extraction_pool = ExtractionPool(max_workers=2)
    yield extraction_pool
    extraction_pool.shutdown()


@pytest.fixture
def history(tmp_path: Path) -> Iterator[History]:
    db = Database(tmp_path / "history.db", "history")
    db.migrate()
    yield History(db)
    db.close_all()


@pytest.fixture
def documents(tmp_path: Path, pool: ExtractionPool) -> Iterator[DocumentAccess]:
    cache = ExtractionCache(tmp_path / "cache")
    access = DocumentAccess(cache, pool, None)
    yield access
    access.shutdown()
    cache.close()


class Recorder:
    """Collecte les événements publiés et peut déclencher une action."""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self.on_event: Any = None

    def __call__(self, event: Event) -> None:
        self.events.append(event)
        if self.on_event is not None:
            self.on_event(event)

    def types(self) -> list[str]:
        return [event.type.value for event in self.events]

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [event.payload for event in self.events if event.type.value == kind]


@pytest.fixture
def events() -> Recorder:
    return Recorder()


@pytest.fixture
def make_runner(corpus: Corpus, history: History, documents: DocumentAccess, pool: ExtractionPool,
                events: Recorder):  # fmt: skip
    def factory(
        provider: Provider,
        *,
        question: str = "Quelle est l'échéance du bail ?",
        mode: Mode = "quick",
        model_id: str = "mistral-small-latest",
        provider_id: str = "fake",
        limits: LoopLimits | None = None,
        cancel: CancelToken | None = None,
        scope_rel: str | None = None,
        conversation_id: str | None = None,
        ocr_available: bool = True,
        seconds: float | None = None,
        model_info: ModelInfo | None = None,
    ) -> TurnRunner:
        catalog = load_catalog()
        model = model_info or catalog.model("mistral", model_id)
        if conversation_id is None:
            conversation = history.create_conversation(
                provider_id,
                model.id,
                mode=mode,
                workdir=str(corpus.root),
            )
            conversation_id = conversation.id
        token = cancel or CancelToken()
        rules = limits or LoopLimits()
        ctx = ToolContext(
            workspace=Workspace.open(corpus.root, scope_rel),
            mode=mode,
            ocr=None,
            cancel=token,
            turn_deadline=time.monotonic() + (seconds or rules.seconds(mode)),
            pool=pool,
            documents=documents,
            exclusions=ScanExclusions.defaults(),
            limits=ToolLimits(),
            progress=lambda key, params: None,
        )
        request = TurnRequest(
            conversation_id=conversation_id,
            run_id="run-test",
            mode=mode,
            question=question,
            workdir=str(corpus.root),
            scope_rel=scope_rel,
            provider=provider,
            provider_id=provider_id,
            model=model,
        )
        return TurnRunner(
            request,
            history=history,
            tool_context=ctx,
            cancel=token,
            publish=events,
            limits=rules,
            retry_policy=FAST_RETRY,
            ocr_available=ocr_available,
        )

    return factory
