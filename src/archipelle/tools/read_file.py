"""Outil ``read_file`` : lecture d'un fichier, avec reprise à une position (CDC §4, §7)."""

from __future__ import annotations

from pathlib import Path

from archipelle.core.i18n import format_number, t
from archipelle.extraction import tasks
from archipelle.extraction.formats import FormatKind, kind_of
from archipelle.extraction.types import DocumentNote, ExtractionError, ExtractionMethod, NoteEntry
from archipelle.tools.common import Journal, clamp, extraction_error_text
from archipelle.tools.context import (
    ConsultedFile,
    IgnoredReason,
    Stop,
    ToolContext,
    ToolInputError,
    ToolOutcome,
)
from archipelle.tools.documents import DocumentLoad
from archipelle.tools.paths import PathRefused, io_path, to_absolute
from archipelle.tools.process_pool import ExtractionDeadline


def read_file(
    ctx: ToolContext,
    path: str,
    page: int | None = None,
    offset: int | None = None,
    max_chars: int | None = None,
) -> ToolOutcome:
    abs_path = to_absolute(ctx.workspace, path)
    if not abs_path.exists():
        raise PathRefused("errors.path.not_found", path=path)
    if abs_path.is_dir():
        raise ToolInputError("tools.read.is_directory", path=path)
    rel = path.strip().replace("\\", "/").strip("/")
    kind = kind_of(abs_path)
    journal = Journal(ctx.limits.journal_max_entries)
    if kind is None:
        journal.add(rel, IgnoredReason.UNSUPPORTED_FORMAT, abs_path.suffix.lstrip(".").lower())
        return ToolOutcome(
            ok=False, content=t("tools.read.unsupported", path=rel), ignored=journal.entries
        )
    if page is not None and page < 1:
        raise ToolInputError("tools.read.bad_page", page=page)
    if offset is not None and offset < 0:
        raise ToolInputError("tools.read.bad_offset", offset=offset)
    budget = clamp(max_chars, ctx.limits.read_max_chars, ctx.limits.read_max_chars)
    stop = ctx.stop(ctx.limits.tool_timeout_s)
    reader = _Reader(ctx, abs_path, rel, budget, stop, journal)
    try:
        if kind is FormatKind.TEXT:
            return reader.read_text(page, offset or 0)
        if kind is FormatKind.PDF:
            return reader.read_pdf(page or 1, offset or 0)
        return reader.read_single(kind, page, offset or 0)
    except ExtractionError as exc:
        journal.add(rel, IgnoredReason.READ_ERROR, exc.code.value)
        return ToolOutcome(
            ok=False, content=extraction_error_text(exc, rel), ignored=journal.entries
        )


def _note_lines(notes: list[NoteEntry]) -> list[str]:
    lines: list[str] = []
    for entry in notes:
        if entry.note is DocumentNote.XLSX_MISSING_VALUES:
            lines.append(t("tools.read.note_xlsx", count=entry.detail))
        elif entry.note is DocumentNote.EML_ATTACHMENTS:
            lines.append(t("tools.read.note_attachments", names=entry.detail))
        elif entry.note is DocumentNote.TRUNCATED:
            lines.append(t("tools.read.note_truncated"))
    return lines


