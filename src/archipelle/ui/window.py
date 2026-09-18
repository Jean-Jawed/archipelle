"""Création de la fenêtre pywebview (docs/ARCHITECTURE.md §9)."""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Any

from archipelle.core import resources
from archipelle.core.i18n import t

_log = logging.getLogger(__name__)

# Ouverture en fenêtre maximisée 🔧 : l'interface est dense (liste, conversation, saisie)
# et confortable sur toute la largeur. « maximized » garde la barre de titre et les
# commandes de la fenêtre, contrairement à « fullscreen ». Les dimensions ci-dessous
# servent quand l'utilisateur restaure la fenêtre.
MAXIMIZED = True

MIN_WIDTH = 900
MIN_HEIGHT = 600
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 820


def index_path() -> Path:
    return resources.resource_path("..", "ui", "web", "index.html")


def create_window(api: object, *, debug: bool = False) -> Any:
    webview: Any = importlib.import_module("webview")
    window = webview.create_window(
        title=t("app.name"),
        url=str(index_path()),
        js_api=api,
        width=DEFAULT_WIDTH,
        height=DEFAULT_HEIGHT,
        min_size=(MIN_WIDTH, MIN_HEIGHT),
        maximized=MAXIMIZED,
        text_select=True,
        confirm_close=False,
    )
    _log.info("Fenêtre créée (debug=%s)", debug)
    return window
