"""Fonctions système. Seul module du code où figurent des conditions par OS assumées."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
# Systèmes de fichiers habituellement insensibles à la casse (CDC §9).
CASE_INSENSITIVE_FS = IS_WINDOWS or IS_MACOS


def open_with_default_app(path: Path) -> None:
    """Ouvre un fichier avec l'application par défaut du système (CDC §10).

    Ne fait aucun contrôle : l'appelant (``tools/open_source.py``) a déjà validé le chemin,
    le statut de source vérifiée et l'extension. Lève ``OSError`` en cas d'échec.
    """
    target = str(path)
    if IS_WINDOWS:
        os.startfile(target)  # type: ignore[attr-defined]
        return
    command = ["open", target] if IS_MACOS else ["xdg-open", target]
    subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def cpu_workers() -> int:
    """Nombre de processus d'extraction : cœurs − 1, au moins 1 (CDC §4bis)."""
    return max(1, (os.cpu_count() or 2) - 1)
