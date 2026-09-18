"""Chemins échangés avec le modèle et validation d'accès (CDC §4, §9).

Toutes les conversions entre chemins relatifs (côté modèle et interface) et chemins
absolus (côté code) passent par ce module, avec la validation d'accès. Il est réutilisé
tel quel par l'ouverture des sources ; aucune autre validation n'existe ailleurs.

Format côté modèle : relatif au dossier de travail, séparateur ``/``, Unicode NFC.
Le dossier de travail lui-même s'écrit ``.``.
"""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path, PurePath, PureWindowsPath

from archipelle.core import system
from archipelle.core.outcome import AppError

ROOT_REL = "."
# Seuil à partir duquel les chemins Windows reçoivent le préfixe étendu « \\?\ ».
# 248 et non 260 : la création de dossiers est limitée à MAX_PATH − 12.
_WINDOWS_LONG_PATH = 248
_EXTENDED_PREFIX = "\\\\?\\"

# Modifiable par les tests pour simuler un système insensible à la casse.
CASE_INSENSITIVE: bool = system.CASE_INSENSITIVE_FS


class PathRefused(AppError):
    """Chemin refusé. ``key`` précise le motif (absolu, remontée, hors dossier…)."""


class WorkspaceError(AppError):
    """Dossier de travail inutilisable."""


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def same_path_key(path: Path | str) -> str:
    """Clé de comparaison d'un chemin : NFC, et casse repliée sous Windows et macOS."""
    key = nfc(str(path))
    return key.casefold() if CASE_INSENSITIVE else key


def _name_key(name: str) -> str:
    key = nfc(name)
    return key.casefold() if CASE_INSENSITIVE else key


def is_within(child: Path, parent: Path) -> bool:
    """Vrai si ``child`` est ``parent`` ou se trouve dessous (chemins déjà résolus)."""
    child_parts = PurePath(same_path_key(child)).parts
    parent_parts = PurePath(same_path_key(parent)).parts
    return child_parts[: len(parent_parts)] == parent_parts


def _resolve(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:  # boucle de liens, chemin invalide
        raise PathRefused("errors.path.invalid", path=path.name) from exc


@dataclass(frozen=True)
class Workspace:
    """Dossier de travail résolu et éventuelle restriction « parcourir » (CDC §6)."""

    root: Path
    scope: Path

    @classmethod
    def open(cls, root: str | Path, scope_rel: str | None = None) -> Workspace:
        raw = Path(root).expanduser()
        if not raw.is_absolute():
            raise WorkspaceError("errors.workspace.not_absolute")
        try:
            resolved = raw.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise WorkspaceError("errors.workspace.not_found", path=str(raw)) from exc
        if not resolved.is_dir():
            raise WorkspaceError("errors.workspace.not_a_directory", path=str(raw))
        workspace = cls(root=resolved, scope=resolved)
        return workspace.with_scope(scope_rel)

    def with_scope(self, scope_rel: str | None) -> Workspace:
        """Copie avec une restriction (chemin relatif au dossier de travail) ou sans."""
        if scope_rel is None or split_relative(scope_rel) == []:
            return replace(self, scope=self.root)
        scope = to_absolute(replace(self, scope=self.root), scope_rel)
        if not scope.is_dir():
            raise PathRefused("errors.path.not_a_directory", path=scope_rel)
        return replace(self, scope=scope)

    @property
    def restricted(self) -> bool:
        return same_path_key(self.scope) != same_path_key(self.root)

    @property
    def scope_rel(self) -> str | None:
        return to_relative(self, self.scope) if self.restricted else None


def split_relative(rel: str) -> list[str]:
    """Découpe et contrôle un chemin relatif reçu du modèle ou de l'interface."""
    if not isinstance(rel, str):  # pyright: ignore[reportUnnecessaryIsInstance] — valeur venue du JSON
        raise PathRefused("errors.path.invalid", path=str(rel))
    if "\x00" in rel:
        raise PathRefused("errors.path.invalid", path=rel.replace("\x00", ""))
    text = nfc(rel.strip()).replace("\\", "/")
    windows_view = PureWindowsPath(text)
    if text.startswith("/") or windows_view.drive or windows_view.anchor:
        raise PathRefused("errors.path.absolute", path=rel)
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise PathRefused("errors.path.parent", path=rel)
    if system.IS_WINDOWS and any(PureWindowsPath(part).drive for part in parts):
        raise PathRefused("errors.path.invalid", path=rel)
    return parts


def _join_existing(root: Path, parts: list[str]) -> Path:
    """Joint ``parts`` à ``root`` en retrouvant les noms stockés sous une autre forme
    Unicode (NFD) ou une autre casse sur les systèmes insensibles à la casse."""
    current = root
    for index, part in enumerate(parts):
        candidate = current / part
        if os.path.lexists(candidate):
            current = candidate
            continue
        wanted = _name_key(part)
        match: str | None = None
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if _name_key(entry.name) == wanted:
                        match = entry.name
                        break
        except OSError:
            match = None
        if match is None:
            return current.joinpath(*parts[index:])
        current = current / match
    return current


def to_absolute(workspace: Workspace, rel: str, *, use_scope: bool = True) -> Path:
    """Chemin absolu résolu d'un chemin relatif, après validation d'accès.

    ``use_scope=False`` ignore la restriction « parcourir » : c'est le cas de
    l'ouverture des sources, validée par rapport au dossier de travail seul (CDC §9).
    L'existence du fichier n'est pas vérifiée ici.
    """
    parts = split_relative(rel)
    resolved = _resolve(_join_existing(workspace.root, parts))
    if not is_within(resolved, workspace.root):
        raise PathRefused("errors.path.outside_workspace", path=rel)
    if use_scope and not is_within(resolved, workspace.scope):
        raise PathRefused(
            "errors.path.outside_scope", path=rel, scope=workspace.scope_rel or ROOT_REL
        )
    return resolved


def to_relative(workspace: Workspace, path: Path) -> str:
    """Chemin relatif (format modèle) d'un chemin situé sous le dossier de travail.

    ``path`` doit être exprimé à partir de la racine résolue (chemins issus du parcours
    du dossier, ou de ``to_absolute``).
    """
    if not is_within(path, workspace.root):
        raise PathRefused("errors.path.outside_workspace", path=path.name)
    rel_parts = path.parts[len(workspace.root.parts) :]
    if not rel_parts:
        return ROOT_REL
    return "/".join(nfc(part) for part in rel_parts)


def extended_windows_path(path: str) -> str:
    """Forme « \\\\?\\ » d'un chemin Windows long (fonction pure, testable partout)."""
    if len(path) < _WINDOWS_LONG_PATH or path.startswith(_EXTENDED_PREFIX):
        return path
    if path.startswith("\\\\"):  # chemin réseau UNC : \\serveur\partage\...
        return _EXTENDED_PREFIX + "UNC\\" + path[2:]
    return _EXTENDED_PREFIX + path


def io_path(path: Path) -> str:
    """Chemin à passer aux fonctions d'entrée-sortie (gère les chemins longs Windows)."""
    text = str(path)
    return extended_windows_path(text) if system.IS_WINDOWS else text
