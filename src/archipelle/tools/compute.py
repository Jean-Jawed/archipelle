"""Outil ``compute`` : calculs exacts en décimal (CDC §4).

Garantit l'exactitude arithmétique seulement. Les unités doivent être identiques après
normalisation (sauf pour ``compte``) ; toute incohérence est refusée plutôt que calculée.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Context, Decimal, DivisionByZero, InvalidOperation, localcontext
from typing import Any

from archipelle.core.i18n import t
from archipelle.core.textsearch import normalize
from archipelle.tools.context import ToolContext, ToolInputError, ToolOutcome
from archipelle.tools.units import normalize_unit

OPERATIONS = (
    "somme",
    "moyenne",
    "compte",
    "min",
    "max",
    "différence",
    "pourcentage",
    "ratio",
)
_TWO_VALUES = {"différence", "pourcentage", "ratio"}
_NUMBER = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
_DISPLAY_QUANTUM = Decimal("0.01")  # 🔧 2 décimales
_PRECISION = Context(prec=34)


@dataclass(frozen=True)
class _Value:
    number: Decimal | None
    raw: str
    unit: str  # unité telle que fournie
    canonical: str  # unité normalisée
    source: str


def _canonical_operation(operation: str) -> str:
    wanted = normalize(operation).text.strip()
    for name in OPERATIONS:
        if normalize(name).text == wanted:
            return name
    raise ToolInputError("tools.compute.unknown_operation", operation=operation)


def _parse_number(value: Any, index: int) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ToolInputError("tools.compute.bad_value", index=index, value=repr(value))
    text = str(value).strip() if isinstance(value, int | float | str) else ""
    if not _NUMBER.match(text):
        raise ToolInputError("tools.compute.bad_value", index=index, value=repr(value))
    return Decimal(text)


def _format_decimal(value: Decimal) -> str:
    """Affichage français, arrondi à 2 décimales : 12 345,68."""
    rounded = value.quantize(_DISPLAY_QUANTUM, rounding=ROUND_HALF_UP)
    sign = "-" if rounded < 0 else ""
    integer, _, fraction = f"{abs(rounded):f}".partition(".")
    groups: list[str] = []
    while len(integer) > 3:
        groups.insert(0, integer[-3:])
        integer = integer[:-3]
    groups.insert(0, integer)
    return f"{sign}{'\u202f'.join(groups)},{fraction or '00'}"


def _exact(value: Decimal) -> str:
    text = f"{value.normalize():f}"
    return "0" if text in ("-0", "") else text


def compute(ctx: ToolContext, operation: str, values: list[dict[str, Any]]) -> ToolOutcome:
    del ctx
    name = _canonical_operation(operation)
    if not values:
        raise ToolInputError("tools.compute.no_values")
    if name in _TWO_VALUES and len(values) != 2:
        raise ToolInputError("tools.compute.two_values", operation=name, count=len(values))

    parsed: list[_Value] = []
    for index, item in enumerate(values, start=1):
        unit = str(item.get("unit") or "").strip()
        source = str(item.get("source") or "").strip()
        raw = item.get("value")
        number = None if name == "compte" else _parse_number(raw, index)
        parsed.append(_Value(number, str(raw), unit, normalize_unit(unit), source))

    if name == "compte":
        result: Decimal = Decimal(len(parsed))
        unit_label = ""
    else:
        units = {v.canonical for v in parsed}
        if len(units) > 1:
            listed = ", ".join(sorted(f"« {u or t('tools.compute.no_unit')} »" for u in units))
            raise ToolInputError("tools.compute.unit_mismatch", units=listed)
        unit_label = parsed[0].canonical
        numbers = [v.number for v in parsed if v.number is not None]
        result = _calculate(name, numbers)
        if name == "pourcentage":
            unit_label = "%"
        elif name == "ratio":
            unit_label = ""

    lines = [
        t(
            "tools.compute.result",
            operation=name,
            value=_format_decimal(result),
            unit=f" {unit_label}" if unit_label else "",
            exact=_exact(result),
        ),
        t("tools.compute.detail"),
    ]
    for index, value in enumerate(parsed, start=1):
        shown = value.raw if value.number is None else _exact(value.number)
        lines.append(
            t(
                "tools.compute.detail_line",
                index=index,
                value=shown,
                unit=f" {value.unit}" if value.unit else "",
                source=value.source or t("tools.compute.no_source"),
            )
        )
    lines.append(t("tools.compute.disclaimer"))
    return ToolOutcome(ok=True, content="\n".join(lines))


def _calculate(name: str, numbers: list[Decimal]) -> Decimal:
    with localcontext(_PRECISION) as context:
        context.traps[DivisionByZero] = True
        context.traps[InvalidOperation] = True
        try:
            if name == "somme":
                return sum(numbers, Decimal(0))
            if name == "moyenne":
                return sum(numbers, Decimal(0)) / Decimal(len(numbers))
            if name == "min":
                return min(numbers)
            if name == "max":
                return max(numbers)
            first, second = numbers
            if name == "différence":
                return first - second
            if second == 0:
                raise ToolInputError("tools.compute.division_by_zero")
            if name == "ratio":
                return first / second
            return first / second * Decimal(100)
        except (DivisionByZero, InvalidOperation) as exc:
            raise ToolInputError("tools.compute.division_by_zero") from exc
