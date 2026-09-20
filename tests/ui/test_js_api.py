"""Pont exposé au JavaScript et passeur d'événements."""

from __future__ import annotations

import queue
from pathlib import Path
from typing import Any

import pytest

from archipelle.agent.service import AgentService
from archipelle.core.events import Event, EventType
from archipelle.providers.fake import FakeProvider
from archipelle.ui.bridge import EventBridge, event_json
from archipelle.ui.js_api import FolderChooser, JsApi, ui_texts
from tests.corpus.make_corpus import Corpus


class StubChooser(FolderChooser):
    def __init__(self, answers: list[str | None]) -> None:
        super().__init__()
        self.answers = answers
        self.calls: list[str | None] = []

    def choose(self, start: str | None = None) -> str | None:
        self.calls.append(start)
        return self.answers.pop(0) if self.answers else None


@pytest.fixture
def api(service: Any, corpus: Corpus) -> JsApi:
    agent: AgentService = service(
        FakeProvider.from_script({"steps": [{"text": "Réponse [[notes.txt]]."}]})
    )
    return JsApi(agent, chooser=StubChooser([]))


def test_bootstrap_exposes_everything_the_ui_needs(api: JsApi) -> None:
    data = api.bootstrap()
    assert data["ok"]
    assert data["settings"]["default_provider"] == "mistral"
    assert {p["id"] for p in data["providers"]} >= {"mistral", "openai", "custom"}
    assert data["texts"]["ui.composer.send"] == "Envoyer"
    assert data["platform"] in ("linux", "macos", "windows")


def test_keys_are_never_returned_in_clear(api: JsApi) -> None:
    saved = api.save_key("openai", "sk-secret-123456789")
    assert saved["ok"] and "secret" not in saved["masked_key"]
    payload = str(api.bootstrap())
    assert "sk-secret-123456789" not in payload
    assert "cle-de-test" not in payload  # la clé Mistral de la fixture non plus
    masked = next(p["masked_key"] for p in api.bootstrap()["providers"] if p["id"] == "openai")
    assert masked and "…" in masked
    assert api.delete_key("openai")["ok"]


def test_empty_key_is_refused(api: JsApi) -> None:
    result = api.save_key("openai", "   ")
    assert not result["ok"] and result["message"]


def test_conversation_flow(api: JsApi, corpus: Corpus) -> None:
    created = api.new_conversation()
    conversation_id = created["conversation"]["id"]
    assert created["conversation"]["needs_key"] is False
    api.service.history.update_conversation(conversation_id, workdir=str(corpus.root))

    sent = api.send(conversation_id, "Quelle échéance ?")
    assert sent["ok"] and api.service.wait(60)
    opened = api.open_conversation(conversation_id)
    roles = [item["role"] for item in opened["items"]]
    assert roles[0] == "hidden"  # l'arborescence n'est pas affichée telle quelle
    assert "user" in roles and "assistant" in roles
    answer = next(item for item in opened["items"] if item["role"] == "assistant")
    assert answer["sources"] == [
        {"path": "notes.txt", "verified": False, "workdir": str(corpus.root)}
    ]
    assert api.list_conversations()["conversations"][0]["id"] == conversation_id
    assert api.list_conversations("échéance")["conversations"][0]["id"] == conversation_id
    turn_id = api.service.history.turns(conversation_id)[0].id
    details = api.turn_details(turn_id)
    assert details["ok"] and isinstance(details["steps"], list)
    assert api.delete_conversation(conversation_id)["conversations"] == []


def test_refusals_come_back_as_json(api: JsApi) -> None:
    assert api.open_conversation("inconnue") == {
        "ok": False,
        "key": "agent.errors.unknown_conversation",
        "message": api.open_conversation("inconnue")["message"],
    }
    assert not api.set_mode("inconnue", "turbo")["ok"]
    assert not api.stop()["ok"]  # rien à arrêter


