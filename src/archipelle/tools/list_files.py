"""Outil ``list_files`` : contenu d'un dossier (CDC §4)."""

from __future__ import annotations

from archipelle.core.i18n import format_number, t
from archipelle.tools.common import build_filter, clamp, count_notice, resolve_directory
from archipelle.tools.context import ToolContext, ToolOutcome
from archipelle.tools.tree import WalkStats, describe_file, iter_entries

DEFAULT_DEPTH = 1
MAX_DEPTH = 20


def list_files(
    ctx: ToolContext,
    path: str | None = None,
    depth: int | None = None,
    extensions: list[str] | None = None,
    modified_after: str | None = None,
    modified_before: str | None = None,
    max_results: int | None = None,
) -> ToolOutcome:
    start, rel = resolve_directory(ctx, path)
    file_filter = build_filter(extensions, modified_after, modified_before)
    limit = clamp(max_results, ctx.limits.list_max, ctx.limits.list_max)
    levels = clamp(depth, DEFAULT_DEPTH, MAX_DEPTH)
    stats = WalkStats()
    stop = ctx.stop(ctx.limits.tool_timeout_s)

    lines: list[str] = []
    total = 0
    for entry in iter_entries(
        ctx.workspace,
        start,
        ctx.exclusions,
        max_depth=levels,
        stop=stop,
        max_entries=ctx.limits.walk_max_entries,
        stats=stats,
    ):
        if file_filter.active and not file_filter.matches(entry):
            continue
        total += 1
        if len(lines) < limit:
            if entry.is_dir:
                lines.append(t("tools.list.dir_line", path=entry.rel))
            else:
                lines.append(
                    t("tools.list.file_line", path=entry.rel, details=describe_file(entry))
                )
    stop.check_cancel()

    parts = [t("tools.list.header", path=rel, depth=levels)]
    if total == 0:
        parts.append(t("tools.list.empty"))
    else:
        parts.append(count_notice(total, len(lines), "entries"))
        parts.extend(lines)
    if stats.capped or stats.stopped:
        parts.append(t("tools.list.walk_incomplete", count=format_number(stats.visited)))
    if stats.unreadable_dirs:
        parts.append(t("tree.unreadable", count=stats.unreadable_dirs))
    return ToolOutcome(ok=True, content="\n".join(parts))
