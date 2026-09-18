from __future__ import annotations

import json
import logging
import logging.handlers
import threading
from pathlib import Path

import pytest

from archipelle.core import i18n, logging_setup, masking, resources
from archipelle.core.cancel import CancelToken
from archipelle.core.dirs import AppDirs
from archipelle.core.events import Event, EventType
from archipelle.core.outcome import AppError, Cancelled, Err, Ok


def test_resources_are_found() -> None:
    assert resources.resource_path("i18n", "fr.json").is_file()
    assert resources.package_path("persistence", "migrations", "history").is_dir()
    assert not resources.is_frozen()
    assert resources.bundled_binary_dir() is None


def test_app_dirs_are_isolated(app_dirs: AppDirs, tmp_path: Path) -> None:
    assert app_dirs.config.is_relative_to(tmp_path)
    for directory in (app_dirs.config, app_dirs.data, app_dirs.cache, app_dirs.logs):
        assert directory.is_dir()
    assert app_dirs.settings_file.parent == app_dirs.config


def test_translation_with_parameters() -> None:
    text = i18n.t("errors.search.keyword_too_short", keyword="an", min=3)
    assert "« an »" in text
    assert "3" in text


def test_translation_missing_key_returns_key(caplog: pytest.LogCaptureFixture) -> None:
    assert i18n.t("does.not.exist") == "does.not.exist"
    assert "does.not.exist" in caplog.text


def test_translation_missing_parameter_is_left_visible() -> None:
    assert "{path}" in i18n.t("errors.path.invalid")


def test_every_translation_is_a_string() -> None:
    flat = i18n.catalog()
    assert flat
    assert all(isinstance(value, str) and value for value in flat.values())


def test_format_number() -> None:
    assert i18n.format_number(1240) == "1\u202f240"
    assert i18n.format_number(1234.5) == "1\u202f234,50"


def test_app_error_message_and_outcomes() -> None:
    error = AppError("errors.path.not_found", path="a.pdf")
    assert "a.pdf" in error.message()
    err = Err.from_error(error)
    assert not err.ok and "a.pdf" in err.message()
    assert Ok(3).ok and Ok(3).value == 3
    assert Cancelled().key == "errors.cancelled"


@pytest.mark.parametrize(
    ("secret", "expected"),
    [
        ("sk-proj-abcdefghijklmnopa4f2", "sk-…a4f2"),
        ("abcdefghijklmnop1234", "…1234"),
        ("short", "••••"),
    ],
)
def test_mask_secret(secret: str, expected: str) -> None:
    assert masking.mask_secret(secret) == expected


def test_mask_known_and_generic_secrets() -> None:
    masking.register_secret("MistralKey0123456789XYZ")
    text = masking.mask_known_secrets(
        "clé MistralKey0123456789XYZ, autre sk-ant-api03-abcdefghijklmnop, "
        "Authorization: Bearer abcdefghijklmnopqrstu, api_key=zzzzzzzzzzzzzzzz"
    )
    assert "MistralKey0123456789XYZ" not in text
    assert "…9XYZ" in text
    assert "abcdefghijklmnop" not in text
    assert "abcdefghijklmnopqrstu" not in text
    assert "zzzzzzzzzzzzzzzz" not in text


def _read_log(dirs: AppDirs) -> str:
    for handler in logging.getLogger().handlers:
        handler.flush()
    return dirs.log_file.read_text(encoding="utf-8")


def test_logs_mask_keys_even_in_tracebacks(app_dirs: AppDirs) -> None:
    logging_setup.configure_logging(app_dirs)
    try:
        masking.register_secret("secret-value-1234567890")
        log = logging.getLogger("archipelle.test")
        log.info("appel avec %s", "secret-value-1234567890")
        try:
            raise RuntimeError("échec secret-value-1234567890")
        except RuntimeError:
            log.exception("erreur")
        content = _read_log(app_dirs)
        assert "secret-value-1234567890" not in content
        assert "secret-…7890" in content
    finally:
        logging_setup.shutdown_logging()


def test_content_logged_only_in_diagnostic_mode(app_dirs: AppDirs) -> None:
    logging_setup.configure_logging(app_dirs, diagnostic=False)
    try:
        log = logging.getLogger("archipelle.test")
        logging_setup.log_content(log, "prompt", "CONTENU-CONFIDENTIEL")
        assert "CONTENU-CONFIDENTIEL" not in _read_log(app_dirs)
        logging_setup.set_diagnostic(True)
        assert logging_setup.is_diagnostic()
        logging_setup.log_content(log, "prompt", "CONTENU-DIAGNOSTIC")
        assert "CONTENU-DIAGNOSTIC" in _read_log(app_dirs)
    finally:
        logging_setup.set_diagnostic(False)
        logging_setup.shutdown_logging()


def test_logging_rotation_settings_and_purge(app_dirs: AppDirs) -> None:
    logging_setup.configure_logging(app_dirs)
    try:
        installed = logging_setup._installed  # pyright: ignore[reportPrivateUsage]
        rotating = [h for h in installed if isinstance(h, logging.handlers.RotatingFileHandler)]
        assert rotating[0].maxBytes == 10 * 1024 * 1024
        assert rotating[0].backupCount + 1 == 5
        logging.getLogger("archipelle.test").warning("avant purge")
        (app_dirs.logs / "archipelle.log.1").write_text("ancien", encoding="utf-8")
        logging_setup.purge_logs(app_dirs)
        assert not (app_dirs.logs / "archipelle.log.1").exists()
        assert "avant purge" not in _read_log(app_dirs)
    finally:
        logging_setup.shutdown_logging()


def test_cancel_token_runs_callbacks_once() -> None:
    token = CancelToken()
    calls: list[str] = []
    handle = token.add_callback(lambda: calls.append("a"))
    removed = token.add_callback(lambda: calls.append("b"))
    token.remove_callback(removed)
    token.raise_if_cancelled()
    token.cancel()
    token.cancel()
    assert calls == ["a"]
    assert handle >= 0
    with pytest.raises(Cancelled):
        token.raise_if_cancelled()
    token.add_callback(lambda: calls.append("late"))
    assert calls == ["a", "late"]


def test_cancel_token_survives_failing_callback() -> None:
    token = CancelToken()
    calls: list[int] = []

    def boom() -> None:
        raise ValueError("boom")

    token.add_callback(boom)
    token.add_callback(lambda: calls.append(1))
    token.cancel()
    assert calls == [1]


def test_cancel_token_wait_wakes_up() -> None:
    token = CancelToken()
    threading.Timer(0.05, token.cancel).start()
    assert token.wait(5)


def test_event_serialization() -> None:
    event = Event("run", "conv", EventType.FILE_CONSULTED, {"path": "a/é.pdf"})
    data = json.loads(event.to_json())
    assert data["type"] == "file_consulted"
    assert data["payload"]["path"] == "a/é.pdf"
