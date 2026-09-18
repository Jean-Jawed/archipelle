"""Format pivot : historique neutre, indépendant des API (CDC §13, docs/ARCHITECTURE.md §5).

Chaque module fournisseur traduit ces éléments vers son API au moment de l'appel, et
retraduit la réponse vers un ``AssistantMessage``. Le pivot est aussi la forme archivée
dans l'historique (``to_dict``/``from_dict``), c'est pourquoi il reste sérialisable en JSON.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, cast


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class ToolCall:
    call_id: str  # identifiant tel que produit par le fournisseur
    name: str
    arguments: dict[str, Any]  # JSON décodé ; {} si illisible
    raw_arguments: str  # texte exact reçu, renvoyé tel quel à l'API

    @classmethod
    def from_raw(cls, call_id: str, name: str, raw: str) -> ToolCall:
        return cls(call_id, name, decode_arguments(raw), raw)

    @classmethod
    def from_arguments(cls, call_id: str, name: str, arguments: dict[str, Any]) -> ToolCall:
        return cls(call_id, name, arguments, json.dumps(arguments, ensure_ascii=False))

    @property
    def arguments_valid(self) -> bool:
        return self.raw_arguments.strip() in ("", "{}") or bool(self.arguments)


def decode_arguments(raw: str) -> dict[str, Any]:
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


@dataclass(frozen=True)
class ReasoningBlock:
    """Bloc de raisonnement opaque, renvoyé tel quel au modèle qui l'a produit."""

    provider: str
    model: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class UserMessage:
    text: str


@dataclass(frozen=True)
class SystemNotice:
    """Information de l'application destinée au modèle (changement de dossier…)."""

    kind: str
    text: str


@dataclass(frozen=True)
class TreeMessage:
    """Arborescence courante du dossier de travail, unique dans le contexte (CDC §5)."""

    snapshot_digest: str
    text: str


@dataclass(frozen=True)
class AssistantMessage:
    text: str
    provider: str
    model: str
    tool_calls: list[ToolCall] = field(default_factory=list[ToolCall])
    reasoning: list[ReasoningBlock] = field(default_factory=list[ReasoningBlock])
    usage: Usage | None = None
    stop_reason: str | None = None  # valeur normalisée : "end", "tool_calls", "length", "other"


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    name: str
    content: str
    is_error: bool = False
    synthetic: bool = False  # « non exécuté : limite atteinte », « interrompu »…
    marker: str | None = None  # remplace le contenu dans le contexte (CDC §7ter)

    @property
    def archived_content(self) -> str:
        """Texte conservé dans l'historique et substitué au contenu quand le budget de
        contexte l'exige (CDC §7ter, §12).

        À ne jamais envoyer à la place de ``content`` : les modules fournisseurs
        transmettent toujours ``content``, que ``budget.fit()`` a déjà remplacé par ce
        marqueur si nécessaire.
        """
        return self.marker if self.marker is not None else self.content


@dataclass(frozen=True)
class ToolResultGroup:
    """Résultats des appels du dernier ``AssistantMessage``, dans le même ordre."""

    results: list[ToolResult]


type PivotItem = UserMessage | SystemNotice | TreeMessage | AssistantMessage | ToolResultGroup

PIVOT_VERSION = 1


# --- sérialisation -------------------------------------------------------------------


def _call_to_dict(call: ToolCall) -> dict[str, Any]:
    return {"id": call.call_id, "name": call.name, "raw_arguments": call.raw_arguments}


def _call_from_dict(data: dict[str, Any]) -> ToolCall:
    return ToolCall.from_raw(str(data["id"]), str(data["name"]), str(data["raw_arguments"]))


def to_dict(item: PivotItem) -> dict[str, Any]:
    match item:
        case UserMessage(text=text):
            return {"type": "user", "text": text}
        case SystemNotice(kind=kind, text=text):
            return {"type": "notice", "kind": kind, "text": text}
        case TreeMessage(snapshot_digest=digest, text=text):
            return {"type": "tree", "digest": digest, "text": text}
        case AssistantMessage():
            return {
                "type": "assistant",
                "text": item.text,
                "provider": item.provider,
                "model": item.model,
                "tool_calls": [_call_to_dict(c) for c in item.tool_calls],
                "reasoning": [
                    {"provider": r.provider, "model": r.model, "payload": r.payload}
                    for r in item.reasoning
                ],
                "usage": (
                    {"input": item.usage.input_tokens, "output": item.usage.output_tokens}
                    if item.usage
                    else None
                ),
                "stop_reason": item.stop_reason,
            }
        case ToolResultGroup(results=results):
            return {
                "type": "tool_results",
                "results": [
                    {
                        "call_id": r.call_id,
                        "name": r.name,
                        "content": r.content,
                        "is_error": r.is_error,
                        "synthetic": r.synthetic,
                        "marker": r.marker,
                    }
                    for r in results
                ],
            }


class PivotFormatError(ValueError):
    pass


def from_dict(data: dict[str, Any]) -> PivotItem:
    try:
        kind = data["type"]
        if kind == "user":
            return UserMessage(str(data["text"]))
        if kind == "notice":
            return SystemNotice(str(data["kind"]), str(data["text"]))
        if kind == "tree":
            return TreeMessage(str(data["digest"]), str(data["text"]))
        if kind == "assistant":
            usage = cast(dict[str, Any] | None, data.get("usage"))
            return AssistantMessage(
                text=str(data.get("text") or ""),
                provider=str(data["provider"]),
                model=str(data["model"]),
                tool_calls=[
                    _call_from_dict(c)
                    for c in cast(list[dict[str, Any]], data.get("tool_calls") or [])
                ],
                reasoning=[
                    ReasoningBlock(str(r["provider"]), str(r["model"]), dict(r["payload"]))
                    for r in cast(list[dict[str, Any]], data.get("reasoning") or [])
                ],
                usage=Usage(int(usage["input"]), int(usage["output"])) if usage else None,
                stop_reason=cast(str | None, data.get("stop_reason")),
            )
        if kind == "tool_results":
            return ToolResultGroup(
                [
                    ToolResult(
                        call_id=str(r["call_id"]),
                        name=str(r.get("name") or ""),
                        content=str(r.get("content") or ""),
                        is_error=bool(r.get("is_error")),
                        synthetic=bool(r.get("synthetic")),
                        marker=cast(str | None, r.get("marker")),
                    )
                    for r in cast(list[dict[str, Any]], data["results"])
                ]
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise PivotFormatError(str(exc)) from exc
    raise PivotFormatError(f"type inconnu : {data.get('type')!r}")


# --- aides communes aux modules fournisseurs -----------------------------------------


def notice_text(item: SystemNotice | TreeMessage) -> str:
    """Contenu balisé des messages de l'application, transmis en rôle utilisateur."""
    kind = "tree" if isinstance(item, TreeMessage) else item.kind
    return f'<archipelle-notice kind="{kind}">\n{item.text}\n</archipelle-notice>'


def user_side_text(item: UserMessage | SystemNotice | TreeMessage) -> str:
    return item.text if isinstance(item, UserMessage) else notice_text(item)


def reasoning_for(message: AssistantMessage, provider: str, model: str) -> list[ReasoningBlock]:
    """Blocs à renvoyer : uniquement ceux du modèle actif (CDC §13)."""
    return [r for r in message.reasoning if r.provider == provider and r.model == model]