class _Reader:
    def __init__(
        self,
        ctx: ToolContext,
        abs_path: Path,
        rel: str,
        budget: int,
        stop: Stop,
        journal: Journal,
    ) -> None:
        self.ctx = ctx
        self.abs_path = abs_path
        self.rel = rel
        self.budget = budget
        self.stop = stop
        self.journal = journal

    def _outcome(self, lines: list[str], body_chars: int, from_cache: bool) -> ToolOutcome:
        consulted = [ConsultedFile(self.rel, from_cache)] if body_chars > 0 else []
        return ToolOutcome(
            ok=True,
            content="\n".join(lines),
            consulted=consulted,
            ignored=self.journal.entries,
            ignored_overflow=self.journal.overflow,
        )

    def _slice_status(self, start: int, end: int, total: int) -> str:
        if end >= total:
            return t("tools.read.end_of_file")
        return t(
            "tools.read.truncated",
            max=format_number(self.budget),
            page=1,
            offset=end,
        )

    # --- fichiers texte -------------------------------------------------------------

    def read_text(self, page: int | None, offset: int) -> ToolOutcome:
        if page not in (None, 1):
            raise ToolInputError("tools.read.single_page", path=self.rel)
        try:
            piece = self.ctx.pool.run(
                tasks.read_text_slice, io_path(self.abs_path), offset, self.budget, stop=self.stop
            )
        except ExtractionDeadline:
            return ToolOutcome(ok=False, content=t("tools.read.timeout_text", path=self.rel))
        end = piece.offset + len(piece.text)
        lines = [
            t(
                "tools.read.header_text",
                path=self.rel,
                start=format_number(piece.offset),
                end=format_number(end),
                total=format_number(piece.total_chars),
            ),
            piece.text,
            self._slice_status(piece.offset, end, piece.total_chars),
        ]
        return self._outcome(lines, len(piece.text.strip()), from_cache=False)

    # --- documents non paginés ------------------------------------------------------

    def read_single(self, kind: FormatKind, page: int | None, offset: int) -> ToolOutcome:
        if page not in (None, 1):
            raise ToolInputError("tools.read.single_page", path=self.rel)
        load = self.ctx.documents.load(
            self.abs_path,
            want_ocr=self.ctx.explore,
            stop=self.stop,
            progress=self.ctx.progress,
            rel_path=self.rel,
        )
        if load.missing:
            return ToolOutcome(ok=False, content=t("tools.read.timeout_text", path=self.rel))
        page_text = load.pages[1]
        if page_text.method is ExtractionMethod.PENDING:
            return self._image_not_read(load)
        text = page_text.text
        start = min(offset, len(text))
        body = text[start : start + self.budget]
        end = start + len(body)
        key = "tools.read.header_image" if kind is FormatKind.IMAGE else "tools.read.header_text"
        lines = [
            t(
                key,
                path=self.rel,
                start=format_number(start),
                end=format_number(end),
                total=format_number(len(text)),
            ),
            *_note_lines(load.notes),
        ]
        if not text.strip():
            lines.append(t("tools.read.no_text"))
            if kind is FormatKind.IMAGE:
                self.journal.add(self.rel, IgnoredReason.NO_OCR_TEXT)
        else:
            lines.append(body)
            lines.append(self._slice_status(start, end, len(text)))
        return self._outcome(lines, len(body.strip()), load.from_cache)

    def _image_not_read(self, load: DocumentLoad) -> ToolOutcome:
        if load.ocr_unavailable:
            self.journal.add(self.rel, IgnoredReason.OCR_UNAVAILABLE)
            content = t("tools.read.image_no_ocr", path=self.rel)
        else:
            self.journal.add(self.rel, IgnoredReason.SCAN_NOT_PROCESSED, "1")
            content = t("tools.read.image_quick", path=self.rel)
        return ToolOutcome(ok=True, content=content, ignored=self.journal.entries)

    # --- PDF ------------------------------------------------------------------------

    def read_pdf(self, first_page: int, offset: int) -> ToolOutcome:
        batch = self.ctx.limits.pdf_batch_pages
        body: list[str] = []
        used = 0
        body_chars = 0
        pending: list[int] = []
        no_ocr: list[int] = []
        ocr_failed: list[int] = []
        resume: tuple[int, int] | None = None
        interrupted_at: int | None = None
        all_cached = True
        page_count = 0
        current = first_page
        last_read = first_page - 1
        notes: list[NoteEntry] = []

        while True:
            load = self.ctx.documents.load(
                self.abs_path,
                want_ocr=self.ctx.explore,
                stop=self.stop,
                first_page=current,
                max_pages=batch,
                progress=self.ctx.progress,
                rel_path=self.rel,
            )
            all_cached = all_cached and load.from_cache
            page_count = load.page_count
            notes = load.notes
            if page_count and first_page > page_count:
                raise ToolInputError(
                    "tools.read.page_out_of_range", page=first_page, count=page_count
                )
            if not load.requested and load.stop_reason is not None:
                interrupted_at = current
                break
            ocr_failed.extend(load.ocr_failed)
            for number in load.requested:
                if number not in load.pages:
                    interrupted_at = number
                    break
                page_text = load.pages[number]
                start = offset if number == first_page else 0
                text = page_text.text[start:]
                is_pending = page_text.method is ExtractionMethod.PENDING
                if (
                    is_pending
                    and self.ctx.explore
                    and not load.ocr_unavailable
                    and number not in load.ocr_failed
                ):
                    # OCR non terminé (limite de temps) : la lecture reprendra à cette page.
                    interrupted_at = number
                    break
                if is_pending:
                    if load.ocr_unavailable:
                        no_ocr.append(number)
                    elif not self.ctx.explore:
                        pending.append(number)
                    marker = t("tools.read.page_marker_unread", page=number)
                else:
                    marker = t("tools.read.page_marker", page=number)
                remaining = self.budget - used
                if remaining <= 0:
                    resume = (number, start)
                    break
                piece = text[:remaining]
                body.append(marker)
                if piece.strip():
                    body.append(piece)
                used += len(piece)
                if not is_pending:
                    body_chars += len(piece.strip())
                last_read = number
                if len(piece) < len(text):
                    resume = (number, start + len(piece))
                    break
            if resume is not None or interrupted_at is not None:
                break
            if not load.requested or load.requested[-1] >= page_count:
                break
            current = load.requested[-1] + 1

        lines = [
            t("tools.read.header_pdf", path=self.rel, count=format_number(page_count)),
            *_note_lines(notes),
            *body,
        ]
        if interrupted_at is not None:
            lines.append(
                t(
                    "tools.read.pdf_interrupted",
                    first=first_page,
                    last=max(last_read, first_page - 1),
                    count=page_count,
                    next=interrupted_at,
                )
            )
        elif resume is not None:
            lines.append(
                t(
                    "tools.read.truncated",
                    max=format_number(self.budget),
                    page=resume[0],
                    offset=resume[1],
                )
            )
        else:
            lines.append(t("tools.read.end_of_file"))
        if pending:
            lines.append(t("tools.read.pdf_scanned_quick", count=len(pending)))
            self.journal.add(self.rel, IgnoredReason.SCAN_NOT_PROCESSED, str(len(pending)))
        if no_ocr:
            lines.append(t("tools.read.pdf_no_ocr", count=len(no_ocr)))
            self.journal.add(self.rel, IgnoredReason.OCR_UNAVAILABLE, str(len(no_ocr)))
        if ocr_failed:
            pages = ", ".join(str(n) for n in sorted(set(ocr_failed)))
            lines.append(t("tools.read.pdf_ocr_failed", pages=pages))
        return self._outcome(lines, body_chars, all_cached)
