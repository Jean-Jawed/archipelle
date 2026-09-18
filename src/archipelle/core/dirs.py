"""Répertoires de l'application (configuration, données, cache, logs).

Obtenus via ``platformdirs``, sans condition par OS dans le code (CDC §4bis, §12).
La variable d'environnement ``ARCHIPELLE_HOME`` regroupe tous les répertoires sous un
même dossier : elle sert aux tests et au débogage.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import PlatformDirs

from archipelle import APP_NAME

HOME_OVERRIDE_ENV = "ARCHIPELLE_HOME"


@dataclass(frozen=True)
class AppDirs:
    config: Path
    data: Path
    cache: Path
    logs: Path

    def ensure(self) -> AppDirs:
        for directory in (self.config, self.data, self.cache, self.logs):
            directory.mkdir(parents=True, exist_ok=True)
        return self

    @property
    def settings_file(self) -> Path:
        return self.config / "config.json"

    @property
    def secrets_file(self) -> Path:
        return self.config / "secrets.json"

    @property
    def history_db(self) -> Path:
        return self.data / "archipelle.db"

    @property
    def cache_index_db(self) -> Path:
        return self.cache / "index.db"

    @property
    def cache_docs(self) -> Path:
        return self.cache / "docs"

    @property
    def log_file(self) -> Path:
        return self.logs / "archipelle.log"


def resolve_dirs() -> AppDirs:
    override = os.environ.get(HOME_OVERRIDE_ENV)
    if override:
        base = Path(override).expanduser().resolve()
        return AppDirs(
            config=base / "config",
            data=base / "data",
            cache=base / "cache",
            logs=base / "logs",
        )
    platform_dirs = PlatformDirs(appname=APP_NAME, appauthor=False)
    return AppDirs(
        config=Path(platform_dirs.user_config_dir),
        data=Path(platform_dirs.user_data_dir),
        cache=Path(platform_dirs.user_cache_dir),
        logs=Path(platform_dirs.user_log_dir),
    )
