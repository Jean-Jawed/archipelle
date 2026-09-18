"""Vérifie que chaque clé de traduction littérale du code existe dans fr.json.

Usage : uv run python scripts/check_i18n.py
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "src" / "archipelle"
PREFIXES = (
    "errors.",
    "tools.",
    "tree.",
    "units.",
    "extraction.",
    "progress.",
    "tool_specs.",
    "app.",
)
KEY = re.compile(r"^[a-z_]+(\.[a-z0-9_]+)+$")


def literal_keys() -> set[str]:
    keys: set[str] = set()
    for path in ROOT.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value.startswith(PREFIXES)
                and KEY.match(node.value)
                and not node.value.endswith(".json")
            ):
                keys.add(node.value)
    return keys


def main() -> int:
    sys.path.insert(0, str(ROOT.parent))
    from archipelle.core.i18n import catalog

    known = catalog()
    from archipelle.extraction.types import ErrorCode
    from archipelle.tools.context import IgnoredReason
    from archipelle.tools.registry import registered_tools

    expected = literal_keys()
    expected |= {f"tools.errors.extraction.{code.value}" for code in ErrorCode}
    expected |= {
        f"tools.count.{kind}_{variant}"
        for kind in ("entries", "paths")
        for variant in ("all", "partial")
    }
    expected |= {f"journal.reasons.{reason.value}" for reason in IgnoredReason}
    from archipelle.providers import base as provider_base

    for error_class in vars(provider_base).values():
        if isinstance(error_class, type) and issubclass(error_class, provider_base.ProviderError):
            expected.add(f"errors.provider.{error_class.key_name}")
    missing_dynamic: list[str] = []
    for tool in registered_tools().values():
        texts = [tool.spec.description]
        stack = [tool.spec.parameters]
        while stack:
            schema = stack.pop()
            if "description" in schema:
                texts.append(schema["description"])
            stack.extend(schema.get("properties", {}).values())
            if isinstance(schema.get("items"), dict):
                stack.append(schema["items"])
        missing_dynamic.extend(text for text in texts if text.startswith("tool_specs."))
    missing = sorted(k for k in expected if k not in known) + missing_dynamic
    for key in missing:
        print(key)
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
