"""Pool de sous-processus d'extraction, interruptible (CDC §4bis, §7bis).

Annuler un futur pebble termine le processus qui l'exécute ; le pool le remplace.
"""

from __future__ import annotations

import concurrent.futures
import logging
import multiprocessing
from collections.abc import Callable, Iterable, Iterator
from typing import Any, cast

import pebble

from archipelle.core.outcome import Cancelled
from archipelle.core.system import cpu_workers
from archipelle.extraction import tasks
from archipelle.extraction.types import ErrorCode, ExtractionError
from archipelle.tools.context import Stop

_log = logging.getLogger(__name__)
_POLL_S = 0.05


class ExtractionDeadline(Exception):
    """Échéance atteinte avant la fin d'une tâche (la tâche a été tuée)."""


def unwrap[T](future: pebble.ProcessFuture[T]) -> T:
    """Résultat d'une tâche terminée, avec les erreurs normalisées."""
    try:
        return future.result()
    except ExtractionError:
        raise
    except pebble.ProcessExpired as exc:
        raise ExtractionError(ErrorCode.CRASHED, str(exc)) from exc
    except concurrent.futures.TimeoutError as exc:
        raise ExtractionError(ErrorCode.READ_ERROR, "délai dépassé") from exc
    except concurrent.futures.CancelledError as exc:
        raise ExtractionDeadline() from exc
    except Exception as exc:  # erreur de sérialisation ou inattendue
        _log.exception("Tâche d'extraction en échec")
        raise ExtractionError(ErrorCode.CRASHED, type(exc).__name__) from exc


class ExtractionPool:
    def __init__(self, max_workers: int | None = None) -> None:
        self.max_workers = max_workers or cpu_workers()
        self._pool = pebble.ProcessPool(
            max_workers=self.max_workers,
            initializer=tasks.worker_initializer,
            context=cast(Any, multiprocessing.get_context("spawn")),
        )

    def submit[T](self, function: Callable[..., T], *args: Any) -> pebble.ProcessFuture[T]:
        return self._pool.submit(function, None, *args)

    def completed[T](
        self, futures: Iterable[pebble.ProcessFuture[T]], stop: Stop
    ) -> Iterator[pebble.ProcessFuture[T]]:
        """Renvoie les tâches au fur et à mesure qu'elles se terminent.

        Si ``stop`` se déclenche, toutes les tâches restantes sont annulées (processus
        tués) et l'itération s'arrête : l'appelant consulte alors ``stop.reason()``.
        """
        pending = set(futures)
        try:
            while pending:
                if stop.reason() is not None:
                    return
                done, still_running = concurrent.futures.wait(
                    pending,
                    timeout=min(_POLL_S, max(stop.remaining(), 0.001)),
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                pending = cast(set[pebble.ProcessFuture[T]], still_running)
                yield from cast(set[pebble.ProcessFuture[T]], done)
        finally:
            for future in pending:
                future.cancel()

    def run[T](self, function: Callable[..., T], *args: Any, stop: Stop) -> T:
        """Exécute une tâche et attend son résultat.

        Lève ``Cancelled`` (bouton stop), ``ExtractionDeadline`` (échéance) ou
        ``ExtractionError``.
        """
        future = self.submit(function, *args)
        for done in self.completed([future], stop):
            return unwrap(done)
        if stop.reason() == "cancel":
            raise Cancelled()
        raise ExtractionDeadline()

    def shutdown(self) -> None:
        self._pool.stop()
        self._pool.join(timeout=5)
