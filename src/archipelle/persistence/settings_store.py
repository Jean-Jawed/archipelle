"""Préférences utilisateur (``config.json``) et clés API (``secrets.json``) — CDC §12.

Les deux fichiers sont séparés : la purge des clés ne touche pas aux préférences, et le
fichier des clés reçoit des permissions restreintes. Aucune clé n'est chiffrée en V1.
"""

from __future__ import annotations

import copy
import logging
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from archipelle.core import masking, resources
from archipelle.persistence.files import read_json_object, write_json_atomic

_log = logging.getLogger(__name__)

type Mode = Literal["quick", "explore"]
type Theme = Literal["light", "dark"]

DEFAULT_PROVIDER = "mistral"
CUSTOM_PROVIDER_ID = "custom"


@dataclass
class ScanExclusions:
    """Exclusions du relevé d'arborescence (CDC §3)."""

    dir_names: list[str]
    file_patterns: list[str]
    hide_dotfiles: bool = True

    @classmethod
    def defaults(cls) -> ScanExclusions:
        data = resources.load_json("scan_exclusions.json")
        return cls(
            dir_names=list(data["dir_names"]),
            file_patterns=list(data["file_patterns"]),
            hide_dotfiles=bool(data["hide_dotfiles"]),
        )


@dataclass
class ModeModels:
    quick: str | None = None
    explore: str | None = None

    def for_mode(self, mode: Mode) -> str | None:
        return self.quick if mode == "quick" else self.explore


@dataclass
class CustomModel:
    """Modèle saisi librement (fournisseur personnalisé ou modèle hors catalogue)."""

    id: str
    context_window: int | None = None
    max_output: int | None = None


@dataclass
class CustomProviderSettings:
    """Fournisseur « Compatible OpenAI » à adresse personnalisée (CDC §13)."""

    base_url: str = ""
    models: list[CustomModel] = field(default_factory=list[CustomModel])


