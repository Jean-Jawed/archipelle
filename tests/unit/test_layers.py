"""Vérifie les dépendances autorisées entre couches (docs/ARCHITECTURE.md §2)."""

from __future__ import annotations

import ast
from pathlib import Path

import archipelle

PACKAGE_ROOT = Path(archipelle.__file__).parent

ALLOWED: dict[str, tuple[str, ...]] = {
    "core": ("core",),
    "persistence": ("core", "persistence"),
    "extraction": ("core", "extraction"),
    "cache": ("core", "cache", "extraction.types", "persistence.db", "persistence.files"),
    "tools": (
        "core",
        "cache",
        "tools",
        "extraction.errors",
        "extraction.formats",
        "extraction.tasks",
        "extraction.types",
        "persistence.settings_store",
    ),
    "providers": ("core", "providers"),
    "agent": ("core", "tools", "providers", "persistence", "cache", "agent"),
    # L'interface ne parle qu'à la façade ; elle lit des types (conversations, catalogue,
    # réglages, pivot) mais n'appelle ni les outils, ni les fournisseurs, ni l'extraction.
    "ui": (
        "core",
        "ui",
        "agent.service",
        "agent.history",
        "providers.catalog",
        "providers.pivot",
        "persistence.settings_store",
    ),
}
UNRESTRICTED = {"app", "__main__", "__init__"}


def _imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            package_dir = PACKAGE_ROOT.joinpath(*node.module.split(".")[1:])
            if node.module.startswith("archipelle") and package_dir.is_dir():
                found.extend(f"{node.module}.{alias.name}" for alias in node.names)
            else:
                found.append(node.module)
    return [name for name in found if name.startswith("archipelle.")]


def _allowed(target: str, prefixes: tuple[str, ...]) -> bool:
    relative = target.removeprefix("archipelle.")
    if relative in {"APP_NAME", "__version__"}:
        return True
    return any(relative == prefix or relative.startswith(prefix + ".") for prefix in prefixes)


def test_no_forbidden_cross_layer_imports() -> None:
    violations: list[str] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        relative = path.relative_to(PACKAGE_ROOT)
        layer = relative.parts[0] if len(relative.parts) > 1 else relative.stem
        if layer in UNRESTRICTED:
            continue
        assert layer in ALLOWED, f"couche inconnue : {layer}"
        for target in _imported_modules(path):
            if not _allowed(target, ALLOWED[layer]):
                violations.append(f"{relative} importe {target}")
    assert not violations, "\n".join(violations)


def test_no_relative_imports() -> None:
    offenders = [
        str(path.relative_to(PACKAGE_ROOT))
        for path in PACKAGE_ROOT.rglob("*.py")
        if any(
            isinstance(node, ast.ImportFrom) and node.level > 0
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
    ]
    assert not offenders
