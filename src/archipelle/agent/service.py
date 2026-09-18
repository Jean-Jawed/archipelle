"""Façade de l'orchestration (docs/ARCHITECTURE.md §7.1).

Seule surface visible par l'interface. Elle garde l'invariant du CDC §7bis : une seule
question traitée à la fois, dans un thread worker, les réglages de la conversation en
cours de traitement étant verrouillés. Tous les refus d'état sont renvoyés comme
résultats typés, jamais comme exceptions.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from archipelle.agent.history import Conversation, History, Mode, new_id
from archipelle.agent.loop import LoopLimits, TurnRequest, TurnResult, TurnRunner
from archipelle.agent.prompts import mode_changed, scope_changed, workdir_changed
from archipelle.cache.store import ExtractionCache
from archipelle.core.cancel import CancelToken
from archipelle.core.events import Event, EventType
from archipelle.core.outcome import AppError, Err, Ok, Outcome
from archipelle.persistence.settings_store import (
    CUSTOM_PROVIDER_ID,
    SecretsStore,
    Settings,
    SettingsStore,
)
from archipelle.providers.base import Provider
from archipelle.providers.catalog import Catalog, ExtraModel, ModelInfo, load_catalog
from archipelle.providers.factory import (
    ProviderConfig,
    create_provider,
    is_cleartext_remote,
    is_local_url,
    validate_base_url,
)
from archipelle.providers.pivot import SystemNotice
from archipelle.tools.context import ToolContext, ToolLimits
from archipelle.tools.documents import DocumentAccess
from archipelle.tools.open_source import open_source
from archipelle.tools.paths import Workspace, to_relative
from archipelle.tools.process_pool import ExtractionPool

_log = logging.getLogger(__name__)

type EventSink = Callable[[Event], None]
type ProviderBuilder = Callable[[ProviderConfig], Provider]


@dataclass(frozen=True)
class RunHandle:
    run_id: str
    conversation_id: str
    mode: Mode
    model: str


@dataclass
class _ActiveTurn:
    handle: RunHandle
    cancel: CancelToken
    thread: threading.Thread
    restore_mode: Mode | None = None
    restore_model: str | None = None


@dataclass
class AgentService:
    history: History
    settings_store: SettingsStore
    secrets: SecretsStore
    cache: ExtractionCache
    pool: ExtractionPool
    documents: DocumentAccess
    publish: EventSink
    ocr_available: bool = True
    require_keys: bool = True  # mis à False en mode démo (aucun fournisseur réel appelé)
    catalog: Catalog = field(default_factory=load_catalog)
    limits: LoopLimits = field(default_factory=LoopLimits)
    tool_limits: ToolLimits = field(default_factory=ToolLimits)
    build_provider: ProviderBuilder = create_provider
    _active: _ActiveTurn | None = field(default=None, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    last_result: TurnResult | None = field(default=None, init=False)

    # --- état ------------------------------------------------------------------------

    @property
    def settings(self) -> Settings:
        return self.settings_store.get()

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._active is not None

    def active_run(self) -> RunHandle | None:
        with self._lock:
            return self._active.handle if self._active else None

    def startup(self) -> list[str]:
        """Réparation des tours laissés incomplets par un plantage (CDC §7bis)."""
        repaired = self.history.repair_unfinished()
        if repaired:
            self.publish(
                Event("", "", EventType.APP_NOTICE, {"key": "agent.interrupted.app_stopped"})
            )
        return repaired

    # --- conversations ---------------------------------------------------------------

    def new_conversation(self, workdir: str | None = None) -> Conversation:
        settings = self.settings
        provider_id = settings.default_provider
        mode: Mode = "quick"
        model = settings.default_model or self.model_for(provider_id, mode)
        return self.history.create_conversation(
            provider_id,
            model,
            mode=mode,
            workdir=workdir or settings.default_workdir,
        )

    def model_for(self, provider_id: str, mode: Mode) -> str:
        """Modèle associé à un mode : réglages de l'utilisateur, sinon catalogue (CDC §13bis)."""
        configured = self.settings.mode_models.get(provider_id)
        chosen = configured.for_mode(mode) if configured else None
        if chosen:
            return chosen
        profile = self.catalog.provider(provider_id)
        return profile.default_model(mode) or ""

    def model_info(self, provider_id: str, model_id: str) -> ModelInfo:
        extra = [
            ExtraModel(m.id, m.context_window, m.max_output)
            for m in self.settings.extra_models.get(provider_id, [])
        ]
        if provider_id == CUSTOM_PROVIDER_ID:
            extra += [
                ExtraModel(m.id, m.context_window, m.max_output)
                for m in self.settings.custom_provider.models
            ]
        return self.catalog.model(provider_id, model_id, extra)

    def rename_conversation(self, conversation_id: str, title: str) -> Outcome[Conversation]:
        cleaned = " ".join(title.split())[:120]
        if not cleaned:
            return Err("agent.errors.empty_title")
        if self.history.get_conversation(conversation_id) is None:
            return Err("agent.errors.unknown_conversation")
        self.history.update_conversation(conversation_id, title=cleaned)
        return self._reloaded(conversation_id)

    def set_provider(self, conversation_id: str, provider_id: str) -> Outcome[Conversation]:
        conversation = self.history.get_conversation(conversation_id)
        if conversation is None:
            return Err("agent.errors.unknown_conversation")
        if refusal := self._locked(conversation_id):
            return refusal
        if conversation.provider_locked:
            return Err("agent.errors.provider_locked")
        model = self.model_for(provider_id, conversation.mode)
        self.history.update_conversation(conversation_id, provider_id=provider_id, model=model)
        return self._reloaded(conversation_id)

    def set_model(self, conversation_id: str, model_id: str) -> Outcome[Conversation]:
        if refusal := self._locked(conversation_id):
            return refusal
        self.history.update_conversation(conversation_id, model=model_id)
        return self._reloaded(conversation_id)

    def set_mode(self, conversation_id: str, mode: Mode) -> Outcome[Conversation]:
        """Bascule Rapide/Exploration : le modèle du mode remplace celui en cours (CDC §13)."""
        conversation = self.history.get_conversation(conversation_id)
        if conversation is None:
            return Err("agent.errors.unknown_conversation")
        if refusal := self._locked(conversation_id):
            return refusal
        self.history.update_conversation(
            conversation_id, mode=mode, model=self.model_for(conversation.provider_id, mode)
        )
        self._notice(conversation_id, mode_changed(mode))
        return self._reloaded(conversation_id)

    def set_workdir(self, conversation_id: str, workdir: str) -> Outcome[Conversation]:
        if refusal := self._locked(conversation_id):
            return refusal
        try:
            workspace = Workspace.open(workdir)
        except AppError as error:
            return Err.from_error(error)
        self.history.update_conversation(
            conversation_id, workdir=str(workspace.root), scope_rel=None
        )
        self._notice(conversation_id, workdir_changed(str(workspace.root)))
        return self._reloaded(conversation_id)

    def set_scope(self, conversation_id: str, scope_rel: str | None) -> Outcome[Conversation]:
        """Restriction « parcourir », maintenue jusqu'à son retrait (CDC §6)."""
        conversation = self.history.get_conversation(conversation_id)
        if conversation is None:
            return Err("agent.errors.unknown_conversation")
        if refusal := self._locked(conversation_id):
            return refusal
        if scope_rel and conversation.workdir:
            try:
                Workspace.open(conversation.workdir, scope_rel)
            except AppError as error:
                return Err.from_error(error)
        self.history.update_conversation(conversation_id, scope_rel=scope_rel)
        self._notice(conversation_id, scope_changed(scope_rel))
        return self._reloaded(conversation_id)

    def _notice(self, conversation_id: str, notice: SystemNotice) -> None:
        """Message de l'application, rattaché au prochain tour (enregistré à son ouverture)."""
        self._pending.setdefault(conversation_id, []).append(notice)

    _pending: dict[str, list[SystemNotice]] = field(
        default_factory=dict[str, list[SystemNotice]], init=False
    )

    def _reloaded(self, conversation_id: str) -> Outcome[Conversation]:
        conversation = self.history.get_conversation(conversation_id)
        return Ok(conversation) if conversation else Err("agent.errors.unknown_conversation")

    def _locked(self, conversation_id: str) -> Err | None:
        handle = self.active_run()
        if handle is not None and handle.conversation_id == conversation_id:
            return Err("agent.errors.turn_in_progress")
        return None

    # --- envoi d'une question --------------------------------------------------------

    def send(self, conversation_id: str, question: str) -> Outcome[RunHandle]:
        return self._start(conversation_id, question, deeper=False)

    def continue_deeper(self, conversation_id: str) -> Outcome[RunHandle]:
        """« Continuer / creuser plus » : un tour en Exploration, puis retour à l'état
        antérieur, quelle que soit l'issue (CDC §7)."""
        return self._start(conversation_id, "", deeper=True)

    def _start(self, conversation_id: str, question: str, *, deeper: bool) -> Outcome[RunHandle]:
        text = question.strip()
        if not deeper and not text:
            return Err("agent.errors.empty_question")
        conversation = self.history.get_conversation(conversation_id)
        if conversation is None:
            return Err("agent.errors.unknown_conversation")
        if not conversation.workdir:
            return Err("agent.errors.no_workdir")
        with self._lock:
            if self._active is not None:
                return Err("agent.errors.busy")
            prepared = self._prepare(conversation, text, deeper=deeper)
            if isinstance(prepared, Err):
                return prepared
            request, restore = prepared.value
            cancel = CancelToken()
            handle = RunHandle(request.run_id, conversation_id, request.mode, request.model.id)
            thread = threading.Thread(
                target=self._run,
                args=(request, cancel, handle, restore),
                name=f"turn-{request.run_id[:8]}",
                daemon=True,
            )
            self._active = _ActiveTurn(handle, cancel, thread, *restore)
        thread.start()
        return Ok(handle)

    def _prepare(
        self, conversation: Conversation, question: str, *, deeper: bool
    ) -> Outcome[tuple[TurnRequest, tuple[Mode | None, str | None]]]:
        provider_id = conversation.provider_id
        profile = self.catalog.provider(provider_id)
        key = self.secrets.get(provider_id)
        if profile.key_required and self.require_keys and not key:
            return Err("agent.errors.missing_key", {"provider": profile.label})
        mode: Mode = "explore" if deeper else conversation.mode
        model_id = self.model_for(provider_id, mode) if deeper else conversation.model
        restore = (conversation.mode, conversation.model) if deeper else (None, None)
        base_url = (
            self.settings.custom_provider.base_url if provider_id == CUSTOM_PROVIDER_ID else None
        )
        try:
            provider = self.build_provider(ProviderConfig(profile, key, base_url))
        except AppError as error:
            return Err.from_error(error)
        text = question or self._continue_question(conversation.id)
        request = TurnRequest(
            conversation_id=conversation.id,
            run_id=new_id(),
            mode=mode,
            question=text,
            workdir=conversation.workdir or "",
            scope_rel=conversation.scope_rel,
            provider=provider,
            provider_id=provider_id,
            model=self.model_info(provider_id, model_id),
            general_knowledge=self.settings.general_knowledge,
            restore_mode=restore[0],
            restore_model=restore[1],
        )
        if not conversation.provider_locked:
            self.history.update_conversation(conversation.id, provider_locked=True)
        return Ok((request, restore))

    @staticmethod
    def _continue_question(conversation_id: str) -> str:
        del conversation_id
        from archipelle.core.i18n import t

        return t("agent.continue_question")

    def _run(
        self,
        request: TurnRequest,
        cancel: CancelToken,
        handle: RunHandle,
        restore: tuple[Mode | None, str | None],
    ) -> None:
        try:
            runner = TurnRunner(
                request,
                history=self.history,
                tool_context=self._tool_context(request, cancel),
                cancel=cancel,
                publish=self.publish,
                limits=self.limits,
                ocr_available=self.ocr_available,
            )
            runner.pending_notices.extend(self._pending.pop(request.conversation_id, []))
            self.last_result = runner.run()
        except Exception:
            _log.exception("Échec inattendu du tour %s", handle.run_id)
            self.publish(
                Event(
                    handle.run_id,
                    handle.conversation_id,
                    EventType.ERROR,
                    {"key": "errors.unexpected", "recoverable": False},
                )
            )
        finally:
            mode, model = restore
            if mode is not None and model is not None:
                self.history.update_conversation(request.conversation_id, mode=mode, model=model)
            with self._lock:
                self._active = None

    def _tool_context(self, request: TurnRequest, cancel: CancelToken) -> ToolContext:
        workspace = Workspace.open(request.workdir, request.scope_rel)
        return ToolContext(
            workspace=workspace,
            mode=request.mode,
            ocr=self.documents.ocr,
            cancel=cancel,
            turn_deadline=time.monotonic() + self.limits.seconds(request.mode),
            pool=self.pool,
            documents=self.documents,
            exclusions=self.settings.effective_scan_exclusions(),
            limits=self.tool_limits,
            progress=lambda key, params: self.publish(
                Event(request.run_id, request.conversation_id, EventType.STEP_PROGRESS,
                      {"key": key, **params})
            ),
        )  # fmt: skip

    def set_scope_from_path(self, conversation_id: str, absolute: str) -> Outcome[Conversation]:
        """Restriction choisie par le dialogue natif : le chemin absolu est rapporté au
        dossier de travail, et refusé s'il en sort (CDC §6)."""
        conversation = self.history.get_conversation(conversation_id)
        if conversation is None or not conversation.workdir:
            return Err("agent.errors.no_workdir")
        try:
            workspace = Workspace.open(conversation.workdir)
            relative = to_relative(workspace, Path(absolute).resolve())
        except AppError as error:
            return Err.from_error(error)
        return self.set_scope(conversation_id, None if relative in ("", ".") else relative)

    def check_custom_url(self, url: str) -> Outcome[dict[str, bool]]:
        """Contrôle de l'adresse d'un serveur compatible OpenAI (CDC §13)."""
        if validate_base_url(url) is not None:
            return Err("ui.errors.bad_url")
        return Ok({"local": is_local_url(url), "cleartext_remote": is_cleartext_remote(url)})

    def stop(self) -> Outcome[None]:
        with self._lock:
            active = self._active
        if active is None:
            return Err("agent.errors.nothing_to_stop")
        active.cancel.cancel()
        return Ok(None)

    def wait(self, timeout: float = 30.0) -> bool:
        """Attend la fin du tour en cours (tests, fermeture de l'application)."""
        with self._lock:
            active = self._active
        if active is None:
            return True
        active.thread.join(timeout)
        return not active.thread.is_alive()

    # --- sources et purges -----------------------------------------------------------

    def open_source(self, conversation_id: str, rel_path: str) -> Outcome[None]:
        conversation = self.history.get_conversation(conversation_id)
        if conversation is None or not conversation.workdir:
            return Err("agent.errors.unknown_conversation")
        verified = rel_path in self.history.consulted_files(conversation_id, conversation.workdir)
        return open_source(conversation.workdir, rel_path, verified=verified)

    def purge_history(self, conversation_id: str | None = None) -> None:
        if conversation_id is None:
            self.history.delete_all_conversations()
        else:
            self.history.delete_conversation(conversation_id)

    def purge_cache(self) -> None:
        self.cache.purge()

    def cache_size(self) -> int:
        return self.cache.size_bytes()

    def delete_key(self, provider_id: str) -> None:
        self.secrets.delete(provider_id)

    def shutdown(self) -> None:
        self.stop()
        self.wait(10)
        self.documents.shutdown()
        self.pool.shutdown()


def default_paths(home: Path) -> tuple[Path, Path]:
    return home / "data" / "archipelle.db", home / "cache"
