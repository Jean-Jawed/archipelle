"""Outil ``search_fulltext`` : recherche plafonnée dans le contenu des fichiers (CDC §4bis)."""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass, field
from pathlib import Path

from archipelle.core.i18n import format_number, t
from archipelle.core.outcome import Cancelled
from archipelle.core.textsearch import (
    contains_any,
    find_matches,
    normalize,
    prepare_keywords,
    snippet,
)
from archipelle.extraction import tasks
from archipelle.extraction.formats import FormatKind, kind_of
from archipelle.extraction.types import DocumentNote, ExtractionError
from archipelle.tools.common import (
    Journal,
    build_filter,
    clamp,
    extraction_error_text,
    resolve_directory,
)
from archipelle.tools.context import (
    ConsultedFile,
    IgnoredReason,
    Stop,
    ToolContext,
    ToolOutcome,
)
from archipelle.tools.paths import PathRefused, io_path, to_absolute
from archipelle.tools.process_pool import ExtractionDeadline
from archipelle.tools.tree import Entry, WalkStats, iter_entries

_MAX_ERRORS_LISTED = 5


@dataclass
class _Hit:
    page: int
    offset: int
    snippet: str


@dataclass
class _FileResult:
    entry: Entry
    order: int
    total: int = 0
    hits: list[_Hit] = field(default_factory=list[_Hit])
    searched: bool = False
    from_cache: bool = False
    is_text: bool = False
    pending_pages: int = 0
    ocr_unavailable: bool = False
    partial: bool = False
    no_text: bool = False
    error: ExtractionError | None = None


def _search_text_file(
    ctx: ToolContext, result: _FileResult, abs_path: Path, needles: list[str], stop: Stop
) -> _FileResult:
    result.is_text = True
    try:
        found = ctx.pool.run(
            tasks.search_text_file,
            io_path(abs_path),
            needles,
            ctx.limits.fulltext_snippets,
            ctx.limits.snippet_chars,
            stop=stop,
        )
    except ExtractionDeadline:
        return result
    except ExtractionError as exc:
        result.error = exc
        return result
    result.searched = True
    result.total = found.total
    result.hits = [_Hit(1, hit.offset, hit.snippet) for hit in found.hits]
    return result


def _search_document(
    ctx: ToolContext, result: _FileResult, abs_path: Path, needles: list[str], stop: Stop
) -> _FileResult:
    try:
        load = ctx.documents.load(
            abs_path,
            want_ocr=ctx.explore,
            stop=stop,
            progress=ctx.progress,
            rel_path=result.entry.rel,
        )
    except ExtractionError as exc:
        result.error = exc
        return result
    result.from_cache = load.from_cache
    result.ocr_unavailable = load.ocr_unavailable
    result.pending_pages = len(load.pending_ocr)
    result.partial = not load.complete and bool(load.pages)
    result.no_text = load.has_note(DocumentNote.NO_TEXT)
    for number in sorted(load.pages):
        text = load.pages[number].text
        if not text:
            continue
        matches = find_matches(normalize(text), needles)
        result.total += len(matches)
        for match in matches:
            if len(result.hits) >= ctx.limits.fulltext_snippets:
                break
            result.hits.append(
                _Hit(number, match.offset, snippet(text, match, ctx.limits.snippet_chars))
            )
    result.searched = bool(load.pages) or load.complete
    return result


def _examine(
    ctx: ToolContext, result: _FileResult, abs_path: Path, needles: list[str], stop: Stop
) -> _FileResult:
    try:
        if kind_of(abs_path) is FormatKind.TEXT:
            return _search_text_file(ctx, result, abs_path, needles, stop)
        return _search_document(ctx, result, abs_path, needles, stop)
    except Cancelled:
        return result


