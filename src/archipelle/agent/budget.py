"""Budget de contexte (CDC §7ter).

Répartition de la fenêtre utile, recalculée à chaque appel puisque le modèle peut changer
en cours de conversation :

- arborescence : 15 % 🔧 au plus, jamais remplacée par un marqueur ;
- résultats d'outils : 55 % 🔧, réduits d'autant que la part fixe déborde ;
- part fixe (prompt système, outils, messages, marqueurs) : jamais retirée.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from archipelle.core.outcome import AppError
from archipelle.providers.catalog import ModelInfo
from archipelle.providers.pivot import (
    AssistantMessage,
    PivotItem,
    ToolResultGroup,
    TreeMessage,
    to_dict,
)

_log = logging.getLogger(__name__)

CHARS_PER_TOKEN = 4  # 🔧 estimation simple (CDC §7ter)
TREE_SHARE = 0.15  # 🔧
TOOLS_SHARE = 0.55  # 🔧
FIXED_SHARE = 0.30  # 🔧


class ContextSaturated(AppError):
    """La part fixe ne tient plus dans la fenêtre : la conversation est saturée."""

    def __init__(self) -> None:
        super().__init__("agent.errors.context_saturated")


def estimate_tokens(text: str) -> int:
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def item_tokens(item: PivotItem) -> int:
    """Estimation du coût d'un élément, sérialisation comprise (clés, identifiants)."""
    payload = to_dict(item)
    return estimate_tokens(_measure(payload))


def _measure(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        entries: dict[object, object] = value  # type: ignore[assignment]
        return "".join(_measure(k) + _measure(v) for k, v in entries.items())
    if isinstance(value, list):
        items: list[object] = value  # type: ignore[assignment]
        return "".join(_measure(item) for item in items)
    return str(value or "")


@dataclass(frozen=True)
class BudgetReport:
    usable_tokens: int
    fixed_tokens: int
    tree_tokens: int
    tools_allowance: int
    tools_tokens: int
    replaced: int  # résultats remplacés par leur marqueur

    @property
    def tree_budget_chars(self) -> int:
        return int(self.usable_tokens * TREE_SHARE) * CHARS_PER_TOKEN


def tree_budget_chars(model: ModelInfo) -> int:
    """Taille maximale du relevé d'arborescence injecté (CDC §5)."""
    usable = max(0, model.usable_context - model.max_output)
    return int(usable * TREE_SHARE) * CHARS_PER_TOKEN


def _marked(group: ToolResultGroup) -> ToolResultGroup:
    return ToolResultGroup(
        [replace(result, content=result.archived_content) for result in group.results]
    )


def fit(
    items: list[PivotItem], model: ModelInfo, *, overhead_chars: int = 0
) -> tuple[list[PivotItem], BudgetReport]:
    """Historique ajusté à la fenêtre du modèle.

    ``overhead_chars`` couvre le prompt système et les définitions d'outils. Les résultats
    d'outils les plus anciens sont remplacés par leur marqueur jusqu'à tenir dans leur part ;
    ``ContextSaturated`` est levée si la part fixe ne tient pas, même sans aucun résultat.
    Une seule arborescence est conservée : la plus récente (CDC §5).
    """
    usable = max(0, model.usable_context - model.max_output)
    kept: list[PivotItem] = []
    last_tree = max(
        (i for i, item in enumerate(items) if isinstance(item, TreeMessage)), default=-1
    )
    for index, item in enumerate(items):
        if isinstance(item, TreeMessage) and index != last_tree:
            continue
        kept.append(item)

    tree_tokens = sum(item_tokens(i) for i in kept if isinstance(i, TreeMessage))
    groups = [(index, item) for index, item in enumerate(kept) if isinstance(item, ToolResultGroup)]
    fixed = estimate_tokens(" " * overhead_chars) + sum(
        item_tokens(item) for item in kept if not isinstance(item, TreeMessage | ToolResultGroup)
    )
    fixed += sum(item_tokens(_marked(group)) for _, group in groups)  # marqueurs : part fixe
    if fixed + tree_tokens >= usable:
        raise ContextSaturated()

    allowance = max(0, min(int(usable * TOOLS_SHARE), usable - fixed - tree_tokens))
    if fixed > int(usable * FIXED_SHARE):
        _log.info("Part fixe débordante : budget des résultats d'outils réduit")

    # Coût supplémentaire du contenu brut par rapport au marqueur, du plus ancien au plus récent.
    extra = {index: item_tokens(group) - item_tokens(_marked(group)) for index, group in groups}
    total_extra = sum(extra.values())
    replaced: set[int] = set()
    for index, _ in groups:
        if total_extra <= allowance:
            break
        replaced.add(index)
        total_extra -= extra[index]

    result = [
        _marked(item) if index in replaced and isinstance(item, ToolResultGroup) else item
        for index, item in enumerate(kept)
    ]
    report = BudgetReport(
        usable_tokens=usable,
        fixed_tokens=fixed,
        tree_tokens=tree_tokens,
        tools_allowance=allowance,
        tools_tokens=total_extra,
        replaced=len(replaced),
    )
    if replaced:
        _log.info("Contexte : %d résultats d'outils remplacés par leur marqueur", len(replaced))
    return result, report


def marker_for(tool_name: str, target: str, content: str) -> str:
    """Marqueur qui remplace un résultat d'outil dans le contexte et dans l'archive."""
    from archipelle.core.i18n import format_number, t

    key = "agent.marker.file" if target else "agent.marker.tool"
    return t(key, tool=tool_name, target=target, chars=format_number(len(content)))


def answer_tokens(message: AssistantMessage) -> int:
    return item_tokens(message)
