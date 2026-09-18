"""Outil ``search_files`` : recherche dans les noms de fichiers et de dossiers (CDC §4)."""

from __future__ import annotations

from archipelle.core.i18n import format_number, t
from archipelle.core.textsearch import contains_any, prepare_keywords
from archipelle.tools.common import clamp, count_notice, parse_extensions, resolve_directory
from archipelle.tools.context import ToolContext, ToolOutcome
from archipelle.tools.tree import WalkStats, describe_file, iter_entries


def search_files(
    ctx: ToolContext,
    keywords: list[str],
    path: str | None = None,
    extensions: list[str] | None = None,
    max_results: int | None = None,
) -> ToolOutcome:
    needles = prepare_keywords(keywords)
    start, rel = resolve_directory(ctx, path)
    wanted = parse_extensions(extensions)
    limit = clamp(max_results, ctx.limits.search_files_max, ctx.limits.search_files_max)
    stats = WalkStats()
    stop = ctx.stop(ctx.limits.tool_timeout_s)

    lines: list[str] = []
    total = 0
    for entry in iter_entries(
        ctx.workspace,
        start,
        ctx.exclusions,
        stop=stop,
        max_entries=ctx.limits.walk_max_entries,
        stats=stats,
    ):
        if wanted is not None and (entry.is_dir or entry.ext not in wanted):
            continue
        if not contains_any(entry.name, needles):
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

    parts = [t("tools.search_files.header", keywords=_quoted(keywords), path=rel)]
    if total == 0:
        parts.append(t("tools.search_files.none"))
    else:
        parts.append(count_notice(total, len(lines), "paths"))
        parts.extend(lines)
    if stats.capped or stats.stopped:
        parts.append(t("tools.list.walk_incomplete", count=format_number(stats.visited)))
    return ToolOutcome(ok=True, content="\n".join(parts))


def _quoted(keywords: list[str]) -> str:
    return ", ".join(f"« {k.strip()} »" for k in keywords if k.strip())