def search_fulltext(
    ctx: ToolContext,
    keywords: list[str],
    path: str | None = None,
    extensions: list[str] | None = None,
    modified_after: str | None = None,
    modified_before: str | None = None,
    max_results: int | None = None,
) -> ToolOutcome:
    needles = prepare_keywords(keywords)
    start, rel = resolve_directory(ctx, path)
    file_filter = build_filter(extensions, modified_after, modified_before)
    shown_limit = clamp(max_results, ctx.limits.fulltext_files, ctx.limits.fulltext_files)
    stop = ctx.stop(ctx.limits.fulltext_seconds)
    journal = Journal(ctx.limits.journal_max_entries)

    # 1. Candidats, puis ordre de parcours : nom correspondant d'abord, puis les plus récents.
    walk_stats = WalkStats()
    candidates: list[Entry] = []
    unsupported = 0
    for entry in iter_entries(
        ctx.workspace,
        start,
        ctx.exclusions,
        stop=stop,
        max_entries=ctx.limits.walk_max_entries,
        stats=walk_stats,
    ):
        if entry.is_dir or (file_filter.active and not file_filter.matches(entry)):
            continue
        if not entry.explored:
            unsupported += 1
            journal.add(entry.rel, IgnoredReason.UNSUPPORTED_FORMAT, entry.ext)
            continue
        candidates.append(entry)
    stop.check_cancel()
    candidates.sort(key=lambda e: (not contains_any(e.name, needles), -e.mtime))

    # 2. Fouille parallèle, plafonnée.
    results: list[_FileResult] = []
    new_extractions = 0
    cap_reached = False
    not_started: list[Entry] = []
    running: set[concurrent.futures.Future[_FileResult]] = set()
    window = max(1, ctx.pool.max_workers) * 2
    pending = iter(enumerate(candidates))
    exhausted = False
    total_candidates = len(candidates)

    while True:
        while not exhausted and len(running) < window and stop.reason() is None:
            item = next(pending, None)
            if item is None:
                exhausted = True
                break
            order, entry = item
            result = _FileResult(entry=entry, order=order)
            try:
                abs_path = to_absolute(ctx.workspace, entry.rel)
                if kind_of(abs_path) is not FormatKind.TEXT and ctx.documents.needs_extraction(
                    abs_path, want_ocr=ctx.explore
                ):
                    if new_extractions >= ctx.limits.fulltext_new_files:
                        cap_reached = True
                        not_started.append(entry)
                        continue
                    new_extractions += 1
            except (PathRefused, OSError):
                continue
            except ExtractionError as exc:
                result.error = exc
                results.append(result)
                continue
            running.add(
                ctx.documents.threads.submit(_examine, ctx, result, abs_path, needles, stop)
            )
        if not running:
            break
        done, running = concurrent.futures.wait(
            running, timeout=0.1, return_when=concurrent.futures.FIRST_COMPLETED
        )
        for future in done:
            results.append(future.result())
        if done:
            ctx.progress(
                "progress.fulltext",
                {"done": len(results), "total": total_candidates},
            )

    stop.check_cancel()
    not_started.extend(entry for _, entry in pending)
    stop_reason = stop.reason()
    return _render(
        ctx,
        keywords=keywords,
        rel=rel,
        results=results,
        total_candidates=total_candidates,
        unsupported=unsupported,
        not_started=not_started,
        cap_reached=cap_reached,
        deadline_reached=stop_reason == "deadline" or walk_stats.stopped,
        walk_capped=walk_stats.capped,
        shown_limit=shown_limit,
        journal=journal,
    )


