"""Ouverture d'une source cliquée dans l'interface (CDC §9).

Triple contrôle : chemin validé par rapport au dossier de travail de la source (sans la
restriction « parcourir »), source vérifiée, extension parmi les formats explorés.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from archipelle.core import system
from archipelle.core.outcome import AppError, Err, Ok, Outcome
from archipelle.extraction.formats import OPENABLE_EXTENSIONS, extension_of
from archipelle.tools.paths import Workspace, to_absolute

_log = logging.getLogger(__name__)

type Opener = Callable[[Path], None]


def open_source(
    workdir: str | None,
    rel_path: str,
    *,
    verified: bool,
    opener: Opener = system.open_with_default_app,
) -> Outcome[None]:
    if not verified or not workdir:
        return Err("errors.source.not_verified", {"path": rel_path})
    if extension_of(rel_path) not in OPENABLE_EXTENSIONS:
        return Err("errors.source.forbidden_type", {"path": rel_path})
    try:
        workspace = Workspace.open(workdir)
        target = to_absolute(workspace, rel_path, use_scope=False)
    except AppError as error:
        return Err.from_error(error)
    if extension_of(target.name) not in OPENABLE_EXTENSIONS:  # lien renommé, par exemple
        return Err("errors.source.forbidden_type", {"path": rel_path})
    if not target.is_file():
        return Err("errors.path.not_found", {"path": rel_path})
    try:
        opener(target)
    except OSError:
        _log.exception("Ouverture d'une source impossible")
        return Err("errors.source.open_failed", {"path": rel_path})
    return Ok(None)
