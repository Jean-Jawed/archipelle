"""Historique des conversations (CDC §12) : lecture, écriture au fil de l'eau, réparation.

Le contenu brut des résultats d'outils n'est jamais archivé : seuls leurs marqueurs le
sont, le texte restant disponible dans le cache d'extraction.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal, cast

from archipelle.core.i18n import t
from archipelle.persistence.db import Database
from archipelle.providers.pivot import (
    AssistantMessage,
    PivotFormatError,
    PivotItem,
    SystemNotice,
    ToolResult,
    ToolResultGroup,
    TreeMessage,
    UserMessage,
    from_dict,
    to_dict,
)

_log = logging.getLogger(__name__)

type Mode = Literal["quick", "explore"]
type TurnStatus = Literal["in_progress", "complete", "partial", "interrupted"]

ITEM_KINDS = ("user", "system_notice", "tree", "assistant", "tool_results", "interruption")


def new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class Conversation:
    id: str
    title: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    provider_id: str = ""
    provider_locked: bool = False
    model: str = ""
    mode: Mode = "quick"
    workdir: str | None = None
    scope_rel: str | None = None
    tree_text: str | None = None
    tree_digest: str | None = None


@dataclass
class Turn:
    id: str
    conversation_id: str
    seq: int
    run_id: str
    mode: Mode
    status: TurnStatus
    workdir: str | None
    started_at: float
    ended_at: float | None = None
    restore_mode: Mode | None = None
    restore_model: str | None = None


@dataclass(frozen=True)
class Source:
    rel_path: str
    workdir: str | None
    verified: bool


@dataclass
class StoredItem:
    id: int
    turn_id: str
    seq: int
    kind: str
    payload: dict[str, Any]
    provider: str | None = None
    model: str | None = None
    created_at: float = 0.0
    sources: list[Source] = field(default_factory=list[Source])

    def pivot(self) -> PivotItem | None:
        """Élément du format pivot, ou ``None`` pour un message d'interruption."""
        if self.kind == "interruption":
            return None
        try:
            return from_dict(self.payload)
        except PivotFormatError:
            _log.warning("Élément d'historique illisible (id %d), ignoré", self.id)
            return None

    @property
    def text(self) -> str:
        return str(self.payload.get("text") or "")


@dataclass(frozen=True)
class IgnoredRecord:
    rel_path: str
    reason: str
    detail: str = ""


TITLE_MAX_CHARS = 60  # 🔧


