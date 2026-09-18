"""Relevé d'arborescence (CDC §5) et parcours de dossiers partagé par les outils.

Aucun contenu de fichier n'est lu : seulement les noms, types, tailles et dates.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import stat
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

from archipelle.core.i18n import format_number, t
from archipelle.extraction.formats import extension_of, is_explored
from archipelle.persistence.settings_store import ScanExclusions
from archipelle.tools.context import Stop
from archipelle.tools.paths import Workspace, is_within, nfc, to_relative

_HIDDEN_ATTRIBUTES = 0x2 | 0x4  # FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM (Windows)


@dataclass(frozen=True)
class Entry:
    rel: str  # chemin relatif au dossier de travail (format modèle)
    name: str
    is_dir: bool
    size: int
    mtime: float
    depth: int  # 1 pour les enfants directs du dossier parcouru
    ext: str

    @property
    def explored(self) -> bool:
        return not self.is_dir and is_explored(self.name)

    @property
    def modified(self) -> dt.date:
        return dt.datetime.fromtimestamp(self.mtime).date()


class ExclusionMatcher:
    def __init__(self, exclusions: ScanExclusions) -> None:
        self.dir_names = {name.casefold() for name in exclusions.dir_names}
        self.patterns = [pattern.casefold() for pattern in exclusions.file_patterns]
        self.hide_dotfiles = exclusions.hide_dotfiles

    def excluded(self, name: str, is_dir: bool, attributes: int) -> bool:
        if self.hide_dotfiles and name.startswith("."):
            return True
        if attributes & _HIDDEN_ATTRIBUTES:
            return True
        folded = name.casefold()
        if is_dir and folded in self.dir_names:
            return True
        return any(fnmatchcase(folded, pattern) for pattern in self.patterns)


@dataclass
class WalkStats:
    visited: int = 0
    unreadable_dirs: int = 0
    capped: bool = False  # plafond d'entrées ou échéance atteints
    stopped: bool = False


def iter_entries(
    workspace: Workspace,
    start: Path,
    exclusions: ScanExclusions,
    *,
    max_depth: int | None = None,
    stop: Stop | None = None,
    max_entries: int = 200_000,
    stats: WalkStats | None = None,
) -> Iterator[Entry]:
    """Parcours en profondeur : dans chaque dossier, fichiers puis sous-dossiers, par ordre
    alphabétique ; chaque sous-dossier est immédiatement suivi de son contenu.

    Les liens symboliques et jonctions sont montrés s'ils pointent dans le dossier de
    travail, mais jamais suivis (ni boucle, ni double comptage).
    """
    matcher = ExclusionMatcher(exclusions)
    walk_stats = stats if stats is not None else WalkStats()

    def count() -> bool:
        walk_stats.visited += 1
        if walk_stats.visited > max_entries:
            walk_stats.capped = True
            return False
        return True

    def walk(directory: Path, depth: int) -> Iterator[Entry]:
        if stop is not None and stop.reason() is not None:
            walk_stats.stopped = True
            return
        try:
            with os.scandir(directory) as iterator:
                raw_entries = list(iterator)
        except OSError:
            walk_stats.unreadable_dirs += 1
            return
        files: list[Entry] = []
        dirs: list[tuple[Entry, Path, bool]] = []
        for raw in raw_entries:
            made = _make_entry(workspace, raw, depth, matcher)
            if made is None:
                continue
            item, followable = made
            if item.is_dir:
                dirs.append((item, Path(raw.path), followable))
            else:
                files.append(item)
        files.sort(key=lambda e: e.name.casefold())
        dirs.sort(key=lambda d: d[0].name.casefold())
        for item in files:
            if not count():
                return
            yield item
        for item, path, followable in dirs:
            if walk_stats.capped or walk_stats.stopped or not count():
                return
            yield item
            if followable and (max_depth is None or depth < max_depth):
                yield from walk(path, depth + 1)

    yield from walk(start, 1)


def _make_entry(
    workspace: Workspace, raw: os.DirEntry[str], depth: int, matcher: ExclusionMatcher
) -> tuple[Entry, bool] | None:
    try:
        link = raw.is_symlink() or raw.is_junction()
        if link:
            target = Path(raw.path).resolve(strict=True)
            if not is_within(target, workspace.root):
                return None
            info = target.stat()
        else:
            info = raw.stat(follow_symlinks=False)
    except (OSError, RuntimeError):
        return None
    is_dir = stat.S_ISDIR(info.st_mode)
    if not is_dir and not stat.S_ISREG(info.st_mode):
        return None
    attributes = int(getattr(info, "st_file_attributes", 0))
    if matcher.excluded(raw.name, is_dir, attributes):
        return None
    try:
        rel = to_relative(workspace, Path(raw.path))
    except Exception:
        return None
    entry = Entry(
        rel=rel,
        name=nfc(raw.name),
        is_dir=is_dir,
        size=0 if is_dir else info.st_size,
        mtime=info.st_mtime,
        depth=depth,
        ext="" if is_dir else extension_of(raw.name),
    )
    return entry, not link


# --- formatage commun ----------------------------------------------------------------


def format_size(size: int) -> str:
    if size < 1024:
        return t("units.bytes", value=size)
    value = float(size)
    for key in ("units.kb", "units.mb", "units.gb"):
        value /= 1024
        if value < 1024 or key == "units.gb":
            text = f"{value:.1f}".replace(".", ",").removesuffix(",0")
            return t(key, value=text)
    raise AssertionError("inatteignable")


def describe_file(entry: Entry) -> str:
    details = [format_size(entry.size), entry.modified.isoformat()]
    if not entry.explored:
        details.append(t("tree.not_explored"))
    return ", ".join(details)


# --- relevé et rendu -----------------------------------------------------------------


@dataclass
class TreeSnapshot:
    entries: list[Entry]
    capped: bool
    unreadable_dirs: int
    digest: str
    file_count: int = 0
    dir_count: int = 0
    subtree_files: dict[str, int] = field(default_factory=dict[str, int])
    subtree_dirs: dict[str, int] = field(default_factory=dict[str, int])
    subtree_types: dict[str, Counter[str]] = field(default_factory=dict[str, Counter[str]])


def scan(
    workspace: Workspace, exclusions: ScanExclusions, stop: Stop | None = None,
    max_entries: int = 200_000,
) -> TreeSnapshot:  # fmt: skip
    stats = WalkStats()
    entries = list(
        iter_entries(
            workspace, workspace.root, exclusions, stop=stop, max_entries=max_entries, stats=stats
        )
    )
    digest = hashlib.sha256()
    snapshot = TreeSnapshot(
        entries=entries,
        capped=stats.capped or stats.stopped,
        unreadable_dirs=stats.unreadable_dirs,
        digest="",
    )
    for entry in entries:
        digest.update(f"{entry.rel}\0{entry.is_dir}\0{entry.size}\0{entry.mtime}\n".encode())
        if entry.is_dir:
            snapshot.dir_count += 1
        else:
            snapshot.file_count += 1
        parts = entry.rel.split("/")
        for index in range(1, len(parts)):
            ancestor = "/".join(parts[:index])
            if entry.is_dir:
                snapshot.subtree_dirs[ancestor] = snapshot.subtree_dirs.get(ancestor, 0) + 1
            else:
                snapshot.subtree_files[ancestor] = snapshot.subtree_files.get(ancestor, 0) + 1
                snapshot.subtree_types.setdefault(ancestor, Counter())[entry.ext or "?"] += 1
    snapshot.digest = digest.hexdigest()
    return snapshot


@dataclass(frozen=True)
class RenderedTree:
    text: str
    level: str  # "full", "depth:N", "summary"
    truncated: bool


def _line(entry: Entry) -> str:
    indent = "  " * (entry.depth - 1)
    if entry.is_dir:
        return f"{indent}{entry.name}/"
    return f"{indent}{entry.name} ({describe_file(entry)})"


def _hidden_content(snapshot: TreeSnapshot, rel: str) -> str:
    files = snapshot.subtree_files.get(rel, 0)
    dirs = snapshot.subtree_dirs.get(rel, 0)
    if not files and not dirs:
        return ""
    return " " + t("tree.hidden_content", files=format_number(files), dirs=format_number(dirs))


def _types_summary(counter: Counter[str]) -> str:
    return ", ".join(f"{ext} {format_number(count)}" for ext, count in counter.most_common(6))


def render(snapshot: TreeSnapshot, budget_chars: int) -> RenderedTree:
    header = t(
        "tree.header",
        files=format_number(snapshot.file_count),
        dirs=format_number(snapshot.dir_count),
    )
    if not snapshot.entries:
        return RenderedTree(f"{header}\n{t('tree.empty')}", "full", False)
    footer_parts: list[str] = []
    if snapshot.capped:
        footer_parts.append(t("tree.scan_capped"))
    if snapshot.unreadable_dirs:
        footer_parts.append(t("tree.unreadable", count=snapshot.unreadable_dirs))
    base_footer = "\n".join(footer_parts)

    def assemble(lines: list[str], notice: str) -> str:
        blocks = [header, *lines]
        if notice:
            blocks.append(notice)
        if base_footer:
            blocks.append(base_footer)
        return "\n".join(blocks)

    full = assemble([_line(e) for e in snapshot.entries], "")
    if len(full) <= budget_chars:
        return RenderedTree(full, "full", snapshot.capped)

    max_depth = max(e.depth for e in snapshot.entries)
    for depth in range(max_depth - 1, 0, -1):
        lines: list[str] = []
        for entry in snapshot.entries:
            if entry.depth > depth:
                continue
            line = _line(entry)
            if entry.is_dir and entry.depth == depth:
                line += _hidden_content(snapshot, entry.rel)
            lines.append(line)
        text = assemble(lines, t("tree.truncated_depth", depth=depth))
        if len(text) <= budget_chars:
            return RenderedTree(text, f"depth:{depth}", True)

    # Résumé : dossiers de premier niveau et nombre de fichiers par type.
    notice = t("tree.truncated_summary")
    lines = []
    top_files = Counter(e.ext or "?" for e in snapshot.entries if e.depth == 1 and not e.is_dir)
    if top_files:
        count = sum(top_files.values())
        lines.append(
            t("tree.summary_root", count=format_number(count), types=_types_summary(top_files))
        )
    top_dirs = [e for e in snapshot.entries if e.depth == 1 and e.is_dir]
    for index, entry in enumerate(top_dirs):
        count = snapshot.subtree_files.get(entry.rel, 0)
        types = snapshot.subtree_types.get(entry.rel, Counter[str]())
        line = t(
            "tree.summary_dir",
            name=entry.name,
            count=format_number(count),
            types=_types_summary(types),
        )
        candidate = assemble([*lines, line], notice)
        if len(candidate) > budget_chars:
            rest = len(top_dirs) - index
            lines.append(t("tree.summary_more", count=format_number(rest)))
            break
        lines.append(line)
    text = assemble(lines, notice)
    if len(text) > budget_chars:
        text = text[: max(0, budget_chars - 1)] + "…"
    return RenderedTree(text, "summary", True)
