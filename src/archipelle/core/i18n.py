"""Traductions de l'interface et des messages (CDC §10 : aucune chaîne en dur).

Les libellés sont rangés dans ``resources/i18n/<langue>.json``, sous forme d'objets
imbriqués. Une clé s'écrit en notation pointée : ``errors.path.absolute``.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, cast

from archipelle.core import resources

DEFAULT_LANGUAGE = "fr"

_log = logging.getLogger(__name__)
_lock = threading.Lock()
_catalogs: dict[str, dict[str, str]] = {}


def _flatten(tree: dict[str, Any], prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    for key, value in tree.items():
        full_key = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(cast(dict[str, Any], value), f"{full_key}."))
        elif isinstance(value, str):
            flat[full_key] = value
        else:
            raise ValueError(f"Valeur de traduction invalide pour {full_key!r}")
    return flat


def catalog(language: str = DEFAULT_LANGUAGE) -> dict[str, str]:
    """Catalogue aplati d'une langue (aussi transmis tel quel au frontend)."""
    with _lock:
        if language not in _catalogs:
            _catalogs[language] = _flatten(resources.load_json("i18n", f"{language}.json"))
        return _catalogs[language]


class _SafeParams(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def t(key: str, /, language: str = DEFAULT_LANGUAGE, **params: Any) -> str:
    """Texte traduit. Une clé absente renvoie la clé elle-même et est journalisée."""
    template = catalog(language).get(key)
    if template is None:
        _log.warning("Clé de traduction absente : %s", key)
        return key
    if not params:
        return template
    return template.format_map(_SafeParams(params))


def format_number(value: int | float) -> str:
    """Nombre au format français (espace fine insécable comme séparateur de milliers)."""
    if isinstance(value, int):
        return f"{value:,}".replace(",", "\u202f")
    return f"{value:,.2f}".replace(",", "\u202f").replace(".", ",")
