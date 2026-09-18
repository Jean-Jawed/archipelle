"""Préparation de l'historique commune à tous les modules fournisseurs.

``segments()`` transforme la liste pivot en une suite régulière :

- les messages de rôle utilisateur consécutifs (question, notices, arborescence) sont
  regroupés en un seul segment ;
- chaque ``AssistantMessage`` qui demande des outils est suivi d'exactement un groupe
  de résultats, dans l'ordre des appels. Un résultat manquant reçoit un résultat
  synthétique, un résultat orphelin est écarté : aucune API n'accepte un historique
  désapparié (l'orchestration répare normalement l'historique en amont, CDC §7bis) ;
- optionnellement, un court message assistant de liaison est inséré entre des résultats
  d'outils et un message utilisateur (exigé par l'API Mistral).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, cast

from archipelle.core.i18n import t
from archipelle.providers.pivot import (
    AssistantMessage,
    PivotItem,
    SystemNotice,
    ToolCall,
    ToolResult,
    ToolResultGroup,
    TreeMessage,
    UserMessage,
    user_side_text,
)

_log = logging.getLogger(__name__)


@dataclass
class UserSegment:
    texts: list[str]

    @property
    def text(self) -> str:
        return "\n\n".join(self.texts)


@dataclass
class AssistantSegment:
    message: AssistantMessage
    bridge: bool = False  # message de liaison inséré par l'application


@dataclass
class ToolSegment:
    results: list[ToolResult]


type Segment = UserSegment | AssistantSegment | ToolSegment


def _pair(calls: list[ToolCall], group: ToolResultGroup | None) -> list[ToolResult]:
    available = {r.call_id: r for r in group.results} if group else {}
    paired: list[ToolResult] = []
    for call in calls:
        result = available.pop(call.call_id, None)
        if result is None:
            _log.warning("Résultat d'outil manquant pour %s : résultat synthétique", call.name)
            result = ToolResult(
                call.call_id, call.name, t("providers.missing_result"), synthetic=True
            )
        paired.append(result)
    if available:
        _log.warning("%d résultats d'outils orphelins écartés", len(available))
    return paired


def segments(items: list[PivotItem], *, bridge_after_tools: bool = False) -> list[Segment]:
    result: list[Segment] = []
    index = 0
    while index < len(items):
        item = items[index]
        if isinstance(item, UserMessage | SystemNotice | TreeMessage):
            text = user_side_text(item)
            last = result[-1] if result else None
            if isinstance(last, ToolSegment) and bridge_after_tools:
                bridge = AssistantMessage(t("providers.bridge_after_tools"), "", "")
                result.append(AssistantSegment(bridge, bridge=True))
                last = None
            if isinstance(last, UserSegment):
                last.texts.append(text)
            else:
                result.append(UserSegment([text]))
        elif isinstance(item, AssistantMessage):
            result.append(AssistantSegment(item))
            if item.tool_calls:
                following = items[index + 1] if index + 1 < len(items) else None
                group = following if isinstance(following, ToolResultGroup) else None
                result.append(ToolSegment(_pair(item.tool_calls, group)))
                if group is not None:
                    index += 1
        else:
            _log.warning("Groupe de résultats sans appel d'outils écarté")
        index += 1
    return result


# --- assemblage des appels d'outils diffusés en flux ---------------------------------


@dataclass
class _PartialCall:
    call_id: str = ""
    name: str = ""
    arguments: list[str] = field(default_factory=list[str])


class ToolCallAccumulator:
    """Recompose des appels d'outils diffusés morceau par morceau (format Chat Completions)."""

    def __init__(self) -> None:
        self._calls: dict[int, _PartialCall] = {}
        self._order: list[int] = []

    def add(
        self,
        index: int | None,
        call_id: str | None,
        name: str | None,
        arguments: str | None,
    ) -> None:
        if index is None:
            # Certains serveurs n'indiquent pas l'index : un identifiant nouveau ouvre un appel.
            known = [i for i, c in self._calls.items() if call_id and c.call_id == call_id]
            if known:
                index = known[0]
            elif call_id or not self._order:
                index = len(self._order)
            else:
                index = self._order[-1]
        partial = self._calls.get(index)
        if partial is not None and call_id and partial.call_id and call_id != partial.call_id:
            # Même index, identifiant différent : un nouvel appel complet.
            index = max(self._calls) + 1
            partial = None
        if partial is None:
            partial = _PartialCall()
            self._calls[index] = partial
            self._order.append(index)
        if call_id:
            partial.call_id = call_id
        if name and not partial.name:
            partial.name = name  # certains serveurs répètent le nom dans chaque fragment
        if arguments:
            partial.arguments.append(arguments)

    def calls(self) -> list[ToolCall]:
        result: list[ToolCall] = []
        for position, index in enumerate(self._order, start=1):
            partial = self._calls[index]
            if not partial.name:
                continue
            call_id = partial.call_id or f"call_{position}"
            result.append(ToolCall.from_raw(call_id, partial.name, "".join(partial.arguments)))
        return result


def as_list(value: object) -> list[Any]:
    """Liste typée à partir d'un attribut de SDK éventuellement absent."""
    if value is None or isinstance(value, str | bytes):
        return []
    try:
        return list(cast(Iterable[Any], value))
    except TypeError:
        return []


def normalize_stop_reason(raw: str | None) -> str | None:
    if raw is None:
        return None
    mapping = {
        "stop": "end",
        "end_turn": "end",
        "stop_sequence": "end",
        "tool_calls": "tool_calls",
        "function_call": "tool_calls",
        "tool_use": "tool_calls",
        "length": "length",
        "max_tokens": "length",
        "model_length": "length",
    }
    return mapping.get(raw, "other")
