"""La boucle agentique de bout en bout, avec le faux fournisseur et les vrais outils."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from archipelle.agent.history import History
from archipelle.agent.loop import LoopLimits, TurnRunner
from archipelle.core.cancel import CancelToken
from archipelle.core.events import Event
from archipelle.providers.catalog import ModelInfo
from archipelle.providers.fake import FakeProvider
from archipelle.providers.pivot import AssistantMessage, ToolResultGroup
from tests.agent.conftest import Recorder

ANSWER = "L'échéance est fixée au 30 juin [[notes.txt]] et rien dans [[inexistant.pdf]]."


def script(*steps: dict[str, Any]) -> FakeProvider:
    return FakeProvider.from_script({"steps": list(steps)})


def call(name: str, **arguments: Any) -> dict[str, Any]:
    return {"id": f"c_{name}_{len(arguments)}", "name": name, "arguments": arguments}


def test_complete_turn_from_tools_to_verified_sources(
    make_runner: Any, history: History, events: Recorder
) -> None:
    provider = script(
        {"text": "Je liste.", "tool_calls": [call("list_files", depth=2)]},
        {"text": "Je lis.", "tool_calls": [call("read_file", path="notes.txt")]},
        {"text": ANSWER},
    )
    runner: TurnRunner = make_runner(provider)
    result = runner.run()

    assert result.status == "complete" and result.iterations == 3
    assert result.answer == ANSWER
    assert [(s.rel_path, s.verified) for s in result.sources] == [
        ("notes.txt", True),
        ("inexistant.pdf", False),
    ]
    assert result.consulted == ["notes.txt"]

    kinds = [item.kind for item in history.items(runner.request.conversation_id)]
    assert kinds == [
        "tree", "user", "assistant", "tool_results", "assistant", "tool_results", "assistant"
    ]  # fmt: skip
    assert history.turns(runner.request.conversation_id)[0].status == "complete"
    # Le contenu des documents n'est jamais archivé.
    archive = str([item.payload for item in history.items(runner.request.conversation_id)])
    assert "échéance du bail est fixée" not in archive
    assert events.types()[0] == "turn_started"
    assert events.types()[-2:] == ["final_answer", "turn_ended"]
    assert len(events.of("iteration")) == 3
    assert events.of("final_answer")[0]["sources"][0] == {
        "path": "notes.txt", "verified": True, "workdir": runner.request.workdir
    }  # fmt: skip
    assert [step["kind"] for step in history.steps(result.turn_id)].count("tool_call") == 2
    # Le modèle a bien reçu l'arborescence, le prompt et les outils.
    first = provider.requests[0]
    assert len(first.tools) == 5 and first.tool_choice == "auto"
    assert "Archipelle" in first.system
    assert type(first.items[0]).__name__ == "TreeMessage"


def test_tool_errors_are_returned_to_the_model(make_runner: Any, history: History) -> None:
    provider = script(
        {"text": "J'essaie.", "tool_calls": [
            call("read_file", path="../secret.txt"),
            call("read_file", path="contrats/protege.pdf"),
            call("read_file", path="archives/lot.zip"),
            {"id": "c_bad", "name": "outil_inexistant", "arguments": {}},
        ]},
        {"text": "Rien de lisible."},
    )  # fmt: skip
    runner: TurnRunner = make_runner(provider)
    result = runner.run()
    assert result.status == "complete"
    group = next(i for i in provider.requests[-1].items if isinstance(i, ToolResultGroup))
    contents = [r.content for r in group.results]
    assert all(r.is_error for r in group.results)
    assert "doivent rester dans le dossier de travail" in contents[0]
    assert "mot de passe" in contents[1]
    assert "n'est pas exploré" in contents[2]
    assert "Outil inconnu" in contents[3]
    assert history.ignored_files(result.turn_id)  # journal alimenté


def test_iteration_limit_forces_a_final_answer(make_runner: Any) -> None:
    provider = script(
        {"text": "1", "tool_calls": [call("list_files")]},
        {"text": "2", "tool_calls": [call("list_files")]},
        {"text": "Réponse forcée avec ce que j'ai."},
    )
    runner: TurnRunner = make_runner(provider, limits=LoopLimits(quick_iterations=3))
    result = runner.run()
    assert result.status == "complete" and result.answer.startswith("Réponse forcée")
    assert [r.tool_choice for r in provider.requests] == ["auto", "auto", "none"]


def test_partial_answer_when_the_model_keeps_asking_for_tools(
    make_runner: Any, history: History
) -> None:
    provider = script(
        {"text": "1", "tool_calls": [call("read_file", path="notes.txt")]},
        {"text": "encore", "tool_calls": [call("list_files")]},  # tool_choice « none » ignoré
        {"text": "toujours", "tool_calls": [call("list_files")]},  # rappel unique
    )
    runner: TurnRunner = make_runner(provider, limits=LoopLimits(quick_iterations=2))
    result = runner.run()
    assert result.status == "partial"
    assert "notes.txt" in result.answer  # la réponse du code liste les fichiers consultés
    assert "[[" not in result.answer  # aucune affirmation ni citation inventée
    assert len(provider.requests) == 3  # le rappel est hors compteur
    groups = [i for i in provider.requests[-1].items if isinstance(i, ToolResultGroup)]
    assert all(r.synthetic for r in groups[-1].results)
    assert "limite atteinte" in groups[-1].results[0].content
    assert history.turns(runner.request.conversation_id)[0].status == "partial"


def test_more_than_five_calls_per_iteration(make_runner: Any, history: History) -> None:
    calls = [call("list_files", depth=d) for d in range(1, 8)]
    provider = script({"text": "beaucoup", "tool_calls": calls}, {"text": "Fini."})
    runner: TurnRunner = make_runner(provider)
    runner.run()
    group = next(i for i in provider.requests[-1].items if isinstance(i, ToolResultGroup))
    assert len(group.results) == 7
    assert [r.synthetic for r in group.results] == [False] * 5 + [True, True]
    assert "5 appels" in group.results[5].content


def test_deadline_stops_the_exploration(make_runner: Any, history: History) -> None:
    provider = script(
        {"text": "1", "tool_calls": [call("read_file", path="notes.txt")]},
        {"text": "Réponse malgré la limite de temps."},
    )
    runner: TurnRunner = make_runner(provider, seconds=0.0, limits=LoopLimits(quick_seconds=0.0))
    result = runner.run()
    assert result.status == "complete"
    assert "durée maximale" in result.answer
    assert provider.requests[0].tool_choice == "none"  # dernière itération imposée d'emblée
    assert [s["kind"] for s in history.steps(result.turn_id)].count("deadline") >= 1


def test_stop_button_repairs_history(make_runner: Any, history: History, events: Recorder) -> None:
    token = CancelToken()
    provider = script(
        {"text": "Je lis.", "tool_calls": [call("read_file", path="notes.txt")]},
        {"text": "jamais rendu"},
    )

    def stop_on_first_file(event: Event) -> None:
        if event.type.value == "file_consulted":
            token.cancel()

    events.on_event = stop_on_first_file
    runner: TurnRunner = make_runner(provider, cancel=token)
    result = runner.run()
    assert result.status == "interrupted"
    assert result.answer == "Recherche interrompue."
    items = history.items(runner.request.conversation_id)
    assert [item.kind for item in items][-2:] == ["tool_results", "interruption"]
    group = items[-2].pivot()
    assert isinstance(group, ToolResultGroup) and len(group.results) == 1
    assert items[-1].payload["files"] == ["notes.txt"]
    assert history.turns(runner.request.conversation_id)[0].status == "interrupted"
    assert "turn_interrupted" in events.types()
    assert len(provider.requests) == 1  # le tour n'a pas rappelé le modèle


def test_stop_before_the_first_call(make_runner: Any) -> None:
    token = CancelToken()
    token.cancel()
    runner: TurnRunner = make_runner(script({"text": "jamais"}), cancel=token)
    from archipelle.core.outcome import Cancelled

    with pytest.raises(Cancelled):
        runner.run()


@pytest.mark.parametrize(
    ("error", "expected_key"),
    [("auth", "errors.provider.auth"), ("model_not_found", "errors.provider.model_not_found")],
)
def test_provider_errors_end_the_turn(
    make_runner: Any, history: History, events: Recorder, error: str, expected_key: str
) -> None:
    provider = script({"error": error})
    runner: TurnRunner = make_runner(provider)
    result = runner.run()
    assert result.status == "interrupted" and result.error_key == expected_key
    assert len(provider.requests) == 1  # aucune nouvelle tentative
    assert events.of("error")[0]["recoverable"] is False
    assert history.turns(runner.request.conversation_id)[0].status == "interrupted"


def test_transient_errors_are_retried(make_runner: Any, events: Recorder) -> None:
    provider = script({"error": "server"}, {"error": "rate_limited"}, {"text": "Enfin."})
    runner: TurnRunner = make_runner(provider)
    result = runner.run()
    assert result.status == "complete" and result.answer == "Enfin."
    assert len(provider.requests) == 3
    assert [e["recoverable"] for e in events.of("error")] == [True, True]


def test_empty_answer_gives_a_partial_result(make_runner: Any) -> None:
    result = make_runner(script({"text": "   "})).run()
    assert result.status == "partial" and "Aucun document" in result.answer


def test_scope_restriction_applies_to_tools(make_runner: Any, history: History) -> None:
    provider = script(
        {"text": "1", "tool_calls": [call("read_file", path="notes.txt")]},
        {"text": "Fini."},
    )
    runner: TurnRunner = make_runner(provider, scope_rel="contrats")
    runner.run()
    group = next(i for i in provider.requests[-1].items if isinstance(i, ToolResultGroup))
    assert "sous-dossier" in group.results[0].content
    assert "contrats" in runner.system_prompt()


def test_tree_is_injected_once_and_refreshed(
    make_runner: Any, history: History, corpus: Any
) -> None:
    provider = script({"text": "Un."}, {"text": "Deux."}, {"text": "Trois."})
    first: TurnRunner = make_runner(provider)
    first.run()
    conversation_id = first.request.conversation_id

    second: TurnRunner = make_runner(provider, conversation_id=conversation_id, question="Suite ?")
    second.run()
    trees = [i for i in history.items(conversation_id) if i.kind == "tree"]
    assert len(trees) == 1  # arborescence inchangée : pas de nouveau relevé

    (corpus.root / "nouveau_document.txt").write_text("ajout", encoding="utf-8")
    third: TurnRunner = make_runner(provider, conversation_id=conversation_id, question="Et là ?")
    third.run()
    kinds = [i.kind for i in history.items(conversation_id)]
    assert kinds.count("tree") == 2
    notices = [i for i in history.items(conversation_id) if i.kind == "system_notice"]
    assert "a changé" in notices[-1].payload["text"]
    # Une seule arborescence est envoyée au modèle malgré les deux relevés archivés.
    sent = provider.requests[-1].items
    assert sum(1 for item in sent if type(item).__name__ == "TreeMessage") == 1


def test_pending_notices_open_the_turn(make_runner: Any, history: History) -> None:
    from archipelle.agent import prompts

    runner: TurnRunner = make_runner(script({"text": "Fini."}))
    runner.pending_notices.append(prompts.scope_changed("contrats"))
    runner.run()
    kinds = [i.kind for i in history.items(runner.request.conversation_id)]
    assert kinds[0] == "system_notice"


def test_events_carry_the_run_id(make_runner: Any, events: Recorder) -> None:
    make_runner(script({"text": "Fini."})).run()
    assert {event.run_id for event in events.events} == {"run-test"}
    assert all(isinstance(event, Event) for event in events.events)


def test_reasoning_is_archived_and_replayed(make_runner: Any, history: History) -> None:
    provider = script(
        {"text": "Je lis.", "reasoning": "réflexion interne",
         "tool_calls": [call("read_file", path="notes.txt")]},
        {"text": "Fini."},
    )  # fmt: skip
    runner: TurnRunner = make_runner(provider)
    runner.run()
    assistant = next(i for i in history.pivot_items(runner.request.conversation_id)
                     if isinstance(i, AssistantMessage))  # fmt: skip
    assert assistant.reasoning[0].payload == {"text": "réflexion interne"}
    replayed = [i for i in provider.requests[-1].items if isinstance(i, AssistantMessage)]
    assert replayed[0].reasoning[0].payload == {"text": "réflexion interne"}


def test_long_results_are_replaced_by_markers_in_the_next_request(
    make_runner: Any, history: History
) -> None:
    provider = script(
        {"text": "1", "tool_calls": [call("read_file", path="gros/long.txt")]},
        {
            "text": "2",
            "tool_calls": [call("read_file", path="gros/long.txt", page=1, offset=20000)],
        },
        {"text": "Fini."},
    )
    petit = ModelInfo("petit", "Petit", "", 8000, 1000, tools=True)
    runner: TurnRunner = make_runner(provider, model_info=petit)
    result = runner.run()
    assert result.status == "complete"
    groups = [i for i in provider.requests[-1].items if isinstance(i, ToolResultGroup)]
    assert groups and groups[0].results[0].content.startswith("« gros/long.txt »")
    assert "relis-le avec read_file" in groups[0].results[0].content


def test_runner_is_usable_from_a_worker_thread(make_runner: Any) -> None:
    runner: TurnRunner = make_runner(script({"text": "Fini."}))
    outcome: list[Any] = []
    thread = threading.Thread(target=lambda: outcome.append(runner.run()))
    started = time.monotonic()
    thread.start()
    thread.join(60)
    assert not thread.is_alive() and outcome[0].status == "complete"
    assert time.monotonic() - started < 60


def test_current_turn_keeps_full_tool_content(make_runner: Any, history: History) -> None:
    """Le modèle voit le contenu réel des documents lus pendant le tour, pas les marqueurs
    (l'archive, elle, ne garde que les marqueurs — CDC §12)."""
    provider = script(
        {"text": "1", "tool_calls": [call("read_file", path="notes.txt")]},
        {"text": "Fini."},
    )
    runner: TurnRunner = make_runner(provider)
    runner.run()
    sent = next(i for i in provider.requests[-1].items if isinstance(i, ToolResultGroup))
    assert "échéance du bail est fixée au 30 juin" in sent.results[0].content
    # Et le message réellement transmis à l'API contient ce texte, pas son marqueur.
    from archipelle.providers.catalog import load_catalog
    from archipelle.providers.mistral import MistralProvider

    payload = MistralProvider(load_catalog().provider("mistral"), "cle").build_payload(
        provider.requests[-1]
    )
    assert "échéance du bail est fixée au 30 juin" in str(payload["messages"])
    archived = next(
        i
        for i in history.pivot_items(runner.request.conversation_id)
        if isinstance(i, ToolResultGroup)
    )
    assert "échéance du bail" not in archived.results[0].content
    assert archived.results[0].content.startswith("« notes.txt »")
