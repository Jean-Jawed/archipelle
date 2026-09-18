from __future__ import annotations

import json
import sqlite3
import stat
import sys
import threading
from pathlib import Path

import pytest

from archipelle.core import masking
from archipelle.core.dirs import AppDirs
from archipelle.persistence.db import Database, MigrationError
from archipelle.persistence.settings_store import (
    CustomModel,
    ModeModels,
    ScanExclusions,
    SecretsStore,
    Settings,
    SettingsStore,
)

# --- base SQLite ---------------------------------------------------------------------


@pytest.mark.parametrize(("name", "table"), [("history", "conversations"), ("cache", "pages")])
def test_migrations_apply_and_are_idempotent(tmp_path: Path, name: str, table: str) -> None:
    db = Database(tmp_path / f"{name}.db", name)
    try:
        version = db.migrate()
        assert version >= 1
        assert db.migrate() == version
        conn = db.connection()
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        assert table in tables
    finally:
        db.close_all()


def test_newer_database_is_refused(tmp_path: Path) -> None:
    db = Database(tmp_path / "h.db", "history")
    try:
        db.connection().execute("PRAGMA user_version = 999")
        with pytest.raises(MigrationError):
            db.migrate()
    finally:
        db.close_all()


def test_one_connection_per_thread(tmp_path: Path) -> None:
    db = Database(tmp_path / "h.db", "history")
    try:
        db.migrate()
        main = db.connection()
        assert db.connection() is main
        seen: list[sqlite3.Connection] = []
        thread = threading.Thread(target=lambda: seen.append(db.connection()))
        thread.start()
        thread.join()
        assert seen and seen[0] is not main
    finally:
        db.close_all()


def _insert_conversation(conn: sqlite3.Connection, conv_id: str) -> None:
    conn.execute(
        "INSERT INTO conversations (id, created_at, updated_at, provider_id, model)"
        " VALUES (?, 0, 0, 'mistral', 'm')",
        (conv_id,),
    )


def test_transaction_commits_and_rolls_back(tmp_path: Path) -> None:
    db = Database(tmp_path / "h.db", "history")
    try:
        db.migrate()
        with db.transaction() as conn:
            _insert_conversation(conn, "ok")
        with pytest.raises(RuntimeError), db.transaction() as conn:
            _insert_conversation(conn, "annulée")
            raise RuntimeError("échec")
        ids = [row["id"] for row in db.connection().execute("SELECT id FROM conversations")]
        assert ids == ["ok"]
    finally:
        db.close_all()


def test_cascade_delete_and_accent_insensitive_fts(tmp_path: Path) -> None:
    db = Database(tmp_path / "h.db", "history")
    try:
        db.migrate()
        with db.transaction() as conn:
            _insert_conversation(conn, "c1")
            conn.execute(
                "INSERT INTO turns (id, conversation_id, seq, run_id, mode, status, started_at)"
                " VALUES ('t1', 'c1', 1, 'r1', 'quick', 'complete', 0)"
            )
            conn.execute(
                "INSERT INTO items (turn_id, seq, kind, payload, created_at)"
                " VALUES ('t1', 1, 'user', '{}', 0)"
            )
            conn.execute(
                "INSERT INTO items_fts (text, conversation_id, item_id)"
                " VALUES ('Quelle est la date d''échéance du bail ?', 'c1', 1)"
            )
        conn = db.connection()
        hits = conn.execute(
            "SELECT conversation_id FROM items_fts WHERE items_fts MATCH ?", ("echeance",)
        ).fetchall()
        assert [row[0] for row in hits] == ["c1"]
        with db.transaction() as conn:
            conn.execute("DELETE FROM conversations WHERE id = 'c1'")
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    finally:
        db.close_all()


# --- réglages ------------------------------------------------------------------------


def test_settings_defaults(app_dirs: AppDirs) -> None:
    store = SettingsStore(app_dirs.settings_file)
    settings = store.get()
    assert settings.default_provider == "mistral"
    assert not settings.onboarding_done
    assert not settings.general_knowledge
    exclusions = settings.effective_scan_exclusions()
    assert ".git" in exclusions.dir_names
    assert "~$*" in exclusions.file_patterns