def test_folder_dialogs(api: JsApi, corpus: Corpus, tmp_path: Path) -> None:
    conversation_id = api.new_conversation()["conversation"]["id"]
    api.chooser = StubChooser(
        [str(corpus.root), str(corpus.root / "contrats"), None, str(tmp_path)]
    )
    assert api.choose_workdir(conversation_id)["conversation"]["workdir"] == str(corpus.root)
    scope = api.choose_scope(conversation_id)
    assert scope["conversation"]["scope_rel"] == "contrats"
    assert api.choose_scope(conversation_id)["cancelled"] is True
    outside = api.choose_scope(conversation_id)
    assert not outside["ok"]  # dossier hors du dossier de travail


def test_settings_patch_and_validation(api: JsApi) -> None:
    updated = api.update_settings({"theme": "dark", "general_knowledge": True, "inconnu": 1})
    assert updated["settings"]["theme"] == "dark"
    assert updated["settings"]["general_knowledge"] is True
    api.update_settings({"mode_models": {"mistral": {"quick": "mistral-small-latest"}}})
    assert api.service.settings.mode_models["mistral"].quick == "mistral-small-latest"
    api.update_settings(
        {
            "custom_provider": {
                "base_url": "http://localhost:11434/v1",
                "models": [{"id": "llama3.1"}],
            }
        }
    )
    models = api.bootstrap()["settings"]["custom_provider"]["models"]
    assert models == [{"id": "llama3.1", "context_window": None, "max_output": None}]
    assert not api.update_settings({"extra_models": {"mistral": [{"id": ""}]}})["ok"]


def test_custom_url_checks(api: JsApi) -> None:
    assert api.check_custom_url("http://localhost:11434/v1")["local"] is True
    assert api.check_custom_url("http://192.168.1.10:11434/v1")["cleartext_remote"] is True
    assert not api.check_custom_url("pas une adresse")["ok"]


def test_storage_and_purges(api: JsApi) -> None:
    assert api.storage_info()["ok"]
    assert api.purge_cache()["cache_bytes"] == 0
    assert api.purge_history()["conversations"] == []
    assert api.purge_logs()["ok"]


def test_ui_texts_are_complete() -> None:
    texts = ui_texts()
    assert all(key.startswith("ui.") for key in texts)
    assert all(isinstance(value, str) and value for value in texts.values())
    assert "ui.errors.bridge" in texts and "ui.transparency.iteration" in texts


# --- passeur d'événements ------------------------------------------------------------


class StubWindow:
    def __init__(self, fail: bool = False) -> None:
        self.scripts: list[str] = []
        self.fail = fail

    def evaluate_js(self, script: str) -> None:
        if self.fail:
            raise RuntimeError("fenêtre fermée")
        self.scripts.append(script)


def test_event_json_is_safe() -> None:
    event = Event("r1", "c1", EventType.STEP_STARTED, {"tool": "read_file", "target": 'a"b.pdf'})
    script = f"window.archipelleEvent({event_json(event)})"
    assert '\\"b.pdf' in script and script.endswith(")")
    assert "é" in event_json(Event("r", "c", EventType.ERROR, {"message": "clé"}))


def test_bridge_delivers_and_survives_a_closed_window() -> None:
    bridge = EventBridge(queue.Queue())
    window = StubWindow()
    bridge.start(window)
    try:
        bridge.publish(Event("r1", "c1", EventType.ITERATION, {"index": 1}))
        deadline = 0
        while not window.scripts and deadline < 50:
            deadline += 1
            __import__("time").sleep(0.02)
        assert window.scripts and "iteration" in window.scripts[0]
    finally:
        bridge.stop()
    broken = EventBridge(queue.Queue())
    broken.start(StubWindow(fail=True))
    broken.deliver(Event("r", "c", EventType.TURN_ENDED, {}))  # ne lève pas
    broken.stop()


