"""Résultats et erreurs communs à toutes les couches.

Une erreur destinée à l'utilisateur porte une clé de traduction et ses paramètres,
jamais un texte en dur ni une trace technique (CDC §14).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from archipelle.core.i18n import t


class AppError(Exception):
    """Erreur applicative dont le message est traduisible."""

    def __init__(self, key: str, /, **params: Any) -> None:
        super().__init__(key)
        self.key = key
        self.params = params

    def message(self) -> str:
        return t(self.key, **self.params)

    def __str__(self) -> str:
        return f"{self.key} {self.params}" if self.params else self.key


class Cancelled(AppError):
    """Traitement arrêté par l'utilisateur ou par l'arrêt de l'application."""

    def __init__(self) -> None:
        super().__init__("errors.cancelled")


@dataclass(frozen=True)
class Ok[T]:
    value: T

    @property
    def ok(self) -> bool:
        return True


@dataclass(frozen=True)
class Err:
    key: str
    params: dict[str, Any] = field(default_factory=dict[str, Any])

    @property
    def ok(self) -> bool:
        return False

    def message(self) -> str:
        return t(self.key, **self.params)

    @classmethod
    def from_error(cls, error: AppError) -> Err:
        return cls(error.key, dict(error.params))


type Outcome[T] = Ok[T] | Err
