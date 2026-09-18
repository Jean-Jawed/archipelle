"""Objet exposé au JavaScript (docs/ARCHITECTURE.md §9).

Chaque méthode publique délègue à `agent/service.py` et renvoie du JSON simple. Deux
règles tenues ici : aucune clé API n'est jamais renvoyée en clair (CDC §9), et aucun refus
ne remonte sous forme d'exception — tout retour a la forme ``{"ok": …}``.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from archipelle.agent.history import Conversation, StoredItem
from archipelle.agent.service import AgentService
from archipelle.core import system
from archipelle.core.dirs import AppDirs
from archipelle.core.i18n import catalog as i18n_catalog
from archipelle.core.i18n import t
from archipelle.core.logging_setup import purge_logs, set_diagnostic
from archipelle.core.masking import mask_secret
from archipelle.core.outcome import Err, Outcome
from archipelle.persistence.settings_store import (
    CUSTOM_PROVIDER_ID,
    CustomModel,
    ModeModels,
    ScanExclusions,
    Settings,
)
from archipelle.providers.catalog import ExtraModel
from archipelle.providers.pivot import AssistantMessage, ToolResultGroup, UserMessage

_log = logging.getLogger(__name__)

type Json = dict[str, Any]


def ui_texts() -> Json:
    """Libellés de l'interface, envoyés au JS : aucune chaîne en dur côté web (CDC §10)."""
    return {key: value for key, value in i18n_catalog().items() if key.startswith("ui.")}


def ok(**payload: Any) -> Json:
    return {"ok": True, **payload}


def fail(key: str, **params: Any) -> Json:
    return {"ok": False, "key": key, "message": t(key, **params)}


def from_outcome(outcome: Outcome[Any], **payload: Any) -> Json:
    if isinstance(outcome, Err):
        return {"ok": False, "key": outcome.key, "message": outcome.message()}
    return ok(**payload)


class FolderChooser:
    """Dialogue natif de choix de dossier, injecté pour rester testable.

    La fenêtre est gardée dans un attribut privé : pywebview parcourt les attributs
    publics de l'objet exposé au JavaScript, et atteindrait sinon l'objet natif de la
    fenêtre puis son arbre d'accessibilité, jusqu'au dépassement de pile.
    """

    def __init__(self, window: Any = None) -> None:
        self._window = window

    def attach(self, window: Any) -> None:
        self._window = window

    @property
    def attached(self) -> bool:
        return self._window is not None

    def choose(self, start: str | None = None) -> str | None:
        if self._window is None:
            return None
        import webview  # import local : absent des tests

        result = cast(
            "tuple[str, ...] | None",
            self._window.create_file_dialog(webview.FOLDER_DIALOG, directory=start or ""),
        )
        return result[0] if result else None


