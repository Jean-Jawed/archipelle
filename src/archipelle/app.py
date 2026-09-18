"""Assemblage de l'application (docs/ARCHITECTURE.md §10).

Un seul endroit construit les objets de toutes les couches et les relie ; `__main__.py`
se contente d'appeler `main()`.
"""

from __future__ import annotations

import contextlib
import logging
import multiprocessing
import os
import queue
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from archipelle.agent.history import History
from archipelle.agent.service import AgentService
from archipelle.cache.store import ExtractionCache
from archipelle.core.dirs import AppDirs, resolve_dirs
from archipelle.core.events import Event
from archipelle.core.logging_setup import configure_logging, set_diagnostic, shutdown_logging
from archipelle.extraction import ocr
from archipelle.persistence.db import Database
from archipelle.persistence.settings_store import SecretsStore, SettingsStore
from archipelle.providers.fake import FakeProvider
from archipelle.tools.documents import DocumentAccess
from archipelle.tools.process_pool import ExtractionPool
from archipelle.ui.bridge import EventBridge
from archipelle.ui.js_api import JsApi

_log = logging.getLogger(__name__)

FAKE_SCRIPT_VARIABLE = "ARCHIPELLE_FAKE_SCRIPT"  # mode démo (CDC §14)


@dataclass
class Application:
    dirs: AppDirs
    history_db: Database
    cache: ExtractionCache
    pool: ExtractionPool
    documents: DocumentAccess
    service: AgentService
    bridge: EventBridge
    api: JsApi

    def close(self) -> None:
        _log.info("Fermeture de l'application")
        self.bridge.stop()
        self.service.shutdown()
        self.cache.close()
        self.history_db.close_all()
        shutdown_logging()


def build(dirs: AppDirs | None = None) -> Application:
    """Construit l'application ; utilisable sans fenêtre (tests, mode démo)."""
    app_dirs = (dirs or resolve_dirs()).ensure()
    settings_store = SettingsStore(app_dirs.settings_file)
    settings = settings_store.get()
    configure_logging(app_dirs, diagnostic=settings.diagnostic)
    set_diagnostic(settings.diagnostic)
    _log.info("Démarrage d'Archipelle")

    history_db = Database(app_dirs.history_db, "history")
    history_db.migrate()
    history = History(history_db)

    cache = ExtractionCache(app_dirs.cache)
    pool = ExtractionPool()
    ocr_config = ocr.detect()
    documents = DocumentAccess(cache, pool, ocr_config)

    events: queue.Queue[Event] = queue.Queue()
    bridge = EventBridge(events)
    service = AgentService(
        history=history,
        settings_store=settings_store,
        secrets=SecretsStore(app_dirs.secrets_file),
        cache=cache,
        pool=pool,
        documents=documents,
        publish=bridge.publish,
        ocr_available=ocr_config is not None,
    )
    _install_demo_provider(service)
    service.startup()
    return Application(
        dirs=app_dirs,
        history_db=history_db,
        cache=cache,
        pool=pool,
        documents=documents,
        service=service,
        bridge=bridge,
        api=JsApi(service, dirs=app_dirs),
    )


def _install_demo_provider(service: AgentService) -> None:
    """Mode démo : le faux fournisseur remplace les vrais, sans clé ni réseau (CDC §14)."""
    script = os.environ.get(FAKE_SCRIPT_VARIABLE)
    if not script:
        return
    provider = FakeProvider.from_file(Path(script))
    service.build_provider = lambda config: provider
    service.require_keys = False
    _log.warning("Mode démo actif : les réponses sont simulées (%s)", script)


def main() -> int:
    multiprocessing.freeze_support()
    with contextlib.suppress(RuntimeError):  # déjà défini dans le même processus
        multiprocessing.set_start_method("spawn")
    application = build()
    try:
        import webview

        from archipelle.ui.window import create_window

        window = create_window(application.api)
        attach_window(application, window)
        webview.start()
    except Exception:
        _log.exception("Échec du démarrage de l'interface")
        return 1
    finally:
        application.close()
    return 0


def attach_window(application: Application, window: Any) -> None:
    """Relie la fenêtre aux objets qui en ont besoin.

    Le dialogue natif de choix de dossier n'existe qu'à travers la fenêtre : sans ce lien,
    les boutons « parcourir » ne font rien.
    """
    application.api.chooser.attach(window)
    application.bridge.start(window)


def run_headless(action: Any) -> Any:
    """Exécute une action avec l'application construite, sans fenêtre (tests, scripts)."""
    application = build()
    try:
        return action(application)
    finally:
        application.close()