def test_settings_roundtrip(app_dirs: AppDirs) -> None:
    store = SettingsStore(app_dirs.settings_file)

    def change(settings: Settings) -> None:
        settings.onboarding_done = True
        settings.default_workdir = "/data/docs"
        settings.mode_models["openai"] = ModeModels(quick="small", explore="large")
        settings.extra_models["mistral"] = [CustomModel("perso", context_window=8000)]
        settings.custom_provider.base_url = "http://localhost:11434/v1"
        settings.custom_provider.models.append(CustomModel("llama", 4096, 1024))
        settings.scan_exclusions = ScanExclusions(["build"], ["*.bak"], hide_dotfiles=False)
        settings.theme = "dark"

    store.update(change)
    reloaded = SettingsStore(app_dirs.settings_file).get()
    assert reloaded.onboarding_done
    assert reloaded.mode_models["openai"].for_mode("explore") == "large"
    assert reloaded.extra_models["mistral"][0].context_window == 8000
    assert reloaded.custom_provider.models[0] == CustomModel("llama", 4096, 1024)
    assert reloaded.effective_scan_exclusions().dir_names == ["build"]
    assert reloaded.theme == "dark"


def test_settings_get_returns_a_copy(app_dirs: AppDirs) -> None:
    store = SettingsStore(app_dirs.settings_file)
    copy = store.get()
    copy.default_provider = "openai"
    assert store.get().default_provider == "mistral"


def test_failed_update_changes_nothing(app_dirs: AppDirs) -> None:
    store = SettingsStore(app_dirs.settings_file)

    def broken(settings: Settings) -> None:
        settings.default_provider = "openai"
        raise ValueError("refus")

    with pytest.raises(ValueError, match="refus"):
        store.update(broken)
    assert store.get().default_provider == "mistral"


def test_corrupt_settings_are_set_aside(app_dirs: AppDirs) -> None:
    app_dirs.settings_file.write_text("{ pas du json", encoding="utf-8")
    settings = SettingsStore(app_dirs.settings_file).get()
    assert settings.default_provider == "mistral"
    assert list(app_dirs.config.glob("config.json.corrupt-*"))


def test_invalid_values_fall_back_to_defaults(app_dirs: AppDirs) -> None:
    app_dirs.settings_file.write_text(
        json.dumps(
            {
                "onboarding_done": "oui",
                "default_provider": 12,
                "theme": "violet",
                "mode_models": {"openai": {"quick": 3, "explore": "large"}},
                "custom_provider": {"base_url": "http://x", "models": [{"id": ""}, "bad"]},
                "extra_models": {"mistral": [{"id": "m", "context_window": -5}]},
                "inconnu": True,
            }
        ),
        encoding="utf-8",
    )
    settings = SettingsStore(app_dirs.settings_file).get()
    assert settings.onboarding_done is False
    assert settings.default_provider == "mistral"
    assert settings.theme == "light"
    assert settings.mode_models["openai"] == ModeModels(quick=None, explore="large")
    assert settings.custom_provider.base_url == "http://x"
    assert settings.custom_provider.models == []
    assert settings.extra_models["mistral"][0].context_window is None


# --- clés API ------------------------------------------------------------------------


def test_secrets_roundtrip_and_masking(app_dirs: AppDirs) -> None:
    store = SecretsStore(app_dirs.secrets_file)
    assert store.masked("mistral") is None
    store.set("mistral", "  mk-abcdefghijklmnop9876  ")
    assert store.get("mistral") == "mk-abcdefghijklmnop9876"
    assert store.masked("mistral") == "mk-…9876"
    assert store.has_key("mistral")
    assert masking.mask_known_secrets("mk-abcdefghijklmnop9876") == "mk-…9876"

    reloaded = SecretsStore(app_dirs.secrets_file)
    assert reloaded.providers_with_key() == ["mistral"]


def test_secrets_file_is_separate_from_settings(app_dirs: AppDirs) -> None:
    SecretsStore(app_dirs.secrets_file).set("openai", "sk-abcdefghijklmnopqrst")
    SettingsStore(app_dirs.settings_file).update(lambda s: setattr(s, "onboarding_done", True))
    assert "sk-" not in app_dirs.settings_file.read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform == "win32", reason="permissions POSIX")
def test_secrets_file_is_private(app_dirs: AppDirs) -> None:
    SecretsStore(app_dirs.secrets_file).set("openai", "sk-abcdefghijklmnopqrst")
    mode = stat.S_IMODE(app_dirs.secrets_file.stat().st_mode)
    assert mode == 0o600


def test_secrets_delete_and_purge(app_dirs: AppDirs) -> None:
    store = SecretsStore(app_dirs.secrets_file)
    store.set("openai", "sk-abcdefghijklmnopqrst")
    store.set("anthropic", "sk-ant-abcdefghijklmnop")
    store.set("mistral", "   ")  # clé vide : équivaut à une suppression
    store.delete("openai")
    assert store.providers_with_key() == ["anthropic"]
    store.purge_all()
    assert SecretsStore(app_dirs.secrets_file).providers_with_key() == []