def test_rename_and_auto_title(api: JsApi, corpus: Corpus) -> None:
    created = api.new_conversation()
    conversation_id = created["conversation"]["id"]
    api.service.history.update_conversation(conversation_id, workdir=str(corpus.root))
    assert created["conversation"]["title"] == ""

    # Le titre est repris de la première question.
    assert api.send(conversation_id, "Quelle est l'échéance du bail signé en 2022 ?")["ok"]
    assert api.service.wait(60)
    listed = api.list_conversations()["conversations"][0]
    assert listed["title"] == "Quelle est l'échéance du bail signé en 2022 ?"

    # Une question longue est coupée sur un mot entier.
    other = api.new_conversation()["conversation"]["id"]
    api.service.history.update_conversation(other, workdir=str(corpus.root))
    long_question = "Peux-tu me dire " + "très " * 30 + "précisément le montant ?"
    assert api.send(other, long_question)["ok"] and api.service.wait(60)
    title = api.open_conversation(other)["conversation"]["title"]
    assert len(title) <= 61 and title.endswith("…") and "  " not in title

    # Renommage manuel.
    renamed = api.rename_conversation(conversation_id, "  Bail 2022  ")
    assert renamed["ok"] and renamed["conversation"]["title"] == "Bail 2022"
    assert any(c["title"] == "Bail 2022" for c in renamed["conversations"])
    assert not api.rename_conversation(conversation_id, "   ")["ok"]
    assert not api.rename_conversation("inconnue", "x")["ok"]


def test_delete_returns_the_updated_list(api: JsApi) -> None:
    first = api.new_conversation()["conversation"]["id"]
    api.new_conversation()
    remaining = api.delete_conversation(first)["conversations"]
    assert all(c["id"] != first for c in remaining) and len(remaining) == 1


def test_window_is_not_reachable_from_public_attributes(api: JsApi) -> None:
    """pywebview parcourt les attributs publics de l'objet exposé au JS : la fenêtre ne
    doit pas s'y trouver, sous peine de parcours infini de ses objets natifs."""
    chooser = FolderChooser()
    sentinel = object()
    chooser.attach(sentinel)
    assert chooser.attached
    public = {name: getattr(chooser, name) for name in dir(chooser) if not name.startswith("_")}
    assert sentinel not in public.values()
    assert "window" not in public

    api.chooser = chooser
    exposed = {name: getattr(api, name) for name in dir(api) if not name.startswith("_")}
    assert sentinel not in exposed.values()


def test_extra_models_are_offered_for_any_provider(api: JsApi) -> None:
    """Un modèle saisi à la main rejoint le sélecteur du fournisseur concerné (CDC §13bis)."""
    updated = api.update_settings({"extra_models": {"mistral": [{"id": "ministral-14b-2512"}]}})
    assert updated["ok"]
    mistral = next(p for p in updated["providers"] if p["id"] == "mistral")
    identifiers = [m["id"] for m in mistral["models"]]
    assert "ministral-14b-2512" in identifiers
    assert "ministral-14b-latest" in identifiers  # les Ministral du catalogue sont proposés
    added = next(m for m in mistral["models"] if m["id"] == "ministral-14b-2512")
    assert added["in_catalog"] is False

    conversation_id = api.new_conversation()["conversation"]["id"]
    assert api.set_model(conversation_id, "ministral-14b-2512")["ok"]
    info = api.service.model_info("mistral", "ministral-14b-2512")
    assert info.tools and not info.in_catalog  # outils supposés pris en charge

    cleared = api.update_settings({"extra_models": {}})
    assert all(
        m["id"] != "ministral-14b-2512"
        for p in cleared["providers"]
        if p["id"] == "mistral"
        for m in p["models"]
    )


def test_rate_limit_message_points_to_another_model() -> None:
    from archipelle.providers.base import RateLimited

    message = RateLimited("Mistral").message()
    assert "Mistral" in message and "un autre" in message and "Paramètres avancés" in message


def test_finished_turns_carry_an_end_marker(api: JsApi, corpus: Corpus) -> None:
    """Sans repère de fin, une réponse qui s'arrête net se confond avec une recherche
    encore en cours (cas observé avec un petit modèle)."""
    conversation_id = api.new_conversation()["conversation"]["id"]
    api.service.history.update_conversation(conversation_id, workdir=str(corpus.root))
    assert api.send(conversation_id, "Quelle échéance ?")["ok"] and api.service.wait(60)

    items = api.open_conversation(conversation_id)["items"]
    marked = [item for item in items if "end" in item]
    assert len(marked) == 1  # un seul repère, à la fin du tour
    assert marked[0] is items[-1]
    assert marked[0]["end"]["status"] == "complete"
    assert marked[0]["end"]["seconds"] >= 0
