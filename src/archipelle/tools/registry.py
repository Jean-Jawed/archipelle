"""Déclaration des outils au modèle et exécution d'un appel (CDC §4).

Les noms déclarés sont exactement les noms des fonctions Python (CDC §14). Aucun outil
d'écriture ou de suppression n'existe. Un appel invalide ne lève jamais d'exception :
il produit un résultat d'erreur lisible, que le modèle peut corriger. Seul l'arrêt
demandé par l'utilisateur (``Cancelled``) est propagé.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from typing import Any, cast

from archipelle.core.i18n import t
from archipelle.core.logging_setup import log_content
from archipelle.core.outcome import AppError, Cancelled
from archipelle.core.toolspec import ToolSpec
from archipelle.extraction.types import ExtractionError
from archipelle.tools.common import extraction_error_text
from archipelle.tools.compute import OPERATIONS, compute
from archipelle.tools.context import ToolContext, ToolOutcome
from archipelle.tools.list_files import MAX_DEPTH, list_files
from archipelle.tools.read_file import read_file
from archipelle.tools.search_files import search_files
from archipelle.tools.search_fulltext import search_fulltext

_log = logging.getLogger(__name__)

type ToolFunction = Callable[..., ToolOutcome]


def _string(name: str, tool: str) -> dict[str, Any]:
    return {"type": "string", "description": t(f"tool_specs.{tool}.params.{name}")}


def _integer(name: str, tool: str, minimum: int, maximum: int | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "integer",
        "minimum": minimum,
        "description": t(f"tool_specs.{tool}.params.{name}"),
    }
    if maximum is not None:
        schema["maximum"] = maximum
    return schema


def _string_list(name: str, tool: str) -> dict[str, Any]:
    return {
        "type": "array",
        "items": {"type": "string"},
        "description": t(f"tool_specs.{tool}.params.{name}"),
    }


def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _build_specs() -> list[ToolSpec]:
    specs: list[ToolSpec] = []

    tool = "list_files"
    specs.append(
        ToolSpec(
            tool,
            t(f"tool_specs.{tool}.description"),
            _object(
                {
                    "path": _string("path", tool),
                    "depth": _integer("depth", tool, 1, MAX_DEPTH),
                    "extensions": _string_list("extensions", tool),
                    "modified_after": _string("modified_after", tool),
                    "modified_before": _string("modified_before", tool),
                    "max_results": _integer("max_results", tool, 1, 500),
                },
                [],
            ),
        )
    )

    tool = "search_files"
    keywords = _string_list("keywords", tool)
    keywords["minItems"] = 1
    specs.append(
        ToolSpec(
            tool,
            t(f"tool_specs.{tool}.description"),
            _object(
                {
                    "keywords": keywords,
                    "path": _string("path", tool),
                    "extensions": _string_list("extensions", tool),
                    "max_results": _integer("max_results", tool, 1, 100),
                },
                ["keywords"],
            ),
        )
    )

    tool = "search_fulltext"
    keywords = _string_list("keywords", tool)
    keywords["minItems"] = 1
    specs.append(
        ToolSpec(
            tool,
            t(f"tool_specs.{tool}.description"),
            _object(
                {
                    "keywords": keywords,
                    "path": _string("path", tool),
                    "extensions": _string_list("extensions", tool),
                    "modified_after": _string("modified_after", tool),
                    "modified_before": _string("modified_before", tool),
                    "max_results": _integer("max_results", tool, 1, 30),
                },
                ["keywords"],
            ),
        )
    )

    tool = "read_file"
    specs.append(
        ToolSpec(
            tool,
            t(f"tool_specs.{tool}.description"),
            _object(
                {
                    "path": _string("path", tool),
                    "page": _integer("page", tool, 1),
                    "offset": _integer("offset", tool, 0),
                    "max_chars": _integer("max_chars", tool, 1, 20_000),
                },
                ["path"],
            ),
        )
    )

    tool = "compute"
    value_item = _object(
        {
            "value": {
                "type": ["number", "string"],
                "description": t(f"tool_specs.{tool}.params.value"),
            },
            "unit": _string("unit", tool),
            "source": _string("source", tool),
        },
        ["value", "source"],
    )
    specs.append(
        ToolSpec(
            tool,
            t(f"tool_specs.{tool}.description"),
            _object(
                {
                    "operation": {
                        "type": "string",
                        "enum": list(OPERATIONS),
                        "description": t(f"tool_specs.{tool}.params.operation"),
                    },
                    "values": {
                        "type": "array",
                        "items": value_item,
                        "minItems": 1,
                        "description": t(f"tool_specs.{tool}.params.values"),
                    },
                },
                ["operation", "values"],
            ),
        )
    )
    return specs


@dataclass(frozen=True)
class RegisteredTool:
    spec: ToolSpec
    function: ToolFunction


_FUNCTIONS: dict[str, ToolFunction] = {
    "list_files": list_files,
    "search_files": search_files,
    "search_fulltext": search_fulltext,
    "read_file": read_file,
    "compute": compute,
}


def tool_specs() -> list[ToolSpec]:
    return _build_specs()


@cache
def registered_tools() -> dict[str, RegisteredTool]:
    return {spec.name: RegisteredTool(spec, _FUNCTIONS[spec.name]) for spec in _build_specs()}


# --- validation ----------------------------------------------------------------------


def _coerce(value: Any, schema: dict[str, Any], label: str, errors: list[str]) -> Any:
    """Contrôle le type et corrige les écarts sans ambiguïté (« 2 » → 2, « x » → [« x »])."""
    expected = schema.get("type")
    types: list[Any] = cast(list[Any], expected) if isinstance(expected, list) else [expected]
    if value is None:
        errors.append(t("tools.errors.param_null", name=label))
        return None
    if "integer" in types:
        if isinstance(value, bool):
            pass
        elif isinstance(value, int):
            return value
        elif isinstance(value, float) and value.is_integer():
            return int(value)
        elif isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value.strip())
    if "number" in types and isinstance(value, int | float) and not isinstance(value, bool):
        return value
    if "string" in types and isinstance(value, str):
        # Une valeur hors énumération est tolérée (casse, accents) : l'outil tranche.
        return value
    if "array" in types:
        items_schema = cast(dict[str, Any], schema.get("items", {}))
        if isinstance(value, str) and items_schema.get("type") == "string":
            value = [value]
        if isinstance(value, list):
            coerced = [
                _coerce(item, items_schema, f"{label}[{index}]", errors)
                for index, item in enumerate(cast(list[Any], value))
            ]
            if len(coerced) < int(schema.get("minItems", 0)):
                errors.append(t("tools.errors.param_empty", name=label))
            return coerced
    if "object" in types and isinstance(value, dict):
        return _validate_object(cast(dict[str, Any], value), schema, errors, prefix=f"{label}.")
    errors.append(t("tools.errors.param_type", name=label, expected=" | ".join(map(str, types))))
    return value


def _validate_object(
    data: dict[str, Any], schema: dict[str, Any], errors: list[str], prefix: str = ""
) -> dict[str, Any]:
    properties = cast(dict[str, dict[str, Any]], schema.get("properties", {}))
    result: dict[str, Any] = {}
    for key, value in data.items():
        if key not in properties:
            errors.append(t("tools.errors.param_unknown", name=f"{prefix}{key}"))
            continue
        if value is None:  # paramètre facultatif explicitement vide : ignoré
            continue
        result[key] = _coerce(value, properties[key], f"{prefix}{key}", errors)
    for key in cast(list[str], schema.get("required", [])):
        if key not in result:
            errors.append(t("tools.errors.param_missing", name=f"{prefix}{key}"))
    return result


def parse_arguments(raw: dict[str, Any] | str | None) -> dict[str, Any]:
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AppError("tools.errors.bad_json") from exc
    if not isinstance(decoded, dict):
        raise AppError("tools.errors.bad_json")
    return cast(dict[str, Any], decoded)


# --- exécution -----------------------------------------------------------------------


def dispatch(
    ctx: ToolContext,
    name: str,
    arguments: dict[str, Any] | str | None,
    tools: dict[str, RegisteredTool] | None = None,
) -> ToolOutcome:
    available = tools if tools is not None else registered_tools()
    tool = available.get(name)
    if tool is None:
        return ToolOutcome(
            ok=False, content=t("tools.errors.unknown_tool", name=name, tools=", ".join(available))
        )
    try:
        data = parse_arguments(arguments)
    except AppError as error:
        return ToolOutcome(ok=False, content=error.message())
    errors: list[str] = []
    params = _validate_object(data, tool.spec.parameters, errors)
    if errors:
        return ToolOutcome(
            ok=False, content=t("tools.errors.bad_params", name=name, errors="\n".join(errors))
        )
    try:
        outcome = tool.function(ctx, **params)
    except Cancelled:
        raise
    except ExtractionError as error:
        outcome = ToolOutcome(
            ok=False, content=extraction_error_text(error, str(params.get("path", "")))
        )
    except AppError as error:
        outcome = ToolOutcome(ok=False, content=error.message())
    except Exception:
        _log.exception("Erreur inattendue dans l'outil %s", name)
        outcome = ToolOutcome(ok=False, content=t("tools.errors.unexpected", name=name))
    log_content(_log, f"résultat de {name}", outcome.content)
    return outcome