def title_from_question(question: str) -> str:
    """Titre repris des premiers mots de la question, coupé sur un mot entier."""
    cleaned = " ".join(question.split())
    if len(cleaned) <= TITLE_MAX_CHARS:
        return cleaned
    cut = cleaned[:TITLE_MAX_CHARS]
    space = cut.rfind(" ")
    return (cut[:space] if space > TITLE_MAX_CHARS // 2 else cut).rstrip(" ,;:.") + "…"


def interruption_item(text: str, files: Sequence[str] = ()) -> dict[str, Any]:
    return {"type": "interruption", "text": text, "files": list(files)}


class History:
    def __init__(self, db: Database) -> None:
        self.db = db

    # --- conversations --------------------------------------------------------------

    def create_conversation(
        self,
        provider_id: str,
        model: str,
        *,
        mode: Mode = "quick",
        workdir: str | None = None,
        scope_rel: str | None = None,
        title: str = "",
    ) -> Conversation:
        now = time.time()
        conversation = Conversation(
            id=new_id(),
            title=title,
            created_at=now,
            updated_at=now,
            provider_id=provider_id,
            model=model,
            mode=mode,
            workdir=workdir,
            scope_rel=scope_rel,
        )
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO conversations (id, title, created_at, updated_at, provider_id,"
                " provider_locked, model, mode, workdir, scope_rel) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    conversation.id,
                    title,
                    now,
                    now,
                    provider_id,
                    0,
                    model,
                    mode,
                    workdir,
                    scope_rel,
                ),
            )
        return conversation

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        row = (
            self.db.connection()
            .execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
            .fetchone()
        )
        return _conversation(row) if row else None

    def list_conversations(self, limit: int = 100) -> list[Conversation]:
        rows = self.db.connection().execute(
            "SELECT * FROM conversations ORDER BY updated_at DESC LIMIT ?", (limit,)
        )
        return [_conversation(row) for row in rows]

    def search_conversations(self, query: str, limit: int = 50) -> list[Conversation]:
        """Recherche dans le contenu des conversations (CDC §12)."""
        cleaned = " ".join(f'"{word}"' for word in query.split() if word)
        if not cleaned:
            return []
        rows = self.db.connection().execute(
            "SELECT c.* FROM conversations c WHERE c.id IN ("
            " SELECT conversation_id FROM items_fts WHERE items_fts MATCH ?)"
            " ORDER BY c.updated_at DESC LIMIT ?",
            (cleaned, limit),
        )
        return [_conversation(row) for row in rows]

    def update_conversation(self, conversation_id: str, **fields: Any) -> None:
        allowed = {
            "title",
            "provider_id",
            "provider_locked",
            "model",
            "mode",
            "workdir",
            "scope_rel",
            "tree_text",
            "tree_digest",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"champs inconnus : {sorted(unknown)}")
        assignments = ", ".join(f"{name} = ?" for name in fields)
        values: list[Any] = [
            int(value) if isinstance(value, bool) else value for value in fields.values()
        ]
        with self.db.transaction() as conn:
            conn.execute(
                f"UPDATE conversations SET {assignments}, updated_at = ? WHERE id = ?",
                [*values, time.time(), conversation_id],
            )

    def touch_conversation(self, conversation_id: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (time.time(), conversation_id),
            )

    def delete_conversation(self, conversation_id: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM items_fts WHERE conversation_id = ?", (conversation_id,))
            conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))

    def delete_all_conversations(self) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM items_fts")
            conn.execute("DELETE FROM conversations")

    # --- tours ----------------------------------------------------------------------

    def open_turn(
        self,
        conversation_id: str,
        run_id: str,
        mode: Mode,
        workdir: str | None,
        *,
        restore_mode: Mode | None = None,
        restore_model: str | None = None,
    ) -> Turn:
        now = time.time()
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM turns WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            turn = Turn(
                id=new_id(),
                conversation_id=conversation_id,
                seq=int(row["seq"]),
                run_id=run_id,
                mode=mode,
                status="in_progress",
                workdir=workdir,
                started_at=now,
                restore_mode=restore_mode,
                restore_model=restore_model,
            )
            conn.execute(
                "INSERT INTO turns (id, conversation_id, seq, run_id, mode, status, workdir,"
                " started_at, restore_mode, restore_model) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    turn.id,
                    conversation_id,
                    turn.seq,
                    run_id,
                    mode,
                    turn.status,
                    workdir,
                    now,
                    restore_mode,
                    restore_model,
                ),
            )
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id)
            )
        return turn

    def close_turn(self, turn_id: str, status: TurnStatus) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE turns SET status = ?, ended_at = ? WHERE id = ?",
                (status, time.time(), turn_id),
            )

    def turns(self, conversation_id: str) -> list[Turn]:
        rows = self.db.connection().execute(
            "SELECT * FROM turns WHERE conversation_id = ? ORDER BY seq", (conversation_id,)
        )
        return [_turn(row) for row in rows]

    def unfinished_turns(self) -> list[Turn]:
        rows = self.db.connection().execute("SELECT * FROM turns WHERE status = 'in_progress'")
        return [_turn(row) for row in rows]

    # --- éléments -------------------------------------------------------------------

    def append_item(
        self,
        conversation_id: str,
        turn_id: str,
        item: PivotItem | dict[str, Any],
        *,
        provider: str | None = None,
        model: str | None = None,
    ) -> int:
        payload = item if isinstance(item, dict) else to_dict(_archivable(item))
        kind = _kind_of(payload)
        now = time.time()
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM items WHERE turn_id = ?", (turn_id,)
            ).fetchone()
            cursor = conn.execute(
                "INSERT INTO items (turn_id, seq, kind, payload, provider, model, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    turn_id,
                    int(row["seq"]),
                    kind,
                    json.dumps(payload, ensure_ascii=False),
                    provider,
                    model,
                    now,
                ),
            )
            item_id = int(cursor.lastrowid or 0)
            text = str(payload.get("text") or "")
            if kind in ("user", "assistant", "interruption") and text.strip():
                conn.execute(
                    "INSERT INTO items_fts (text, conversation_id, item_id) VALUES (?,?,?)",
                    (text, conversation_id, item_id),
                )
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id)
            )
        return item_id

    def items(self, conversation_id: str) -> list[StoredItem]:
        rows = self.db.connection().execute(
            "SELECT i.*, t.seq AS turn_seq FROM items i JOIN turns t ON t.id = i.turn_id"
            " WHERE t.conversation_id = ? ORDER BY t.seq, i.seq",
            (conversation_id,),
        )
        stored = [_item(row) for row in rows]
        by_id = {item.id: item for item in stored}
        if by_id:
            placeholders = ",".join("?" * len(by_id))
            sources = self.db.connection().execute(
                f"SELECT * FROM sources WHERE item_id IN ({placeholders}) ORDER BY ord",
                list(by_id),
            )
            for row in sources:
                by_id[int(row["item_id"])].sources.append(
                    Source(row["rel_path"], row["workdir"], bool(row["verified"]))
                )
        return stored

    def pivot_items(self, conversation_id: str) -> list[PivotItem]:
        """Historique au format pivot, prêt pour une requête (CDC §12)."""
        result: list[PivotItem] = []
        for stored in self.items(conversation_id):
            if stored.kind == "interruption":
                result.append(SystemNotice("interrupted", stored.text))
                continue
            item = stored.pivot()
            if item is not None:
                result.append(item)
        return result

    def set_sources(self, item_id: int, sources: Iterable[Source]) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM sources WHERE item_id = ?", (item_id,))
            conn.executemany(
                "INSERT INTO sources (item_id, ord, workdir, rel_path, verified)"
                " VALUES (?,?,?,?,?)",
                [
                    (item_id, index, source.workdir, source.rel_path, int(source.verified))
                    for index, source in enumerate(sources)
                ],
            )

    # --- fichiers consultés, journal, étapes ----------------------------------------

    def add_consulted(
        self, conversation_id: str, turn_id: str, workdir: str, rel_paths: Iterable[str]
    ) -> None:
        with self.db.transaction() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO consulted_files (conversation_id, turn_id, workdir,"
                " rel_path) VALUES (?,?,?,?)",
                [(conversation_id, turn_id, workdir, rel) for rel in rel_paths],
            )

    def consulted_files(self, conversation_id: str, workdir: str) -> set[str]:
        rows = self.db.connection().execute(
            "SELECT rel_path FROM consulted_files WHERE conversation_id = ? AND workdir = ?",
            (conversation_id, workdir),
        )
        return {row["rel_path"] for row in rows}

    def turn_consulted(self, turn_id: str) -> list[str]:
        rows = self.db.connection().execute(
            "SELECT rel_path FROM consulted_files WHERE turn_id = ? ORDER BY rel_path", (turn_id,)
        )
        return [row["rel_path"] for row in rows]

    def add_ignored(self, turn_id: str, entries: Iterable[IgnoredRecord]) -> None:
        with self.db.transaction() as conn:
            conn.executemany(
                "INSERT INTO ignored_files (turn_id, rel_path, reason, detail) VALUES (?,?,?,?)",
                [(turn_id, e.rel_path, e.reason, e.detail) for e in entries],
            )

    def ignored_files(self, turn_id: str) -> list[IgnoredRecord]:
        rows = self.db.connection().execute(
            "SELECT rel_path, reason, detail FROM ignored_files WHERE turn_id = ? ORDER BY id",
            (turn_id,),
        )
        return [IgnoredRecord(r["rel_path"], r["reason"], r["detail"] or "") for r in rows]

    def add_step(self, turn_id: str, payload: dict[str, Any]) -> None:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM turn_steps WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
            conn.execute(
                "INSERT INTO turn_steps (turn_id, seq, payload) VALUES (?,?,?)",
                (turn_id, int(row["seq"]), json.dumps(payload, ensure_ascii=False)),
            )

    def steps(self, turn_id: str) -> list[dict[str, Any]]:
        rows = self.db.connection().execute(
            "SELECT payload FROM turn_steps WHERE turn_id = ? ORDER BY seq", (turn_id,)
        )
        return [cast(dict[str, Any], json.loads(row["payload"])) for row in rows]

    # --- réparation -----------------------------------------------------------------

    def repair_turn(self, turn: Turn, synthetic_text: str, closing_text: str) -> None:
        """Apparie les appels d'outils restés sans résultat et clôt le tour (CDC §7bis)."""
        stored = [item for item in self.items(turn.conversation_id) if item.turn_id == turn.id]
        last = stored[-1] if stored else None
        if last is not None and last.kind == "assistant":
            message = last.pivot()
            if isinstance(message, AssistantMessage) and message.tool_calls:
                group = ToolResultGroup(
                    [
                        ToolResult(call.call_id, call.name, synthetic_text, synthetic=True,
                                   marker=synthetic_text)
                        for call in message.tool_calls
                    ]
                )  # fmt: skip
                self.append_item(turn.conversation_id, turn.id, group)
        files = self.turn_consulted(turn.id)
        self.append_item(turn.conversation_id, turn.id, interruption_item(closing_text, files))
        self.close_turn(turn.id, "interrupted")

    def repair_unfinished(self) -> list[str]:
        """Répare au démarrage les tours laissés incomplets par un plantage (CDC §7bis)."""
        repaired: list[str] = []
        for turn in self.unfinished_turns():
            _log.warning("Tour %s resté incomplet : réparation", turn.id)
            self.repair_turn(
                turn,
                t("agent.synthetic.app_stopped"),
                t("agent.interrupted.app_stopped"),
            )
            repaired.append(turn.id)
        return repaired