def _render(
    ctx: ToolContext,
    *,
    keywords: list[str],
    rel: str,
    results: list[_FileResult],
    total_candidates: int,
    unsupported: int,
    not_started: list[Entry],
    cap_reached: bool,
    deadline_reached: bool,
    walk_capped: bool,
    shown_limit: int,
    journal: Journal,
) -> ToolOutcome:
    results.sort(key=lambda r: r.order)
    matching = [r for r in results if r.total > 0]
    shown = matching[:shown_limit]
    occurrences = sum(r.total for r in matching)
    quoted = ", ".join(f"« {k.strip()} »" for k in keywords if k.strip())

    lines = [t("tools.fulltext.header", keywords=quoted, path=rel)]
    if not matching:
        lines.append(t("tools.fulltext.none"))
    else:
        key = "tools.fulltext.found_all" if len(shown) == len(matching) else "tools.fulltext.found"
        lines.append(
            t(
                key,
                files=format_number(len(matching)),
                occurrences=format_number(occurrences),
                shown=format_number(len(shown)),
            )
        )
    consulted: list[ConsultedFile] = []
    for result in shown:
        lines.append(
            t("tools.fulltext.file", path=result.entry.rel, count=format_number(result.total))
        )
        for hit in result.hits:
            lines.append(
                t("tools.fulltext.hit", page=hit.page, offset=hit.offset, snippet=hit.snippet)
            )
        if result.total > len(result.hits):
            lines.append(t("tools.fulltext.more_hits", count=result.total - len(result.hits)))
        if result.partial:
            lines.append(t("tools.fulltext.file_partial"))
        consulted.append(ConsultedFile(result.entry.rel, result.from_cache and not result.is_text))

    # Bilan de couverture (toujours présent).
    searched = [r for r in results if r.searched]
    from_cache = sum(1 for r in searched if r.from_cache and not r.is_text)
    scans = [r for r in results if r.pending_pages and not r.ocr_unavailable]
    no_ocr = [r for r in results if r.ocr_unavailable]
    errors = [r for r in results if r.error is not None]
    partial = [r for r in results if r.partial]
    unfinished = [r for r in results if not r.searched and r.error is None]

    lines.append("")
    lines.append(
        t(
            "tools.fulltext.coverage",
            searched=format_number(len(searched)),
            candidates=format_number(total_candidates),
            cached=format_number(from_cache),
        )
    )
    if scans and not ctx.explore:
        lines.append(t("tools.fulltext.scans_quick", count=format_number(len(scans))))
    if no_ocr:
        lines.append(t("tools.fulltext.ocr_unavailable", count=format_number(len(no_ocr))))
    if unsupported:
        lines.append(t("tools.fulltext.unsupported", count=format_number(unsupported)))
    if partial:
        lines.append(t("tools.fulltext.partial", count=format_number(len(partial))))
    if errors:
        lines.append(t("tools.fulltext.errors", count=format_number(len(errors))))
        for result in errors[:_MAX_ERRORS_LISTED]:
            assert result.error is not None
            lines.append(f"- {extraction_error_text(result.error, result.entry.rel)}")
    skipped = len(not_started) + len(unfinished)
    if cap_reached:
        lines.append(
            t(
                "tools.fulltext.stopped_cap",
                limit=format_number(ctx.limits.fulltext_new_files),
                count=format_number(skipped),
            )
        )
    elif deadline_reached:
        lines.append(
            t(
                "tools.fulltext.stopped_time",
                seconds=format_number(int(ctx.limits.fulltext_seconds)),
                count=format_number(skipped),
            )
        )
    if walk_capped:
        lines.append(t("tools.fulltext.walk_capped"))
    if cap_reached or deadline_reached or partial:
        lines.append(t("tools.fulltext.resume_hint"))

    # Journal des fichiers ignorés.
    for result in results:
        rel_path = result.entry.rel
        if result.error is not None:
            journal.add(rel_path, IgnoredReason.READ_ERROR, result.error.code.value)
        elif result.ocr_unavailable:
            journal.add(rel_path, IgnoredReason.OCR_UNAVAILABLE)
        elif result.pending_pages and not ctx.explore:
            journal.add(rel_path, IgnoredReason.SCAN_NOT_PROCESSED, str(result.pending_pages))
        elif result.no_text and kind_of(rel_path) is FormatKind.IMAGE:
            journal.add(rel_path, IgnoredReason.NO_OCR_TEXT)
        if result.partial or (not result.searched and result.error is None):
            journal.add(rel_path, IgnoredReason.SEARCH_CAP)
    for entry in not_started:
        journal.add(entry.rel, IgnoredReason.SEARCH_CAP)

    return ToolOutcome(
        ok=True,
        content="\n".join(lines),
        consulted=consulted,
        ignored=journal.entries,
        ignored_overflow=journal.overflow,
    )
