"""E-mails .eml et pages .html/.htm, avec la bibliothèque standard (CDC §3)."""

from __future__ import annotations

import email
import re
from email import policy
from email.message import EmailMessage
from html.parser import HTMLParser

from archipelle.core.i18n import t
from archipelle.extraction.errors import os_errors
from archipelle.extraction.text_files import decode_bytes, normalize_newlines
from archipelle.extraction.types import (
    MAX_DOCUMENT_CHARS,
    DocumentNote,
    ExtractionMethod,
    NoteEntry,
    WholeDocument,
)

_SKIPPED_TAGS = {"script", "style", "noscript", "template", "svg", "head"}
_BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article", "header",
    "footer", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "hr", "dd", "dt",
    "title", "nav", "aside", "main", "figure", "figcaption", "form",
}  # fmt: skip
_CELL_TAGS = {"td", "th"}
_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-]+)""", re.I)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self.skip_depth = 0
        self.title: list[str] = []
        self.in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self.in_title = True
        elif tag in _SKIPPED_TAGS:
            self.skip_depth += 1
        if tag in _BLOCK_TAGS:
            self.chunks.append("\n")
        elif tag in _CELL_TAGS:
            self.chunks.append(" | ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        elif tag in _SKIPPED_TAGS and self.skip_depth:
            self.skip_depth -= 1
        if tag in _BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title.append(data)
        elif not self.skip_depth:
            self.chunks.append(data)

    def text(self) -> str:
        lines: list[str] = []
        for raw_line in "".join(self.chunks).split("\n"):
            line = " ".join(raw_line.split()).strip(" |")
            if line:
                lines.append(line)
        title = " ".join("".join(self.title).split())
        if title and (not lines or lines[0] != title):
            lines.insert(0, title)
        return "\n".join(lines)


def html_to_text(markup: str) -> str:
    parser = _TextExtractor()
    parser.feed(markup)
    parser.close()
    return parser.text()


def _limited(text: str, notes: list[NoteEntry]) -> WholeDocument:
    if len(text) > MAX_DOCUMENT_CHARS:
        text = text[:MAX_DOCUMENT_CHARS]
        notes.append(NoteEntry(DocumentNote.TRUNCATED))
    if not text.strip():
        notes.append(NoteEntry(DocumentNote.NO_TEXT))
    return WholeDocument(text=text, method=ExtractionMethod.NATIVE, notes=notes)


def extract_html(path: str) -> WholeDocument:
    with os_errors(), open(path, "rb") as handle:  # noqa: PTH123
        data = handle.read()
    declared = _META_CHARSET.search(data[:4096])
    preferred = declared.group(1).decode("ascii", "ignore") if declared else None
    return _limited(html_to_text(decode_bytes(data, preferred)), [])


def _body_text(message: EmailMessage) -> str:
    body = message.get_body(preferencelist=("plain", "html"))
    if body is None:
        return ""
    try:
        content = body.get_content()
    except (LookupError, KeyError):
        payload = body.get_payload(decode=True)
        content = decode_bytes(payload) if isinstance(payload, bytes) else ""
    if not isinstance(content, str):
        return ""
    if body.get_content_subtype() == "html":
        return html_to_text(content)
    return normalize_newlines(content).strip()


def extract_eml(path: str) -> WholeDocument:
    with os_errors(), open(path, "rb") as handle:  # noqa: PTH123
        message = email.message_from_binary_file(handle, policy=policy.default)
    assert isinstance(message, EmailMessage)
    lines: list[str] = []
    for header, key in (
        ("From", "extraction.eml.from"),
        ("To", "extraction.eml.to"),
        ("Cc", "extraction.eml.cc"),
        ("Date", "extraction.eml.date"),
        ("Subject", "extraction.eml.subject"),
    ):
        value = message.get(header)
        if value:
            lines.append(f"{t(key)} : {' '.join(str(value).split())}")
    lines.append("")
    lines.append(_body_text(message))

    notes: list[NoteEntry] = []
    attachments = [
        part.get_filename() or t("extraction.eml.unnamed") for part in message.iter_attachments()
    ]
    if attachments:
        names = ", ".join(attachments)
        lines.append("")
        lines.append(t("extraction.eml.attachments", names=names))
        notes.append(NoteEntry(DocumentNote.EML_ATTACHMENTS, names))
    return _limited("\n".join(lines).strip(), notes)