# --- conversions ---------------------------------------------------------------------


def _archivable(item: PivotItem) -> PivotItem:
    """Retire le contenu brut des résultats d'outils avant archivage (CDC §12)."""
    if isinstance(item, ToolResultGroup):
        return ToolResultGroup(
            [
                replace(result, content=result.archived_content, marker=result.archived_content)
                for result in item.results
            ]
        )
    return item


def _kind_of(payload: dict[str, Any]) -> str:
    mapping = {
        "user": "user",
        "notice": "system_notice",
        "tree": "tree",
        "assistant": "assistant",
        "tool_results": "tool_results",
        "interruption": "interruption",
    }
    kind = mapping.get(str(payload.get("type")))
    if kind is None:
        raise ValueError(f"élément non archivable : {payload.get('type')!r}")
    return kind


def _conversation(row: Any) -> Conversation:
    return Conversation(
        id=row["id"],
        title=row["title"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        provider_id=row["provider_id"],
        provider_locked=bool(row["provider_locked"]),
        model=row["model"],
        mode=cast(Mode, row["mode"]),
        workdir=row["workdir"],
        scope_rel=row["scope_rel"],
        tree_text=row["tree_text"],
        tree_digest=row["tree_digest"],
    )


def _turn(row: Any) -> Turn:
    return Turn(
        id=row["id"],
        conversation_id=row["conversation_id"],
        seq=row["seq"],
        run_id=row["run_id"],
        mode=cast(Mode, row["mode"]),
        status=cast(TurnStatus, row["status"]),
        workdir=row["workdir"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        restore_mode=cast("Mode | None", row["restore_mode"]),
        restore_model=row["restore_model"],
    )


def _item(row: Any) -> StoredItem:
    return StoredItem(
        id=int(row["id"]),
        turn_id=row["turn_id"],
        seq=int(row["seq"]),
        kind=row["kind"],
        payload=cast(dict[str, Any], json.loads(row["payload"])),
        provider=row["provider"],
        model=row["model"],
        created_at=row["created_at"],
    )


def tree_item(text: str, digest: str) -> TreeMessage:
    return TreeMessage(digest, text)


def user_item(text: str) -> UserMessage:
    return UserMessage(text)
