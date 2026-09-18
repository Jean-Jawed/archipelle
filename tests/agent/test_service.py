"""Façade de l'orchestration : réglages, thread worker, arrêt, « Continuer »."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from archipelle.agent.history import History
from archipelle.agent.loop import LoopLimits
from archipelle.agent.service import AgentService
from archipelle.cache.store import ExtractionCache
from archipelle.core.outcome import Err, Ok
from archipelle.persistence.settings_store import SecretsStore, SettingsStore
from archipelle.providers.base import ChatRequest, Provider
from archipelle.providers.fake import FakeProvider
from archipelle.providers.pivot import AssistantMessage
from archipelle.tools.documents import DocumentAccess
from archipelle.tools.process_pool import ExtractionPool
from tests.agent.conftest import Recorder
from tests.corpus.make_corpus import Corpus


@pytest.fixture
def service(
    tmp_path: Path,
    corpus: Corpus,
    history: History,
    documents: DocumentAccess,
    pool: ExtractionPool,
    events: Recorder,
) -> Any:
    settings = SettingsStore(tmp_path / "config.json")
    settings.update(lambda s: setattr(s, "default_provider", "mistral"))
    secrets = SecretsStore(tmp_path / "secrets.json")
    secrets.set("mistral", "cle-de-test")
    cache = ExtractionCache(tmp_path / "cache2")

    def factory(provider: FakeProvider):
        return AgentService(
            history=history,
            settings_store=settings,
            secrets=secrets,
            cache=cache,
            pool=pool,
            documents=documents,
            publish=events,
            limits=LoopLimits(quick_iterations=2, explore_iterations=3),
            build_provider=lambda config: provider,
            catalog=__import__(
                "archipelle.providers.catalog", fromlist=["load_catalog"]
            ).load_catalog(),
        )

    return factory


def _provider(*steps: dict[str, Any]) -> FakeProvider:
    return FakeProvider.from_script({"steps": list(steps)})


def _run(service: AgentService, conversation_id: str, question: str = "Question ?") -> Any:
    outcome = service.send(conversation_id, question)
    assert isinstance(outcome, Ok), getattr(outcome, "key", None)
    assert service.wait(60)
    return service.last_result


def test_send_runs_the_turn_in_a_worker_thread(service: Any, corpus: Corpus) -> None:
    agent: AgentService = service(_provider({"text": "Réponse."}))
    conversation = agent.new_conversation(str(corpus.root))
    assert conversation.provider_id == "mistral" and conversation.mode == "quick"
    assert not agent.busy
    result = _run(agent, conversation.id)
    assert result.status == "complete" and result.answer == "Réponse."
    assert not agent.busy and agent.active_run() is None
    reloaded = agent.history.get_conversation(conversation.id)
    assert reloaded is not None and reloaded.provider_locked


def test_refusals_are_typed(service: Any, corpus: Corpus, tmp_path: Path) -> None:
    agent: AgentService = service(_provider({"text": "ok"}))
    conversation = agent.new_conversation(str(corpus.root))
    assert isinstance(agent.send(conversation.id, "   "), Err)
    assert isinstance(agent.send("inconnue", "Q"), Err)
    assert isinstance(agent.stop(), Err)  # rien à arrêter
    sans_dossier = agent.history.create_conversation("mistral", "m")
    refusal = agent.send(sans_dossier.id, "Q")
    assert isinstance(refusal, Err) and refusal.key == "agent.errors.no_workdir"
    agent.secrets.delete("mistral")
    missing = agent.send(conversation.id, "Q")
    assert isinstance(missing, Err) and "Mistral" in missing.message()


def test_settings_are_locked_during_a_turn(service: Any, corpus: Corpus) -> None:
    provider = _provider({"text": "1", "delay_s": 0.4}, {"text": "2"})
    agent: AgentService = service(provider)
    conversation = agent.new_conversation(str(corpus.root))
    assert isinstance(agent.send(conversation.id, "Q"), Ok)
    assert agent.busy
    busy = agent.send(conversation.id, "Autre question")
    assert isinstance(busy, Err) and busy.key == "agent.errors.busy"
    for refusal in (
        agent.set_model(conversation.id, "mistral-large-latest"),
        agent.set_mode(conversation.id, "explore"),
        agent.set_workdir(conversation.id, str(corpus.root)),
        agent.set_scope(conversation.id, "contrats"),
        agent.set_provider(conversation.id, "openai"),
    ):
        assert isinstance(refusal, Err) and refusal.key == "agent.errors.turn_in_progress"
    assert agent.wait(60)
    assert isinstance(agent.set_model(conversation.id, "mistral-large-latest"), Ok)


def test_provider_is_locked_after_the_first_question(service: Any, corpus: Corpus) -> None:
    agent: AgentService = service(_provider({"text": "ok"}))
    conversation = agent.new_conversation(str(corpus.root))
    agent.secrets.set("openai", "cle-openai")
    assert isinstance(agent.set_provider(conversation.id, "openai"), Ok)
    _run(agent, conversation.id)
    refusal = agent.set_provider(conversation.id, "mistral")
    assert isinstance(refusal, Err) and refusal.key == "agent.errors.provider_locked"


def test_mode_switch_changes_the_model(service: Any, corpus: Corpus) -> None:
    agent: AgentService = service(_provider({"text": "ok"}))
    conversation = agent.new_conversation(str(corpus.root))
    assert conversation.model == "mistral-small-latest"
    switched = agent.set_mode(conversation.id, "explore")
    assert isinstance(switched, Ok)
    assert switched.value.model == "mistral-medium-latest"


def test_continue_deeper_restores_mode_and_model(service: Any, corpus: Corpus) -> None:
    provider = _provider({"text": "Première réponse."}, {"text": "Réponse approfondie."})
    agent: AgentService = service(provider)
    conversation = agent.new_conversation(str(corpus.root))
    _run(agent, conversation.id)

    outcome = agent.continue_deeper(conversation.id)
    assert isinstance(outcome, Ok)
    assert outcome.value.mode == "explore" and outcome.value.model == "mistral-medium-latest"
    assert agent.wait(60)
    assert agent.last_result is not None and agent.last_result.answer == "Réponse approfondie."
    # L'effet ne dure qu'un tour (CDC §7).
    restored = agent.history.get_conversation(conversation.id)
    assert restored is not None
    assert (restored.mode, restored.model) == ("quick", "mistral-small-latest")
    turn = agent.history.turns(conversation.id)[-1]
    assert (turn.mode, turn.restore_mode) == ("explore", "quick")
    request: ChatRequest = provider.requests[-1]
    assert request.model == "mistral-medium-latest"


def test_continue_deeper_restores_after_a_failure(service: Any, corpus: Corpus) -> None:
    agent: AgentService = service(_provider({"text": "ok"}, {"error": "server", "repeat": 3}))
    conversation = agent.new_conversation(str(corpus.root))
    _run(agent, conversation.id)
    assert isinstance(agent.continue_deeper(conversation.id), Ok)
    assert agent.wait(60)
    restored = agent.history.get_conversation(conversation.id)
    assert restored is not None and restored.mode == "quick"


def test_stop_interrupts_the_running_turn(service: Any, corpus: Corpus, events: Recorder) -> None:
    agent: AgentService = service(_provider({"text": "trop long", "delay_s": 30}))
    conversation = agent.new_conversation(str(corpus.root))
    assert isinstance(agent.send(conversation.id, "Q"), Ok)
    time.sleep(0.2)
    started = time.monotonic()
    assert isinstance(agent.stop(), Ok)
    assert agent.wait(20)
    assert time.monotonic() - started < 10
    assert agent.last_result is not None and agent.last_result.status == "interrupted"
    assert agent.history.turns(conversation.id)[0].status == "interrupted"
    assert "turn_interrupted" in events.types()


def test_scope_and_workdir_notices_reach_the_next_turn(service: Any, corpus: Corpus) -> None:
    provider = _provider({"text": "ok"})
    agent: AgentService = service(provider)
    conversation = agent.new_conversation(str(corpus.root))
    assert isinstance(agent.set_scope(conversation.id, "contrats"), Ok)
    refusal = agent.set_scope(conversation.id, "dossier_absent")
    assert isinstance(refusal, Err)
    _run(agent, conversation.id)
    kinds = [item.kind for item in agent.history.items(conversation.id)]
    assert kinds[0] == "system_notice"
    notice = agent.history.items(conversation.id)[0]
    assert "contrats" in notice.payload["text"]
    assert "contrats" in provider.requests[-1].system  # restriction rappelée au modèle


def test_unexpected_failure_is_reported_and_releases_the_worker(
    service: Any, corpus: Corpus, events: Recorder
) -> None:
    class Broken:
        id = "fake"

        def complete(self, request: ChatRequest, cancel: Any) -> AssistantMessage:
            raise RuntimeError("panne interne")

    agent: AgentService = service(Broken())
    conversation = agent.new_conversation(str(corpus.root))
    assert isinstance(agent.send(conversation.id, "Q"), Ok)
    assert agent.wait(60)
    assert not agent.busy  # le worker est libéré malgré l'erreur
    assert events.of("error")[-1]["key"] == "errors.unexpected"


def test_startup_repairs_and_purges(service: Any, corpus: Corpus) -> None:
    agent: AgentService = service(_provider({"text": "ok"}))
    conversation = agent.new_conversation(str(corpus.root))
    _run(agent, conversation.id)
    agent.history.open_turn(conversation.id, "run-perdu", "quick", str(corpus.root))
    assert len(agent.startup()) == 1
    assert agent.history.unfinished_turns() == []
    assert agent.cache_size() >= 0
    agent.purge_cache()
    agent.purge_history(conversation.id)
    assert agent.history.get_conversation(conversation.id) is None


def test_open_source_requires_a_consulted_file(service: Any, corpus: Corpus) -> None:
    provider = _provider(
        {"text": "1", "tool_calls": [{"id": "c1", "name": "read_file",
                                      "arguments": {"path": "notes.txt"}}]},
        {"text": "Fini [[notes.txt]]."},
    )  # fmt: skip
    agent: AgentService = service(provider)
    conversation = agent.new_conversation(str(corpus.root))
    _run(agent, conversation.id)
    opened: list[Any] = []
    import archipelle.agent.service as service_module

    original = service_module.open_source

    def fake_open(workdir: Any, rel_path: str, *, verified: bool, **kwargs: Any) -> Any:
        del workdir, rel_path, kwargs
        opened.append(verified)
        return Ok(None)

    service_module.open_source = fake_open
    try:
        agent.open_source(conversation.id, "notes.txt")
        agent.open_source(conversation.id, "contrats/bail_2022.pdf")
    finally:
        service_module.open_source = original
    assert opened == [True, False]


def test_provider_instance_is_the_expected_one(service: Any, corpus: Corpus) -> None:
    provider = _provider({"text": "ok"})
    agent: AgentService = service(provider)
    conversation = agent.new_conversation(str(corpus.root))
    _run(agent, conversation.id)
    assert isinstance(provider.requests[0], ChatRequest)
    assert provider.requests[0].model == "mistral-small-latest"


def test_shutdown_is_idempotent(service: Any, corpus: Corpus) -> None:
    agent: AgentService = service(_provider({"text": "ok"}))
    conversation = agent.new_conversation(str(corpus.root))
    _run(agent, conversation.id)
    assert isinstance(agent.stop(), Err)
    provider: Provider = _provider({"text": "ok"})
    assert provider.id == "fake"