class JsApi:
    def __init__(
        self,
        service: AgentService,
        chooser: FolderChooser | None = None,
        dirs: AppDirs | None = None,
    ) -> None:
        self.service = service
        self.chooser = chooser or FolderChooser()
        self.dirs = dirs

    # --- démarrage -------------------------------------------------------------------

    def bootstrap(self) -> Json:
        """Tout ce dont l'interface a besoin au lancement."""
        settings = self.service.settings
        return ok(
            settings=self._settings_json(settings),
            providers=self._providers_json(settings),
            conversations=self._conversations_json(),
            ocr_available=self.service.ocr_available,
            texts=ui_texts(),
            platform="windows" if system.IS_WINDOWS else "macos" if system.IS_MACOS else "linux",
        )

    def _settings_json(self, settings: Settings) -> Json:
        exclusions = settings.effective_scan_exclusions()
        return {
            "onboarding_done": settings.onboarding_done,
            "default_provider": settings.default_provider,
            "default_model": settings.default_model,
            "default_workdir": settings.default_workdir,
            "general_knowledge": settings.general_knowledge,
            "diagnostic": settings.diagnostic,
            "theme": settings.theme,
            "sidebar_collapsed": settings.sidebar_collapsed,
            "mode_models": {
                provider: {"quick": modes.quick, "explore": modes.explore}
                for provider, modes in settings.mode_models.items()
            },
            "extra_models": {
                provider: [self._model_json(m) for m in models]
                for provider, models in settings.extra_models.items()
            },
            "custom_provider": {
                "base_url": settings.custom_provider.base_url,
                "models": [self._model_json(m) for m in settings.custom_provider.models],
            },
            "scan_exclusions": {
                "dir_names": list(exclusions.dir_names),
                "file_patterns": list(exclusions.file_patterns),
                "hide_dotfiles": exclusions.hide_dotfiles,
            },
        }

    @staticmethod
    def _model_json(model: CustomModel) -> Json:
        return {
            "id": model.id,
            "context_window": model.context_window,
            "max_output": model.max_output,
        }

    def _providers_json(self, settings: Settings) -> list[Json]:
        providers: list[Json] = []
        for profile in self.service.catalog.providers.values():
            key = self.service.secrets.get(profile.id)
            extra = settings.extra_models.get(profile.id, [])
            models = self.service.catalog.selectable_models(
                profile.id,
                [ExtraModel(m.id, m.context_window, m.max_output) for m in extra],
            )
            providers.append(
                {
                    "id": profile.id,
                    "label": profile.label,
                    "region": profile.region,
                    "outside_eu": profile.outside_eu,
                    "is_custom": profile.is_custom,
                    "key_required": profile.key_required,
                    "masked_key": mask_secret(key) if key else None,
                    "defaults": dict(profile.defaults),
                    "models": [
                        {
                            "id": model.id,
                            "label": model.label,
                            "description": model.description,
                            "context_window": model.context_window,
                            "in_catalog": model.in_catalog,
                        }
                        for model in models
                    ],
                }
            )
        return providers

    # --- conversations ---------------------------------------------------------------

    def _conversations_json(self, query: str = "") -> list[Json]:
        history = self.service.history
        found = (
            history.search_conversations(query) if query.strip() else history.list_conversations()
        )
        return [
            {
                "id": c.id,
                "title": c.title or t("ui.conversation.untitled"),
                "updated_at": c.updated_at,
                "provider_id": c.provider_id,
                "model": c.model,
                "mode": c.mode,
            }
            for c in found
        ]

    def list_conversations(self, query: str = "") -> Json:
        return ok(conversations=self._conversations_json(query))

    def new_conversation(self) -> Json:
        conversation = self.service.new_conversation()
        return ok(conversation=self._conversation_json(conversation), items=[])

    def open_conversation(self, conversation_id: str) -> Json:
        conversation = self.service.history.get_conversation(conversation_id)
        if conversation is None:
            return fail("agent.errors.unknown_conversation")
        items = self.service.history.items(conversation_id)
        turns = {turn.id: turn for turn in self.service.history.turns(conversation_id)}
        last_of_turn = {item.turn_id: item.id for item in items}
        payload: list[Json] = []
        for item in items:
            entry = self._item_json(item)
            turn = turns.get(item.turn_id)
            if (
                turn is not None
                and turn.status != "in_progress"
                and last_of_turn[item.turn_id] == item.id
            ):
                entry["end"] = {
                    "status": turn.status,
                    "model": item.model or turn.restore_model or "",
                    "seconds": round((turn.ended_at or turn.started_at) - turn.started_at),
                }
            payload.append(entry)
        return ok(conversation=self._conversation_json(conversation), items=payload)

    def rename_conversation(self, conversation_id: str, title: str) -> Json:
        outcome = self.service.rename_conversation(conversation_id, title)
        if isinstance(outcome, Err):
            return {"ok": False, "key": outcome.key, "message": outcome.message()}
        return ok(
            conversation=self._conversation_json(outcome.value),
            conversations=self._conversations_json(),
        )

    def delete_conversation(self, conversation_id: str) -> Json:
        self.service.purge_history(conversation_id)
        return ok(conversations=self._conversations_json())

    def _conversation_json(self, conversation: Conversation) -> Json:
        profile = self.service.catalog.provider(conversation.provider_id)
        model = self.service.model_info(conversation.provider_id, conversation.model)
        return {
            "id": conversation.id,
            "title": conversation.title,
            "provider_id": conversation.provider_id,
            "provider_label": profile.label,
            "provider_locked": conversation.provider_locked,
            "model": conversation.model,
            "model_label": model.label,
            "mode": conversation.mode,
            "workdir": conversation.workdir,
            "scope_rel": conversation.scope_rel,
            "needs_key": profile.key_required and not self.service.secrets.get(profile.id),
        }

    def _item_json(self, item: StoredItem) -> Json:
        pivot = item.pivot()
        base: Json = {
            "id": item.id,
            "kind": item.kind,
            "turn_id": item.turn_id,
            "created_at": item.created_at,
        }
        if item.kind == "user" and isinstance(pivot, UserMessage):
            return {**base, "role": "user", "text": pivot.text}
        if item.kind == "assistant" and isinstance(pivot, AssistantMessage):
            return {
                **base,
                "role": "assistant",
                "text": pivot.text,
                "model": item.model,
                "provider": item.provider,
                "sources": [
                    {"path": s.rel_path, "verified": s.verified, "workdir": s.workdir}
                    for s in item.sources
                ],
                "tool_calls": [c.name for c in pivot.tool_calls],
            }
        if item.kind == "interruption":
            return {**base, "role": "notice", "text": item.text, "partial": True}
        if item.kind == "tool_results" and isinstance(pivot, ToolResultGroup):
            return {**base, "role": "tools", "count": len(pivot.results)}
        return {**base, "role": "hidden", "text": item.text}

    def turn_details(self, turn_id: str) -> Json:
        """Contenu de la zone de transparence d'un tour terminé (CDC §11)."""
        history = self.service.history
        return ok(
            steps=history.steps(turn_id),
            consulted=history.turn_consulted(turn_id),
            ignored=[
                {"path": e.rel_path, "reason": e.reason, "detail": e.detail,
                 "label": t(f"journal.reasons.{e.reason}")}
                for e in history.ignored_files(turn_id)
            ],
        )  # fmt: skip

    # --- réglages d'une conversation --------------------------------------------------

    def set_provider(self, conversation_id: str, provider_id: str) -> Json:
        return self._applied(self.service.set_provider(conversation_id, provider_id))

    def set_model(self, conversation_id: str, model_id: str) -> Json:
        return self._applied(self.service.set_model(conversation_id, model_id))

    def set_mode(self, conversation_id: str, mode: str) -> Json:
        if mode not in ("quick", "explore"):
            return fail("ui.errors.bad_mode")
        return self._applied(self.service.set_mode(conversation_id, mode))

    def set_scope(self, conversation_id: str, scope_rel: str | None) -> Json:
        return self._applied(self.service.set_scope(conversation_id, scope_rel or None))

    def choose_workdir(self, conversation_id: str) -> Json:
        conversation = self.service.history.get_conversation(conversation_id)
        chosen = self.chooser.choose(conversation.workdir if conversation else None)
        if chosen is None:
            return ok(cancelled=True)
        return self._applied(self.service.set_workdir(conversation_id, chosen))

    def choose_scope(self, conversation_id: str) -> Json:
        """Bouton « parcourir » : restreint la recherche à un sous-dossier (CDC §6)."""
        conversation = self.service.history.get_conversation(conversation_id)
        if conversation is None or not conversation.workdir:
            return fail("agent.errors.no_workdir")
        chosen = self.chooser.choose(conversation.workdir)
        if chosen is None:
            return ok(cancelled=True)
        return self._applied(self.service.set_scope_from_path(conversation_id, chosen))

    def _applied(self, outcome: Outcome[Any]) -> Json:
        if isinstance(outcome, Err):
            return {"ok": False, "key": outcome.key, "message": outcome.message()}
        conversation = cast(Conversation, outcome.value)
        return ok(conversation=self._conversation_json(conversation))

    # --- envoi -----------------------------------------------------------------------

    def send(self, conversation_id: str, question: str) -> Json:
        outcome = self.service.send(conversation_id, question)
        return self._run_json(outcome, conversation_id)

    def continue_deeper(self, conversation_id: str) -> Json:
        return self._run_json(self.service.continue_deeper(conversation_id), conversation_id)

    def _run_json(self, outcome: Outcome[Any], conversation_id: str) -> Json:
        if isinstance(outcome, Err):
            return {"ok": False, "key": outcome.key, "message": outcome.message()}
        conversation = self.service.history.get_conversation(conversation_id)
        return ok(
            run=dict(outcome.value.__dict__),
            conversation=self._conversation_json(conversation) if conversation else None,
        )

    def stop(self) -> Json:
        return from_outcome(self.service.stop())

    def open_source(self, conversation_id: str, rel_path: str) -> Json:
        return from_outcome(self.service.open_source(conversation_id, rel_path))

    # --- paramètres -------------------------------------------------------------------

    def save_key(self, provider_id: str, key: str) -> Json:
        cleaned = key.strip()
        if not cleaned:
            return fail("ui.errors.empty_key")
        self.service.secrets.set(provider_id, cleaned)
        return ok(masked_key=mask_secret(cleaned))

    def delete_key(self, provider_id: str) -> Json:
        self.service.delete_key(provider_id)
        return ok()

    def update_settings(self, patch: Json) -> Json:
        """Applique un sous-ensemble de réglages ; les clés inconnues sont ignorées."""
        try:
            self.service.settings_store.update(lambda s: _apply_patch(s, patch))
        except (ValueError, TypeError) as error:
            _log.warning("Réglage refusé : %s", error)
            return fail("ui.errors.bad_setting")
        settings = self.service.settings
        set_diagnostic(settings.diagnostic)
        return ok(settings=self._settings_json(settings), providers=self._providers_json(settings))

    def choose_default_workdir(self) -> Json:
        chosen = self.chooser.choose(self.service.settings.default_workdir)
        if chosen is None:
            return ok(cancelled=True)
        return self.update_settings({"default_workdir": chosen})

    def check_custom_url(self, url: str) -> Json:
        outcome = self.service.check_custom_url(url)
        if isinstance(outcome, Err):
            return {"ok": False, "key": outcome.key, "message": outcome.message()}
        return ok(**outcome.value)

    # --- maintenance ------------------------------------------------------------------

    def storage_info(self) -> Json:
        return ok(
            cache_bytes=self.service.cache_size(),
            conversations=len(self.service.history.list_conversations(limit=10_000)),
        )

    def purge_cache(self) -> Json:
        self.service.purge_cache()
        return ok(cache_bytes=self.service.cache_size())

    def purge_history(self) -> Json:
        self.service.purge_history()
        return ok(conversations=[])

    def purge_logs(self) -> Json:
        if self.dirs is not None:
            purge_logs(self.dirs)
        return ok()


