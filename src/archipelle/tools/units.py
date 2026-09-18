"""Normalisation des unités pour ``compute`` (CDC §4), d'après ``resources/units.json``."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache

from archipelle.core import resources
from archipelle.core.textsearch import normalize


def _norm(text: str) -> str:
    return normalize(text).text.strip()


@dataclass(frozen=True)
class UnitTable:
    variants: dict[str, str]  # variante normalisée → unité canonique
    qualifiers: list[tuple[str, str]]  # (variante normalisée, qualificatif), plus longues d'abord


@cache
def _table() -> UnitTable:
    data = resources.load_json("units.json")
    variants: dict[str, str] = {}
    for canonical, names in data["units"].items():
        variants[_norm(canonical)] = canonical
        for name in names:
            variants[_norm(name)] = canonical
    qualifiers: list[tuple[str, str]] = []
    for canonical, names in data["qualifiers"].items():
        for name in [canonical, *names]:
            qualifiers.append((_norm(name), canonical))
    qualifiers.sort(key=lambda item: len(item[0]), reverse=True)
    return UnitTable(variants, qualifiers)


def normalize_unit(unit: str | None) -> str:
    """Unité canonique : « € HT » → « EUR HT », « euros » → « EUR », « » → « »."""
    if unit is None:
        return ""
    text = f" {_norm(unit)} "
    table = _table()
    found: list[str] = []
    for variant, canonical in table.qualifiers:
        marker = f" {variant} "
        if marker in text:
            text = text.replace(marker, " ")
            if canonical not in found:
                found.append(canonical)
    base = " ".join(text.split())
    canonical_base = table.variants.get(base, base)
    return " ".join([canonical_base, *sorted(found)]).strip()
