"""Description neutre d'un outil, partagée par la couche outils et les fournisseurs.

Les fournisseurs traduisent ``ToolSpec`` dans le format de leur API sans connaître les
outils eux-mêmes (docs/ARCHITECTURE.md §2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # schéma JSON d'un objet
