"""Assemblage complet de l'application, sans fenêtre."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from archipelle import app
from archipelle.core.dirs import resolve_dirs
from archipelle.core.events import Event, EventType
from archipelle.providers.fake import FakeProvider
from tests.corpus.make_corpus import Corpus


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ARCHIPELLE_HOME", str(tmp_path))
    return tmp_path


def test_build_wires_every_layer(home: Path, corpus: Corpus) -> None:
    application = app.build()
    try:
        assert application.api.bootstrap()["ok"]
        assert application.dirs.history_db.exists()
        assert application.service.history.list_conversations() == []
        # Les migrations des deux bases sont passées.
        assert application.cache.size_bytes() == 0
        conversation = application.service.new_conversation(str(corpus.root))
        assert application.api.open_conversation(conversation.id)["ok"]
        # Les événements du worker partent bien dans la file du pont.
        application.bridge.publish(Event("r", "c", EventType.APP_NOTICE, {"key": "x"}))
        assert application.bridge.events.qsize() == 1
    finally:
        application.close()


def test_demo_mode_replaces_providers(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = home / "demo.json"
    script.write_text('{"steps": [{"text": "Bonjour"}]}', encoding="utf-8")
    monkeypatch.setenv(app.FAKE_SCRIPT_VARIABLE, str(script))
    application = app.build()
    try:
        provider = application.service.build_provider(None)  # type: ignore[arg-type]
        assert isinstance(provider, FakeProvider)
        # Aucune clé n'est exigée en mode démo, sans toucher au catalogue partagé.
        assert application.service.require_keys is False
        assert application.service.catalog.provider("mistral").key_required is True
        conversation = application.service.new_conversation(str(home))
        assert application.service.send(conversation.id, "Bonjour").ok
        assert application.service.wait(30)
        assert application.service.last_result is not None
        assert application.service.last_result.answer == "Bonjour"
    finally:
        application.close()


def test_startup_repairs_unfinished_turns(home: Path, corpus: Corpus) -> None:
    first = app.build()
    try:
        conversation = first.service.new_conversation(str(corpus.root))
        first.service.history.open_turn(conversation.id, "run", "quick", str(corpus.root))
    finally:
        first.close()
    second = app.build()
    try:
        assert second.service.history.unfinished_turns() == []
        assert second.service.history.turns(conversation.id)[0].status == "interrupted"
    finally:
        second.close()


def test_home_override_is_respected(home: Path) -> None:
    dirs = resolve_dirs()
    assert str(home) in str(dirs.config)
    assert os.environ["ARCHIPELLE_HOME"] == str(home)