@dataclass
class Settings:
    onboarding_done: bool = False
    default_provider: str = DEFAULT_PROVIDER
    default_model: str | None = None
    mode_models: dict[str, ModeModels] = field(default_factory=dict[str, ModeModels])
    extra_models: dict[str, list[CustomModel]] = field(default_factory=dict[str, list[CustomModel]])
    default_workdir: str | None = None
    general_knowledge: bool = False
    diagnostic: bool = False
    theme: Theme = "light"
    sidebar_collapsed: bool = False
    scan_exclusions: ScanExclusions | None = None  # None : valeurs par défaut
    custom_provider: CustomProviderSettings = field(default_factory=CustomProviderSettings)

    def effective_scan_exclusions(self) -> ScanExclusions:
        return (
            copy.deepcopy(self.scan_exclusions)
            if self.scan_exclusions
            else ScanExclusions.defaults()
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Settings:
        """Lecture tolérante : toute valeur absente ou invalide reprend sa valeur par défaut."""
        base = cls()
        reader = _Reader(data)
        return cls(
            onboarding_done=reader.boolean("onboarding_done", base.onboarding_done),
            default_provider=reader.string("default_provider") or base.default_provider,
            default_model=reader.string("default_model"),
            mode_models=reader.mode_models("mode_models"),
            extra_models=reader.extra_models("extra_models"),
            default_workdir=reader.string("default_workdir"),
            general_knowledge=reader.boolean("general_knowledge", base.general_knowledge),
            diagnostic=reader.boolean("diagnostic", base.diagnostic),
            theme="dark" if reader.string("theme") == "dark" else "light",
            sidebar_collapsed=reader.boolean("sidebar_collapsed", base.sidebar_collapsed),
            scan_exclusions=reader.scan_exclusions("scan_exclusions"),
            custom_provider=reader.custom_provider("custom_provider"),
        )


def _as_dict(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        entries = cast(dict[Any, Any], value)
        return {key: item for key, item in entries.items() if isinstance(key, str)}
    return None


def _as_list(value: Any) -> list[Any] | None:
    return cast(list[Any], value) if isinstance(value, list) else None


class _Reader:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    def _warn(self, key: str) -> None:
        _log.warning("Réglage %s invalide, valeur par défaut utilisée", key)

    def boolean(self, key: str, default: bool) -> bool:
        value = self.data.get(key, default)
        if isinstance(value, bool):
            return value
        self._warn(key)
        return default

    def string(self, key: str, source: dict[str, Any] | None = None) -> str | None:
        value = (self.data if source is None else source).get(key)
        if isinstance(value, str):
            return value.strip() or None
        if value is not None:
            self._warn(key)
        return None

    def mode_models(self, key: str) -> dict[str, ModeModels]:
        raw = _as_dict(self.data.get(key) or {})
        if raw is None:
            self._warn(key)
            return {}
        result: dict[str, ModeModels] = {}
        for provider, value in raw.items():
            entry = _as_dict(value)
            if entry is not None:
                result[provider] = ModeModels(
                    quick=self.string("quick", entry), explore=self.string("explore", entry)
                )
        return result

    def _models(self, value: Any) -> list[CustomModel]:
        models: list[CustomModel] = []
        for item in _as_list(value) or []:
            entry = _as_dict(item)
            model_id = self.string("id", entry) if entry is not None else None
            if entry is None or not model_id:
                continue
            models.append(
                CustomModel(
                    id=model_id,
                    context_window=_positive_int(entry.get("context_window")),
                    max_output=_positive_int(entry.get("max_output")),
                )
            )
        return models

    def extra_models(self, key: str) -> dict[str, list[CustomModel]]:
        raw = _as_dict(self.data.get(key) or {})
        if raw is None:
            self._warn(key)
            return {}
        return {provider: self._models(entries) for provider, entries in raw.items()}

    def scan_exclusions(self, key: str) -> ScanExclusions | None:
        if self.data.get(key) is None:
            return None
        raw = _as_dict(self.data.get(key))
        dir_names = _as_list(raw.get("dir_names")) if raw is not None else None
        file_patterns = _as_list(raw.get("file_patterns")) if raw is not None else None
        if raw is None or dir_names is None or file_patterns is None:
            self._warn(key)
            return None
        return ScanExclusions(
            dir_names=[str(v).strip() for v in dir_names if str(v).strip()],
            file_patterns=[str(v) for v in file_patterns if str(v).strip()],
            hide_dotfiles=raw.get("hide_dotfiles", True) is not False,
        )

    def custom_provider(self, key: str) -> CustomProviderSettings:
        raw = _as_dict(self.data.get(key))
        if raw is None:
            return CustomProviderSettings()
        return CustomProviderSettings(
            base_url=self.string("base_url", raw) or "",
            models=self._models(raw.get("models")),
        )


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


class SettingsStore:
    """Préférences en mémoire, persistées à chaque modification. Accès thread-safe."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        data = read_json_object(path)
        self._settings = Settings.from_dict(data) if data is not None else Settings()

    def get(self) -> Settings:
        with self._lock:
            return copy.deepcopy(self._settings)

    def update(self, change: Callable[[Settings], None]) -> Settings:
        """Applique ``change`` sur une copie, l'enregistre, puis la rend effective."""
        with self._lock:
            candidate = copy.deepcopy(self._settings)
            change(candidate)
            write_json_atomic(self.path, candidate.to_dict())
            self._settings = candidate
            return copy.deepcopy(candidate)


class SecretsStore:
    """Clés API par fournisseur. Aucune méthode ne sert de clé en clair à l'interface :
    la façade n'expose que ``masked()`` et ``has_key()``."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        data = read_json_object(path) or {}
        self._keys: dict[str, str] = {}
        for provider, key in (_as_dict(data.get("keys")) or {}).items():
            if isinstance(key, str) and key.strip():
                self._keys[provider] = key.strip()
        for key in self._keys.values():
            masking.register_secret(key)

    def get(self, provider: str) -> str | None:
        with self._lock:
            return self._keys.get(provider)

    def has_key(self, provider: str) -> bool:
        with self._lock:
            return provider in self._keys

    def masked(self, provider: str) -> str | None:
        with self._lock:
            key = self._keys.get(provider)
        return masking.mask_secret(key) if key else None

    def providers_with_key(self) -> list[str]:
        with self._lock:
            return sorted(self._keys)

    def set(self, provider: str, key: str) -> None:
        key = key.strip()
        if not key:
            self.delete(provider)
            return
        # Une clé remplacée ou supprimée reste masquée dans les logs de la session.
        masking.register_secret(key)
        with self._lock:
            self._keys[provider] = key
            self._save()

    def delete(self, provider: str) -> None:
        with self._lock:
            self._keys.pop(provider, None)
            self._save()

    def purge_all(self) -> None:
        with self._lock:
            self._keys.clear()
            self._save()

    def _save(self) -> None:
        write_json_atomic(self.path, {"keys": dict(self._keys)}, private=True)
