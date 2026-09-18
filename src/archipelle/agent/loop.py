"""Déroulement d'un tour (CDC §7, §7bis, §7ter, §8).

Un tour s'exécute dans le thread worker : relevé d'arborescence, itérations (un appel au
modèle chacune), exécution d'au plus 5 appels d'outils par itération, puis réponse finale
dont les sources sont vérifiées par le code. Tout est écrit dans l'historique au fil de
l'eau et publié sous forme d'événements.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from typing import Any

from archipelle.agent import citations, prompts
from archipelle.agent.budget import ContextSaturated, fit, marker_for, tree_budget_chars
from archipelle.agent.history import (
    History,
    IgnoredRecord,
    Mode,
    Turn,
    TurnStatus,
    interruption_item,
    title_from_question,
)
from archipelle.core.cancel import CancelToken
from archipelle.core.events import Event, EventType
from archipelle.core.i18n import t
from archipelle.core.outcome import AppError, Cancelled
from archipelle.core.toolspec import ToolSpec
from archipelle.providers.base import ChatRequest, Provider, ProviderError, ToolChoice
from archipelle.providers.catalog import ModelInfo
from archipelle.providers.pivot import (
    AssistantMessage,
    PivotItem,
    SystemNotice,
    ToolCall,
    ToolResult,
    ToolResultGroup,
    TreeMessage,
    UserMessage,
)
from archipelle.providers.retry import RetryPolicy, call_with_retry
from archipelle.tools import registry, tree
from archipelle.tools.context import ToolContext, ToolOutcome

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LoopLimits:
    """Plafonds de la boucle (CDC §7) 🔧"""

    quick_iterations: int = 4
    explore_iterations: int = 8
    quick_seconds: float = 300.0
    explore_seconds: float = 900.0
    max_calls_per_iteration: int = 5

    def iterations(self, mode: Mode) -> int:
        return self.explore_iterations if mode == "explore" else self.quick_iterations

    def seconds(self, mode: Mode) -> float:
        return self.explore_seconds if mode == "explore" else self.quick_seconds


type EventSink = Any  # Callable[[Event], None]


@dataclass
class TurnRequest:
    conversation_id: str
    run_id: str
    mode: Mode
    question: str
    workdir: str
    scope_rel: str | None
    provider: Provider
    provider_id: str
    model: ModelInfo
    general_knowledge: bool = False
    restore_mode: Mode | None = None
    restore_model: str | None = None


@dataclass
class TurnResult:
    turn_id: str
    status: TurnStatus
    answer: str = ""
    sources: list[Any] = field(default_factory=list[Any])
    consulted: list[str] = field(default_factory=list[str])
    error_key: str | None = None
    iterations: int = 0


class TurnRunner:
    def __init__(
        self,
        request: TurnRequest,
        *,
        history: History,
        tool_context: ToolContext,
        cancel: CancelToken,
        publish: EventSink,
        limits: LoopLimits | None = None,
        retry_policy: RetryPolicy | None = None,
        ocr_available: bool = True,
    ) -> None:
        self.request = request
        self.history = history
        self.ctx = tool_context
        self.cancel = cancel
        self.publish_event = publish
        self.limits = limits or LoopLimits()
        self.retry_policy = retry_policy
        self.ocr_available = ocr_available
        self.max_iterations = self.limits.iterations(request.mode)
        self.deadline = time.monotonic() + self.limits.seconds(request.mode)
        self.tools: list[ToolSpec] = [tool.spec for tool in registry.registered_tools().values()]
        self.turn: Turn | None = None
        self.consulted: list[str] = []
        # Messages de l'application accumulés depuis le tour précédent (dossier, restriction).
        self.pending_notices: list[SystemNotice] = []
        # Contexte envoyé au modèle : les tours précédents viennent de l'archive (contenus
        # déjà remplacés par leurs marqueurs, CDC §12), le tour en cours garde ses contenus
        # complets tant que le budget le permet (CDC §7ter).
        self.context: list[PivotItem] = []
        self.deadline_reached = False

    # --- événements et journal ------------------------------------------------------

    def publish(self, kind: EventType, **payload: Any) -> None:
        self.publish_event(Event(self.request.run_id, self.request.conversation_id, kind, payload))

    def record_step(self, kind: str, **payload: Any) -> None:
        """Étape publiée puis archivée, pour que la transparence reste consultable."""
        if self.turn is not None:
            self.history.add_step(self.turn.id, {"kind": kind, **payload})

    def _progress(self, key: str, params: dict[str, Any]) -> None:
        self.publish(EventType.STEP_PROGRESS, key=key, **params)

    # --- préparation ----------------------------------------------------------------

    def prepare(self) -> None:
        """Arborescence à jour, question enregistrée, tour ouvert (CDC §5, §7bis)."""
        conversation = self.history.get_conversation(self.request.conversation_id)
        assert conversation is not None
        self.context = self.history.pivot_items(self.request.conversation_id)
        snapshot = tree.scan(self.ctx.workspace, self.ctx.exclusions, self.ctx.stop())
        rendered = tree.render(snapshot, tree_budget_chars(self.request.model))
        self.cancel.raise_if_cancelled()
        turn = self.history.open_turn(
            self.request.conversation_id,
            self.request.run_id,
            self.request.mode,
            self.request.workdir,
            restore_mode=self.request.restore_mode,
            restore_model=self.request.restore_model,
        )
        self.turn = turn
        self.publish(
            EventType.TURN_STARTED,
            turn_id=turn.id,
            mode=self.request.mode,
            model=self.request.model.id,
            max_iterations=self.max_iterations,
            max_seconds=self.limits.seconds(self.request.mode),
        )
        for notice in self.pending_notices:
            self.append(notice)
        if snapshot.digest != conversation.tree_digest:
            if conversation.tree_digest is not None:
                self.append(prompts.tree_changed())
            self.append(TreeMessage(snapshot.digest, rendered.text))
            self.history.update_conversation(
                conversation.id, tree_text=rendered.text, tree_digest=snapshot.digest
            )
        self.append(UserMessage(self.request.question))
        if not conversation.title and self.request.question.strip():
            self.history.update_conversation(
                conversation.id, title=title_from_question(self.request.question)
            )

    def append(
        self,
        item: PivotItem | dict[str, Any],
        *,
        provider: str | None = None,
        model: str | None = None,
    ) -> int:
        assert self.turn is not None
        if not isinstance(item, dict):
            self.context.append(item)
        return self.history.append_item(
            self.request.conversation_id, self.turn.id, item, provider=provider, model=model
        )

    # --- boucle ---------------------------------------------------------------------

    def run(self) -> TurnResult:
        try:
            self.prepare()
            return self._iterate()
        except Cancelled:
            return self._interrupted()
        except ContextSaturated as error:
            return self._stop_with_message(error.message(), "partial", error.key)
        except ProviderError as error:
            return self._provider_failure(error)

    def _iterate(self) -> TurnResult:
        assert self.turn is not None
        forced_final = False
        for index in range(1, self.max_iterations + 1):
            self.cancel.raise_if_cancelled()
            last = index == self.max_iterations or self._out_of_time()
            choice: ToolChoice = "none" if last else "auto"
            self.publish(
                EventType.ITERATION,
                index=index,
                max=self.max_iterations,
                elapsed=self.limits.seconds(self.request.mode) - max(0.0, self._remaining()),
                tool_choice=choice,
            )
            self.record_step("iteration", index=index, tool_choice=choice)
            message = self._ask(choice)
            if not message.tool_calls:
                return self._finalize(message, index, forced=forced_final)
            if last:
                message, forced_final = self._tool_choice_fallback(message)
                if not message.tool_calls:
                    return self._finalize(message, index, forced=forced_final)
                return self._partial_answer(index)
            self._run_tools(message.tool_calls)
        return self._partial_answer(self.max_iterations)

    def _ask(self, tool_choice: ToolChoice) -> AssistantMessage:
        items, report = fit(
            self.context,
            self.request.model,
            overhead_chars=len(self.system_prompt()) + self._tools_chars(),
        )
        _log.info(
            "Contexte : %d/%d tokens fixes, %d résultats remplacés",
            report.fixed_tokens,
            report.usable_tokens,
            report.replaced,
        )
        request = ChatRequest(
            model=self.request.model.id,
            system=self.system_prompt(),
            items=items,
            tools=self.tools,
            tool_choice=tool_choice,
            max_output_tokens=self.request.model.max_output,
        )
        message = call_with_retry(
            lambda: self.request.provider.complete(request, self.cancel),
            cancel=self.cancel,
            policy=self.retry_policy,
            on_retry=self._on_retry,
        )
        self.append(message, provider=message.provider, model=message.model)
        return message

    def _on_retry(self, error: ProviderError, attempt: int, wait: float) -> None:
        self.publish(
            EventType.ERROR,
            key=error.key,
            message=error.message(),
            recoverable=True,
            attempt=attempt,
            wait=wait,
        )
        self.record_step("retry", error=type(error).__name__, attempt=attempt)

    def system_prompt(self) -> str:
        return prompts.system_prompt(
            prompts.PromptContext(
                workdir=self.request.workdir,
                mode=self.request.mode,
                max_iterations=self.max_iterations,
                scope_rel=self.request.scope_rel,
                general_knowledge=self.request.general_knowledge,
                ocr_available=self.ocr_available,
            )
        )

    def _tools_chars(self) -> int:
        return sum(len(spec.description) + len(str(spec.parameters)) for spec in self.tools)

    # --- outils ---------------------------------------------------------------------

    def _run_tools(self, calls: list[ToolCall]) -> None:
        results: list[ToolResult] = []
        allowed = self.limits.max_calls_per_iteration
        for position, call in enumerate(calls):
            if position >= allowed:
                results.append(self._synthetic(call, "agent.synthetic.too_many_calls"))
                continue
            if self._out_of_time():
                results.append(self._synthetic(call, "agent.synthetic.deadline"))
                continue
            try:
                self.cancel.raise_if_cancelled()
            except Cancelled:
                results.extend(
                    self._synthetic(c, "agent.synthetic.interrupted") for c in calls[position:]
                )
                self.append(ToolResultGroup(results))
                raise
            results.append(self._execute(call))
        self.append(ToolResultGroup(results))

    def _execute(self, call: ToolCall) -> ToolResult:
        target = _target_of(call)
        self.publish(
            EventType.STEP_STARTED, tool=call.name, target=target, arguments=call.arguments
        )
        self.record_step("tool_call", tool=call.name, target=target, arguments=call.arguments)
        started = time.monotonic()
        outcome = registry.dispatch(self.ctx, call.name, call.arguments)
        self._record_outcome(outcome)
        self.record_step(
            "tool_result",
            tool=call.name,
            target=target,
            ok=outcome.ok,
            chars=outcome.size_chars,
            seconds=round(time.monotonic() - started, 2),
        )
        return ToolResult(
            call_id=call.call_id,
            name=call.name,
            content=outcome.content,
            is_error=not outcome.ok,
            marker=marker_for(call.name, target, outcome.content),
        )

    def _record_outcome(self, outcome: ToolOutcome) -> None:
        assert self.turn is not None
        fresh = [f.rel_path for f in outcome.consulted if f.rel_path not in self.consulted]
        if fresh:
            self.consulted.extend(fresh)
            self.history.add_consulted(
                self.request.conversation_id, self.turn.id, self.request.workdir, fresh
            )
        for consulted in outcome.consulted:
            self.publish(
                EventType.FILE_CONSULTED,
                path=consulted.rel_path,
                from_cache=consulted.from_cache,
            )
        if outcome.ignored:
            self.history.add_ignored(
                self.turn.id,
                [IgnoredRecord(e.rel_path, e.reason.value, e.detail) for e in outcome.ignored],
            )
            for ignored in outcome.ignored:
                self.publish(
                    EventType.FILE_IGNORED,
                    path=ignored.rel_path,
                    reason=ignored.reason.value,
                    detail=ignored.detail,
                )

    def _synthetic(self, call: ToolCall, key: str) -> ToolResult:
        text = t(key)
        return ToolResult(
            call_id=call.call_id,
            name=call.name,
            content=text,
            is_error=True,
            synthetic=True,
            marker=text,
        )

    # --- fins de tour ---------------------------------------------------------------

    def _tool_choice_fallback(self, message: AssistantMessage) -> tuple[AssistantMessage, bool]:
        """Le modèle a redemandé des outils malgré « aucun » : un seul rappel (CDC §7)."""
        _log.warning("tool_choice « none » ignoré : rappel unique hors compteur")
        self.record_step("tool_choice_ignored", calls=len(message.tool_calls))
        self.append(
            ToolResultGroup(
                [
                    self._synthetic(call, "agent.synthetic.no_more_tools")
                    for call in message.tool_calls
                ]
            )
        )
        return self._ask("none"), True

    def _finalize(self, message: AssistantMessage, iterations: int, *, forced: bool) -> TurnResult:
        assert self.turn is not None
        text = message.text.strip()
        if not text:
            return self._partial_answer(iterations)
        if message.stop_reason == "length":
            text = f"{text}\n\n{t('agent.notice.answer_truncated')}"
        if self.deadline_reached or message.stop_reason == "length":
            if self.deadline_reached:
                text = f"{text}\n\n{t('agent.notice.deadline_reached')}"
            self.context.pop()  # remplace la réponse par sa version annotée
            item_id = self.append(
                replace(message, text=text), provider=message.provider, model=message.model
            )
        else:
            item_id = self._last_item_id()
        consulted = set(
            self.history.consulted_files(self.request.conversation_id, self.request.workdir)
        )
        sources = citations.verify(text, consulted, self.request.workdir)
        self.history.set_sources(item_id, sources)
        self.history.close_turn(self.turn.id, "complete")
        result = TurnResult(
            turn_id=self.turn.id,
            status="complete",
            answer=text,
            sources=list(sources),
            consulted=list(self.consulted),
            iterations=iterations,
        )
        self.publish(
            EventType.FINAL_ANSWER,
            item_id=item_id,
            text=text,
            sources=[
                {"path": s.rel_path, "verified": s.verified, "workdir": s.workdir} for s in sources
            ],
            model=self.request.model.id,
            forced=forced,
        )
        self.publish(EventType.TURN_ENDED, status="complete", iterations=iterations)
        return result

    def _last_item_id(self) -> int:
        assert self.turn is not None
        items = [
            item
            for item in self.history.items(self.request.conversation_id)
            if item.turn_id == self.turn.id
        ]
        return items[-1].id if items else 0

    def _partial_answer(self, iterations: int) -> TurnResult:
        """Réponse construite par le code : aucune affirmation tirée des documents (CDC §7)."""
        key = "agent.partial.deadline" if self.deadline_reached else "agent.partial.iterations"
        text = t(key, count=iterations)
        if self.consulted:
            text += "\n\n" + t("agent.partial.consulted") + "\n"
            text += "\n".join(f"- {path}" for path in self.consulted)
        else:
            text += "\n\n" + t("agent.partial.nothing")
        return self._stop_with_message(text, "partial", None, iterations=iterations)

    def _stop_with_message(
        self, text: str, status: TurnStatus, error_key: str | None, iterations: int = 0
    ) -> TurnResult:
        assert self.turn is not None
        item_id = self.append(interruption_item(text, self.consulted))
        self.history.close_turn(self.turn.id, status)
        self.publish(
            EventType.FINAL_ANSWER,
            item_id=item_id,
            text=text,
            sources=[],
            partial=True,
            model=self.request.model.id,
        )
        self.publish(EventType.TURN_ENDED, status=status, iterations=iterations)
        return TurnResult(
            turn_id=self.turn.id,
            status=status,
            answer=text,
            consulted=list(self.consulted),
            error_key=error_key,
            iterations=iterations,
        )

    def _interrupted(self) -> TurnResult:
        """Arrêt demandé : historique réparé, message construit par le code (CDC §7bis)."""
        if self.turn is None:
            raise Cancelled()
        self.history.repair_turn(
            self.turn, t("agent.synthetic.interrupted"), t("agent.interrupted.by_user")
        )
        self.publish(EventType.TURN_INTERRUPTED, consulted=list(self.consulted))
        self.publish(EventType.TURN_ENDED, status="interrupted")
        return TurnResult(
            turn_id=self.turn.id,
            status="interrupted",
            answer=t("agent.interrupted.by_user"),
            consulted=list(self.consulted),
        )

    def _provider_failure(self, error: ProviderError) -> TurnResult:
        _log.warning("Tour interrompu par une erreur du fournisseur : %s", error)
        self.publish(EventType.ERROR, key=error.key, message=error.message(), recoverable=False)
        if self.turn is None:
            raise error
        self.history.repair_turn(self.turn, t("agent.synthetic.interrupted"), error.message())
        self.publish(EventType.TURN_ENDED, status="interrupted")
        return TurnResult(
            turn_id=self.turn.id,
            status="interrupted",
            answer=error.message(),
            consulted=list(self.consulted),
            error_key=error.key,
        )

    # --- durée ----------------------------------------------------------------------

    def _remaining(self) -> float:
        return self.deadline - time.monotonic()

    def _out_of_time(self) -> bool:
        if self._remaining() <= 0 and not self.deadline_reached:
            _log.info("Durée maximale du tour atteinte")
            self.deadline_reached = True
            self.record_step("deadline")
        return self.deadline_reached


def _target_of(call: ToolCall) -> str:
    """Fichier ou dossier visé par l'appel, pour les libellés et les marqueurs."""
    for key in ("path", "keywords"):
        value = call.arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list) and value:
            return ", ".join(str(part) for part in value[:3])  # type: ignore[arg-type]
    return ""


class TurnRefused(AppError):
    """Refus d'état renvoyé à l'interface (tour déjà en cours, clé absente…)."""
