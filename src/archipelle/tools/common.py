"""Aides partagées par les outils : paramètres, journal, messages d'erreur."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

from archipelle.core.i18n import format_number, t
from archipelle.extraction.formats import expand_extensions
from archipelle.extraction.types import ExtractionError
from archipelle.tools.context import IgnoredEntry, IgnoredReason, ToolContext, ToolInputError
from archipelle.tools.paths import ROOT_REL, PathRefused, to_absolute, to_relative
from archipelle.tools.tree import Entry


def resolve_directory(ctx: ToolContext, path: str | None) -> tuple[Path, str]:
    """Dossier à parcourir. Sans chemin : le dossier de travail, ou le sous-dossier choisi."""
    if path is None or not path.strip():
        target = ctx.workspace.scope
    else:
        target = to_absolute(ctx.workspace, path)
    if not target.exists():
        raise PathRefused("errors.path.not_found", path=path or ROOT_REL)
    if not target.is_dir():
        raise PathRefused("errors.path.not_a_directory", path=path or ROOT_REL)
    return target, to_relative(ctx.workspace, target)


def parse_extensions(values: list[str] | None) -> set[str] | None:
    """Extensions demandées, groupes développés (« code », « documents », « images »)."""
    if not values:
        return None
    return expand_extensions(values) or None


def parse_date(value: str | None, parameter: str) -> dt.date | None:
    if value is None or not value.strip():
        return None
    try:
        return dt.date.fromisoformat(value.strip()[:10])
    except ValueError as exc:
        raise ToolInputError("tools.errors.bad_date", parameter=parameter, value=value) from exc


@dataclass(frozen=True)
class FileFilter:
    extensions: set[str] | None = None
    after: dt.date | None = None
    before: dt.date | None = None

    @property
    def active(self) -> bool:
        return bool(self.extensions or self.after or self.before)

    def matches(self, entry: Entry) -> bool:
        if entry.is_dir:
            return False
        if self.extensions is not None and entry.ext not in self.extensions:
            return False
        modified = entry.modified
        if self.after is not None and modified < self.after:
            return False
        return not (self.before is not None and modified > self.before)


def build_filter(
    extensions: list[str] | None, modified_after: str | None, modified_before: str | None
) -> FileFilter:
    after = parse_date(modified_after, "modified_after")
    before = parse_date(modified_before, "modified_before")
    if after and before and after > before:
        raise ToolInputError("tools.errors.date_range")
    return FileFilter(parse_extensions(extensions), after, before)


def clamp(value: int | None, default: int, maximum: int) -> int:
    if value is None:
        return default
    return max(1, min(value, maximum))


@dataclass
class Journal:
    """Journal des fichiers ignorés d'un appel, plafonné (CDC §3)."""

    limit: int
    entries: list[IgnoredEntry] = field(default_factory=list[IgnoredEntry])
    overflow: int = 0

    def add(self, rel: str, reason: IgnoredReason, detail: str = "") -> None:
        if len(self.entries) < self.limit:
            self.entries.append(IgnoredEntry(rel, reason, detail))
        else:
            self.overflow += 1


def extraction_error_text(error: ExtractionError, rel: str) -> str:
    return t(f"tools.errors.extraction.{error.code.value}", path=rel)


def count_notice(total: int, shown: int, kind: str) -> str:
    """« Entrées : 1 240 au total, 500 affichées » — toujours explicite (CDC §4).

    ``kind`` vaut ``entries`` ou ``paths``.
    """
    variant = "all" if shown >= total else "partial"
    return t(
        f"tools.count.{kind}_{variant}", total=format_number(total), shown=format_number(shown)
    )
