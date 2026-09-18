from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from archipelle.agent.history import History
from archipelle.agent.loop import LoopLimits
from archipelle.agent.service import AgentService
from archipelle.cache.store import ExtractionCache
from archipelle.core.events import Event
from archipelle.persistence.db import Database
from archipelle.persistence.settings_store import SecretsStore, SettingsStore
from archipelle.providers.base import Provider
from archipelle.tools.documents import DocumentAccess
from archipelle.tools.process_pool import ExtractionPool
from tests.corpus.make_corpus import Corpus, build_corpus


@pytest.fixture(scope="session")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Corpus:
    return build_corpus(tmp_path_factory.mktemp("corpus-ui") / "dossier")


@pytest.fixture(scope="session")
def pool() -> Iterator[ExtractionPool]:
    extraction_pool = ExtractionPool(max_workers=2)
    yield extraction_pool
    extraction_pool.shutdown()


@pytest.fixture
def events() -> list[Event]:
    return []


@pytest.fixture
def service(tmp_path: Path, corpus: Corpus, pool: ExtractionPool, events: list[Event]) -> Any:
    db = Database(tmp_path / "history.db", "history")
    db.migrate()
    cache = ExtractionCache(tmp_path / "cache")
    documents = DocumentAccess(cache, pool, None)
    settings = SettingsStore(tmp_path / "config.json")
    settings.update(lambda s: setattr(s, "default_workdir", str(corpus.root)))
    secrets = SecretsStore(tmp_path / "secrets.json")
    secrets.set("mistral", "cle-de-test")
    built: list[AgentService] = []

    def factory(provider: Provider) -> AgentService:
        agent = AgentService(
            history=History(db),
            settings_store=settings,
            secrets=secrets,
            cache=cache,
            pool=pool,
            documents=documents,
            publish=events.append,
            limits=LoopLimits(quick_iterations=2),
            build_provider=lambda config: provider,
        )
        built.append(agent)
        return agent

    yield factory
    for agent in built:
        agent.stop()
        agent.wait(10)
    documents.shutdown()
    cache.close()
    db.close_all()
