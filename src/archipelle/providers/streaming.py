"""Exécution interruptible d'un appel en streaming (CDC §7bis).

L'appel s'exécute dans un thread auxiliaire. Le thread appelant (le worker) attend soit
la fin de l'appel, soit le bouton stop, soit l'inactivité du flux :

- bouton stop : le flux est fermé et ``Cancelled`` est levé immédiatement, sans attendre
  le thread auxiliaire ; un résultat qui arriverait ensuite est ignoré ;
- inactivité : aucune donnée reçue pendant ``idle_timeout_s`` (y compris pendant
  l'établissement de la connexion) ; le flux est fermé et ``IdleTimeout`` est levé.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Protocol

from archipelle.core.cancel import CancelToken
from archipelle.core.outcome import Cancelled
from archipelle.providers.base import IdleTimeout

_log = logging.getLogger(__name__)
_POLL_S = 0.05


class Closable(Protocol):
    def close(self) -> None: ...


class StreamGuard:
    """Lien entre le thread de lecture et le thread qui surveille."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._resources: list[Closable] = []
        self._closed = False
        self.last_activity = time.monotonic()

    def attach[C: Closable](self, resource: C) -> C:
        """Enregistre une ressource à fermer en cas d'arrêt (flux, réponse HTTP)."""
        with self._lock:
            closed = self._closed
            if not closed:
                self._resources.append(resource)
        if closed:
            _close_quietly(resource)
        return resource

    def touch(self) -> None:
        """Signale la réception de données."""
        self.last_activity = time.monotonic()

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        with self._lock:
            self._closed = True
            resources, self._resources = self._resources, []
        for resource in resources:
            _close_quietly(resource)


def _close_quietly(resource: Closable) -> None:
    try:
        resource.close()
    except Exception:
        _log.debug("Fermeture d'un flux en échec", exc_info=True)


def run_streaming[T](
    work: Callable[[StreamGuard], T],
    *,
    cancel: CancelToken,
    idle_timeout_s: float,
    provider: str,
) -> T:
    """Exécute ``work(guard)`` en surveillant l'arrêt et l'inactivité."""
    cancel.raise_if_cancelled()
    guard = StreamGuard()
    finished = threading.Event()
    outcome: dict[str, object] = {}

    def target() -> None:
        try:
            outcome["value"] = work(guard)
        except BaseException as exc:  # transmis au thread appelant
            outcome["error"] = exc
        finally:
            finished.set()

    thread = threading.Thread(target=target, name=f"provider-{provider}", daemon=True)
    handle = cancel.add_callback(guard.close)
    thread.start()
    try:
        while not finished.wait(_POLL_S):
            if cancel.cancelled:
                raise Cancelled()
            if time.monotonic() - guard.last_activity > idle_timeout_s:
                guard.close()
                raise IdleTimeout(provider, idle_timeout_s)
    finally:
        cancel.remove_callback(handle)
    if cancel.cancelled:
        raise Cancelled()
    if "error" in outcome:
        error = outcome["error"]
        assert isinstance(error, BaseException)
        raise error
    return outcome["value"]  # type: ignore[return-value]