def _apply_patch(settings: Settings, patch: Json) -> None:
    simple = {
        "onboarding_done": bool,
        "default_provider": str,
        "general_knowledge": bool,
        "diagnostic": bool,
        "sidebar_collapsed": bool,
    }
    for name, kind in simple.items():
        if name in patch:
            setattr(settings, name, kind(patch[name]))
    if "default_model" in patch:
        settings.default_model = str(patch["default_model"]) or None
    if "default_workdir" in patch:
        settings.default_workdir = str(patch["default_workdir"]) or None
    if "theme" in patch and patch["theme"] in ("light", "dark"):
        settings.theme = patch["theme"]
    if "mode_models" in patch:
        settings.mode_models = {
            str(provider): ModeModels(modes.get("quick") or None, modes.get("explore") or None)
            for provider, modes in cast(dict[str, Json], patch["mode_models"]).items()
        }
    if "extra_models" in patch:
        settings.extra_models = {
            str(provider): [_custom_model(m) for m in models]
            for provider, models in cast(dict[str, list[Json]], patch["extra_models"]).items()
        }
    if "custom_provider" in patch:
        custom = cast(Json, patch["custom_provider"])
        settings.custom_provider.base_url = str(custom.get("base_url") or "")
        settings.custom_provider.models = [
            _custom_model(m) for m in cast(list[Json], custom.get("models") or [])
        ]
    if "scan_exclusions" in patch:
        exclusions = cast(Json, patch["scan_exclusions"])
        settings.scan_exclusions = ScanExclusions(
            dir_names=[str(name) for name in cast(list[Any], exclusions.get("dir_names") or [])],
            file_patterns=[
                str(pattern) for pattern in cast(list[Any], exclusions.get("file_patterns") or [])
            ],
            hide_dotfiles=bool(exclusions.get("hide_dotfiles", True)),
        )
    if CUSTOM_PROVIDER_ID in patch.get("mode_models", {}):
        _log.debug("Modèles de mode définis pour le fournisseur personnalisé")


def _custom_model(raw: Json) -> CustomModel:
    identifier = str(raw.get("id") or "").strip()
    if not identifier:
        raise ValueError("modèle sans identifiant")
    return CustomModel(
        id=identifier,
        context_window=_positive(raw.get("context_window")),
        max_output=_positive(raw.get("max_output")),
    )


def _positive(value: Any) -> int | None:
    if value in (None, ""):
        return None
    number = int(value)
    if number <= 0:
        raise ValueError("valeur positive attendue")
    return number
