"""Lecture et écriture sûres des fichiers JSON de configuration."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, cast

_log = logging.getLogger(__name__)


def write_json_atomic(path: Path, data: Any, *, private: bool = False) -> None:
    """Écrit un fichier JSON sans jamais laisser de fichier à moitié écrit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    if private:
        _restrict_permissions(tmp)
    tmp.replace(path)


def read_json_object(path: Path) -> dict[str, Any] | None:
    """Lit un objet JSON. Un fichier illisible est mis de côté et ``None`` est renvoyé."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        _log.warning("Fichier %s illisible, mis de côté", path.name)
        _set_aside(path)
        return None
    if not isinstance(data, dict):
        _log.warning("Fichier %s mal formé, mis de côté", path.name)
        _set_aside(path)
        return None
    return cast(dict[str, Any], data)


def _set_aside(path: Path) -> None:
    try:
        path.replace(path.with_name(f"{path.name}.corrupt-{int(time.time())}"))
    except OSError:
        _log.exception("Impossible de mettre de côté %s", path.name)


def _restrict_permissions(path: Path) -> None:
    # Sans effet notable sous Windows, où le répertoire de l'utilisateur est déjà privé.
    try:
        path.chmod(0o600)
    except OSError:
        _log.debug("Permissions non modifiables pour %s", path.name)
