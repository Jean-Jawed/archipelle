"""Logs techniques (CDC §14).

Fichier local avec rotation (5 fichiers de 10 Mo), clés API toujours masquées, aucun
contenu (document, prompt, réponse) journalisé hors mode diagnostic.
"""

from __future__ import annotations

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from typing import Any, cast

from archipelle.core.dirs import AppDirs
from archipelle.core.masking import mask_known_secrets
from archipelle.core.resources import is_frozen

LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 4  # fichier courant + 4 archives = 5 fichiers
_FORMAT = "%(asctime)s %(levelname)-7s %(threadName)s %(name)s — %(message)s"
_NOISY_LOGGERS = ("httpx", "httpcore", "openai", "anthropic", "mistralai", "urllib3", "PIL")

_lock = threading.Lock()
_diagnostic = threading.Event()
_installed: list[logging.Handler] = []


class MaskingFormatter(logging.Formatter):
    """Formateur qui masque les clés dans le message final, traces d'exception comprises."""

    def format(self, record: logging.LogRecord) -> str:
        return mask_known_secrets(super().format(record))


def set_diagnostic(enabled: bool) -> None:
    if enabled:
        _diagnostic.set()
    else:
        _diagnostic.clear()
    logging.getLogger("archipelle").info("Mode diagnostic %s", "activé" if enabled else "désactivé")


def is_diagnostic() -> bool:
    return _diagnostic.is_set()


def log_content(logger: logging.Logger, label: str, content: object) -> None:
    """Journalise un contenu (texte de document, prompt, réponse) en mode diagnostic seulement."""
    if _diagnostic.is_set():
        logger.info("[diagnostic] %s :\n%s", label, content)


def configure_logging(
    dirs: AppDirs, *, diagnostic: bool = False, level: int = logging.INFO
) -> None:
    """Installe les gestionnaires de logs.

    Peut être rappelée : l'installation précédente est alors retirée.
    """
    with _lock:
        _remove_installed()
        dirs.logs.mkdir(parents=True, exist_ok=True)
        formatter = MaskingFormatter(_FORMAT)

        file_handler = RotatingFileHandler(
            dirs.log_file, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        handlers: list[logging.Handler] = [file_handler]

        if not is_frozen():
            console = logging.StreamHandler(sys.stderr)
            console.setLevel(logging.WARNING)
            console.setFormatter(formatter)
            handlers.append(console)

        root = logging.getLogger()
        root.setLevel(level)
        for handler in handlers:
            root.addHandler(handler)
            _installed.append(handler)
        for name in _NOISY_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)

    if diagnostic:
        _diagnostic.set()
    else:
        _diagnostic.clear()


def shutdown_logging() -> None:
    with _lock:
        _remove_installed()


def purge_logs(dirs: AppDirs) -> None:
    """Supprime tous les fichiers de logs puis réinstalle la journalisation (CDC §12)."""
    diagnostic = _diagnostic.is_set()
    shutdown_logging()
    for path in dirs.logs.glob(f"{dirs.log_file.name}*"):
        path.unlink(missing_ok=True)
    configure_logging(dirs, diagnostic=diagnostic)


def _remove_installed() -> None:
    root = logging.getLogger()
    while _installed:
        handler = _installed.pop()
        root.removeHandler(handler)
        handler.close()


def loggable_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Requête allégée pour le journal : les définitions d'outils, identiques à chaque
    appel, sont remplacées par leur nombre (elles pesaient l'essentiel du fichier)."""
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return payload
    return {**payload, "tools": f"<{len(cast(list[Any], tools))} outils déclarés>"}
