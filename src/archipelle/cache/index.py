"""Index SQLite du cache : documents et méthode d'extraction de chaque page."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from archipelle.cache.keys import Fingerprint
from archipelle.extraction.types import DocumentNote, ErrorCode, ExtractionMethod, NoteEntry
from archipelle.persistence.db import Database


@dataclass(frozen=True)
class PageRecord:
    method: ExtractionMethod
    chars: int


@dataclass
class DocumentRecord:
    doc_key: str
    abs_path: str
    fingerprint: Fingerprint
    page_count: int | None
    notes: list[NoteEntry]
    bytes_on_disk: int
    pages: dict[int, PageRecord] = field(default_factory=dict[int, PageRecord])
    failure: tuple[ErrorCode, str] | None = None


def _notes_to_json(notes: list[NoteEntry]) -> str:
    return json.dumps([[entry.note.value, entry.detail] for entry in notes])


def _failure_from_json(raw: str | None) -> tuple[ErrorCode, str] | None:
    if not raw:
        return None
    try:
        code, detail = json.loads(raw)
        return ErrorCode(code), str(detail)
    except (ValueError, TypeError):
        return None


def _notes_from_json(raw: str | None) -> list[NoteEntry]:
    if not raw:
        return []
    entries: list[NoteEntry] = []
    for item in json.loads(raw):
        try:
            entries.append(NoteEntry(DocumentNote(item[0]), str(item[1])))
        except (ValueError, IndexError, TypeError):
            continue
    return entries


class CacheIndex:
    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, key: str) -> DocumentRecord | None:
        conn = self.db.connection()
        row = conn.execute("SELECT * FROM documents WHERE doc_key = ?", (key,)).fetchone()
        if row is None:
            return None
        record = DocumentRecord(
            doc_key=key,
            abs_path=row["abs_path"],
            fingerprint=Fingerprint(row["mtime_ns"], row["size"]),
            page_count=row["page_count"],
            notes=_notes_from_json(row["notes"]),
            bytes_on_disk=row["bytes_on_disk"],
            failure=_failure_from_json(row["failure"]),
        )
        for page in conn.execute("SELECT page, method, chars FROM pages WHERE doc_key = ?", (key,)):
            record.pages[page["page"]] = PageRecord(ExtractionMethod(page["method"]), page["chars"])
        return record

    def create(self, key: str, abs_path: str, fingerprint: Fingerprint) -> DocumentRecord:
        """Crée (ou remplace entièrement) l'entrée d'un document."""
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM documents WHERE doc_key = ?", (key,))
            conn.execute(
                "INSERT INTO documents (doc_key, abs_path, mtime_ns, size, last_used)"
                " VALUES (?, ?, ?, ?, ?)",
                (key, abs_path, fingerprint.mtime_ns, fingerprint.size, time.time()),
            )
        return DocumentRecord(key, abs_path, fingerprint, None, [], 0)

    def touch(self, key: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE documents SET last_used = ? WHERE doc_key = ?", (time.time(), key))

    def set_page_count(self, key: str, count: int) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE documents SET page_count = ? WHERE doc_key = ?", (count, key))

    def set_notes(self, key: str, notes: list[NoteEntry]) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE documents SET notes = ? WHERE doc_key = ?", (_notes_to_json(notes), key)
            )

    def set_failure(self, key: str, code: ErrorCode, detail: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE documents SET failure = ? WHERE doc_key = ?",
                (json.dumps([code.value, detail]), key),
            )

    def save_page(
        self, key: str, page: int, method: ExtractionMethod, chars: int, bytes_delta: int
    ) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO pages (doc_key, page, method, chars) VALUES (?, ?, ?, ?)"
                " ON CONFLICT (doc_key, page) DO UPDATE SET method = excluded.method,"
                " chars = excluded.chars",
                (key, page, method.value, chars),
            )
            conn.execute(
                "UPDATE documents SET bytes_on_disk = MAX(0, bytes_on_disk + ?), last_used = ?"
                " WHERE doc_key = ?",
                (bytes_delta, time.time(), key),
            )

    def delete(self, key: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM documents WHERE doc_key = ?", (key,))

    def total_bytes(self) -> int:
        row = self.db.connection().execute("SELECT SUM(bytes_on_disk) FROM documents").fetchone()
        return int(row[0] or 0)

    def least_recently_used(self) -> list[tuple[str, int]]:
        rows = self.db.connection().execute(
            "SELECT doc_key, bytes_on_disk FROM documents ORDER BY last_used ASC"
        )
        return [(row[0], row[1]) for row in rows]
