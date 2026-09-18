"""Assemblage du prompt système et des messages de l'application (CDC §8).

Le prompt système reste stable pendant une conversation : les informations changeantes
(dossier, restriction, arborescence) sont transmises comme messages de l'application
(`SystemNotice`, `TreeMessage`), qui ne perturbent pas la mise en cache des fournisseurs.
"""

from __future__ import annotations

from dataclasses import dataclass

from archipelle.core import resources
from archipelle.core.i18n import t
from archipelle.persistence.settings_store import Mode
from archipelle.providers.pivot import SystemNotice

SYSTEM_PROMPT = "prompts/system.fr.md"


@dataclass(frozen=True)
class PromptContext:
    workdir: str
    mode: Mode
    max_iterations: int
    scope_rel: str | None = None
    general_knowledge: bool = False
    ocr_available: bool = True


def system_prompt(context: PromptContext) -> str:
    template = resources.read_text(SYSTEM_PROMPT)
    explore = context.mode == "explore"
    return template.format(
        workdir=context.workdir,
        scope_rule=(
            t("agent.prompt.scope", scope=context.scope_rel)
            if context.scope_rel
            else t("agent.prompt.no_scope")
        ),
        keywords_rule=t(
            "agent.prompt.keywords_explore" if explore else "agent.prompt.keywords_quick"
        ),
        knowledge_rule=t(
            "agent.prompt.knowledge_mixed"
            if context.general_knowledge
            else "agent.prompt.knowledge_strict"
        ),
        mode_label=t("agent.mode.explore" if explore else "agent.mode.quick"),
        iterations_rule=t("agent.prompt.iterations", count=context.max_iterations),
        ocr_rule=t(
            "agent.prompt.ocr_explore"
            if explore and context.ocr_available
            else "agent.prompt.ocr_unavailable"
            if not context.ocr_available
            else "agent.prompt.ocr_quick"
        ),
    )


def workdir_changed(workdir: str) -> SystemNotice:
    return SystemNotice("workdir_changed", t("agent.notice.workdir_changed", path=workdir))


def tree_changed() -> SystemNotice:
    return SystemNotice("tree_changed", t("agent.notice.tree_changed"))


def scope_changed(scope_rel: str | None) -> SystemNotice:
    if scope_rel:
        return SystemNotice("scope_set", t("agent.notice.scope_set", scope=scope_rel))
    return SystemNotice("scope_cleared", t("agent.notice.scope_cleared"))


def mode_changed(mode: Mode) -> SystemNotice:
    key = "agent.notice.mode_explore" if mode == "explore" else "agent.notice.mode_quick"
    return SystemNotice("mode_changed", t(key))
