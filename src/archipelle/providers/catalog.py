"""Catalogue des modèles, embarqué avec l'application (CDC §13bis).

Aucun téléchargement : mettre à jour la liste des modèles demande seulement de modifier
``resources/models_catalog.json``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cache
from typing import Any, Literal, cast

from archipelle.core import resources

type ApiFamily = Literal["openai_compat", "mistral", "anthropic"]
type Region = Literal["eu", "us", "cn", "custom"]
type ReasoningEcho = Literal["none", "always", "same_model"]
type ModeName = Literal["quick", "explore"]

CUSTOM_PROVIDER_ID = "custom"


class CatalogError(ValueError):
    pass


@dataclass(frozen=True)
class ModelInfo:
    id: str
    label: str
    description: str
    context_window: int  # fenêtre réelle du modèle
    max_output: int  # taille de réponse demandée et réservée (CDC §7ter)
    tools: bool
    in_catalog: bool = True
    max_context_tokens: int | None = None  # plafond appliqué par Archipelle 🔧

    @property
    def usable_context(self) -> int:
        """Fenêtre retenue pour le budget de contexte."""
        if self.max_context_tokens is None:
            return self.context_window
        return min(self.context_window, self.max_context_tokens)


@dataclass(frozen=True)
class ProviderProfile:
    id: str
    label: str
    region: Region
    api: ApiFamily
    base_url: str | None
    key_required: bool
    reasoning_echo: ReasoningEcho
    max_tokens_param: str = "max_tokens"
    stream_usage: bool = True
    defaults: dict[str, str | None] = field(default_factory=dict[str, str | None])
    models: list[ModelInfo] = field(default_factory=list[ModelInfo])

    @property
    def is_custom(self) -> bool:
        return self.id == CUSTOM_PROVIDER_ID

    @property
    def outside_eu(self) -> bool:
        return self.region in ("us", "cn")

    def default_model(self, mode: ModeName) -> str | None:
        return self.defaults.get(mode)


@dataclass(frozen=True)
class ExtraModel:
    """Modèle hors catalogue saisi par l'utilisateur."""

    id: str
    context_window: int | None = None
    max_output: int | None = None


@dataclass(frozen=True)
class Catalog:
    providers: dict[str, ProviderProfile]
    fallback_context: int
    fallback_output: int
    max_context_tokens: int | None
    verified_on: str = ""

    def provider(self, provider_id: str) -> ProviderProfile:
        try:
            return self.providers[provider_id]
        except KeyError as exc:
            raise CatalogError(f"fournisseur inconnu : {provider_id}") from exc

    def selectable_models(
        self, provider_id: str, extra: list[ExtraModel] | None = None
    ) -> list[ModelInfo]:
        """Modèles proposés dans les sélecteurs : ceux qui gèrent les outils, puis les
        modèles saisis librement (outils supposés, CDC §13bis)."""
        profile = self.provider(provider_id)
        models = [m for m in profile.models if m.tools]
        known = {m.id for m in profile.models}
        for model in extra or []:
            if model.id not in known:
                models.append(self._custom(model))
                known.add(model.id)
        return models

    def model(
        self, provider_id: str, model_id: str, extra: list[ExtraModel] | None = None
    ) -> ModelInfo:
        """Informations d'un modèle ; valeurs prudentes pour un modèle hors catalogue."""
        profile = self.provider(provider_id)
        for model in profile.models:
            if model.id == model_id:
                return model
        for model in extra or []:
            if model.id == model_id:
                return self._custom(model)
        return self._custom(ExtraModel(model_id))

    def _custom(self, model: ExtraModel) -> ModelInfo:
        return ModelInfo(
            id=model.id,
            label=model.id,
            description="",
            context_window=model.context_window or self.fallback_context,
            max_output=model.max_output or self.fallback_output,
            tools=True,
            in_catalog=False,
            max_context_tokens=self.max_context_tokens,
        )


# --- lecture -------------------------------------------------------------------------

_APIS = ("openai_compat", "mistral", "anthropic")
_REGIONS = ("eu", "us", "cn", "custom")
_ECHOES = ("none", "always", "same_model")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CatalogError(message)


def _positive_int(value: Any, where: str) -> int:
    _require(isinstance(value, int) and not isinstance(value, bool) and value > 0, where)
    return cast(int, value)


def parse_catalog(data: dict[str, Any]) -> Catalog:
    fallback = cast(dict[str, Any], data.get("fallback") or {})
    limits = cast(dict[str, Any], data.get("limits") or {})
    cap_raw = limits.get("max_context_tokens")
    cap = _positive_int(cap_raw, "limits.max_context_tokens") if cap_raw is not None else None
    providers: dict[str, ProviderProfile] = {}
    raw_providers = cast(dict[str, Any], data.get("providers") or {})
    _require(bool(raw_providers), "aucun fournisseur")
    for provider_id, raw_value in raw_providers.items():
        raw = cast(dict[str, Any], raw_value)
        where = f"providers.{provider_id}"
        _require(raw.get("api") in _APIS, f"{where}.api")
        _require(raw.get("region") in _REGIONS, f"{where}.region")
        _require(raw.get("reasoning_echo") in _ECHOES, f"{where}.reasoning_echo")
        models: list[ModelInfo] = []
        seen: set[str] = set()
        for index, model_value in enumerate(cast(list[Any], raw.get("models") or [])):
            model = cast(dict[str, Any], model_value)
            mwhere = f"{where}.models[{index}]"
            model_id = model.get("id")
            _require(isinstance(model_id, str) and bool(model_id), f"{mwhere}.id")
            _require(model_id not in seen, f"{mwhere}.id en double")
            seen.add(cast(str, model_id))
            models.append(
                ModelInfo(
                    id=cast(str, model_id),
                    label=str(model.get("label") or model_id),
                    description=str(model.get("description") or ""),
                    context_window=_positive_int(
                        model.get("context_window"), f"{mwhere}.context_window"
                    ),
                    max_output=_positive_int(model.get("max_output"), f"{mwhere}.max_output"),
                    tools=bool(model.get("tools", True)),
                    max_context_tokens=cap,
                )
            )
        defaults = cast(dict[str, str | None], raw.get("defaults") or {})
        for mode in ("quick", "explore"):
            default = defaults.get(mode)
            _require(default is None or default in seen, f"{where}.defaults.{mode}")
        providers[provider_id] = ProviderProfile(
            id=provider_id,
            label=str(raw.get("label") or provider_id),
            region=cast(Region, raw["region"]),
            api=cast(ApiFamily, raw["api"]),
            base_url=cast(str | None, raw.get("base_url")),
            key_required=bool(raw.get("key_required", True)),
            reasoning_echo=cast(ReasoningEcho, raw["reasoning_echo"]),
            max_tokens_param=str(raw.get("max_tokens_param") or "max_tokens"),
            stream_usage=bool(raw.get("stream_usage", True)),
            defaults={"quick": defaults.get("quick"), "explore": defaults.get("explore")},
            models=models,
        )
    return Catalog(
        providers=providers,
        fallback_context=_positive_int(fallback.get("context_window", 32000), "fallback"),
        fallback_output=_positive_int(fallback.get("max_output", 4000), "fallback"),
        max_context_tokens=cap,
        verified_on=str(data.get("verified_on") or ""),
    )


@cache
def load_catalog() -> Catalog:
    return parse_catalog(cast(dict[str, Any], resources.load_json("models_catalog.json")))
