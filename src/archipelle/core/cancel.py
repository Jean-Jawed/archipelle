"""Jeton d'annulation partagé entre la boucle, les outils et les fournisseurs (CDC §7bis).

``cancel()`` lève le drapeau puis exécute les rappels enregistrés (fermeture d'un flux
HTTP, annulation des extractions en cours). Un rappel enregistré après l'annulation est
exécuté immédiatement.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from archipelle.core.outcome import Cancelled

_log = logging.getLogger(__name__)

type Callback = Callable[[], None]


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._callbacks: dict[int, Callback] = {}
        self._next_handle = 0

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        with self._lock:
            if self._event.is_set():
                return
            self._event.set()
            callbacks = list(self._callbacks.values())
            self._callbacks.clear()
        for callback in callbacks:
            _run_safely(callback)

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise Cancelled()

    def wait(self, timeout: float) -> bool:
        """Attend au plus ``timeout`` secondes ; renvoie ``True`` si annulé entre-temps."""
        return self._event.wait(timeout)

    def add_callback(self, callback: Callback) -> int:
        with self._lock:
            if not self._event.is_set():
                handle = self._next_handle
                self._next_handle += 1
                self._callbacks[handle] = callback
                return handle
        _run_safely(callback)
        return -1

    def remove_callback(self, handle: int) -> None:
        with self._lock:
            self._callbacks.pop(handle, None)


def _run_safely(callback: Callback) -> None:
    try:
        callback()
    except Exception:
        _log.exception("Échec d'un rappel d'annulation")
