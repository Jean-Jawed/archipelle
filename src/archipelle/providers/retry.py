"""Nouvelles tentatives : le seul endroit qui décide de retenter (CDC §7).

Deux nouvelles tentatives 🔧 après une panne réseau ou une limite de débit ; aucune pour
une clé invalide, un modèle introuvable, des outils non pris en charge ou une requête
refusée. Les tentatives intégrées aux SDK sont désactivées (``max_retries=0``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from archipelle.core.cancel import CancelToken
from archipelle.core.outcome import Cancelled
from archipelle.providers.base import ProviderError, RateLimited

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetryPolicy:
    retries: int = 2  # 🔧
    network_delays_s: tuple[float, ...] = (2.0, 5.0)
    rate_limit_delays_s: tuple[float, ...] = (10.0, 30.0)  # attente progressive
    max_wait_s: float = 60.0

    def delay(self, error: ProviderError, attempt: int) -> float:
        if isinstance(error, RateLimited):
            base = self.rate_limit_delays_s[min(attempt, len(self.rate_limit_delays_s) - 1)]
            if error.retry_after is not None:
                base = max(error.retry_after, 1.0)
            return min(base, self.max_wait_s)
        return self.network_delays_s[min(attempt, len(self.network_delays_s) - 1)]


type RetryListener = Callable[[ProviderError, int, float], None]


def call_with_retry[T](
    call: Callable[[], T],
    *,
    cancel: CancelToken,
    policy: RetryPolicy | None = None,
    on_retry: RetryListener | None = None,
) -> T:
    """Exécute ``call`` ; ``on_retry(erreur, numéro de tentative, attente)`` est appelé
    avant chaque nouvelle tentative (pour l'afficher dans la transparence)."""
    rules = policy or RetryPolicy()
    attempt = 0
    while True:
        cancel.raise_if_cancelled()
        try:
            return call()
        except ProviderError as error:
            if not error.retryable or attempt >= rules.retries:
                raise
            wait = rules.delay(error, attempt)
            attempt += 1
            _log.warning(
                "Fournisseur %s : %s — nouvelle tentative %d dans %.0f s",
                error.provider,
                type(error).__name__,
                attempt,
                wait,
            )
            if on_retry is not None:
                on_retry(error, attempt, wait)
            if cancel.wait(wait):
                raise Cancelled() from error
