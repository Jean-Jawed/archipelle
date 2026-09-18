from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from archipelle.core import masking
from archipelle.core.dirs import HOME_OVERRIDE_ENV, AppDirs, resolve_dirs


@pytest.fixture
def app_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppDirs:
    """Répertoires de l'application isolés dans un dossier temporaire."""
    monkeypatch.setenv(HOME_OVERRIDE_ENV, str(tmp_path / "home"))
    return resolve_dirs().ensure()


@pytest.fixture(autouse=True)
def _clean_secret_registry() -> Iterator[None]:
    yield
    with masking._lock:  # pyright: ignore[reportPrivateUsage]
        masking._known.clear()  # pyright: ignore[reportPrivateUsage]
