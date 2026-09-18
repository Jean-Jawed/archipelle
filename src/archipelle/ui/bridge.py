"""Passeur d'événements : file du worker → fenêtre web (CDC §7bis, §11)."""

from __future__ import annotations

import json
import logging
import queue
import threading
from typing import Any, Protocol

from archipelle.core.events import Event

_log = logging.getLogger(__name__)
_POLL_S = 0.1


class Window(Protocol):
    def evaluate_js(self, script: str) -> Any: ...


def event_json(event: Event) -> str:
    return json.dumps(
        {
            "run_id": event.run_id,
            "conversation_id": event.conversation_id,
            "type": event.type.value,
            "payload": event.payload,
            "timestamp": event.timestamp,
        },
        ensure_ascii=False,
    )


class EventBridge:
    """Vide la file dans la fenêtre, un événement à la fois.

    Le worker n'appelle jamais la fenêtre directement : il dépose dans la file, ce qui le
    laisse indépendant de l'interface et évite de bloquer le tour si le rendu est lent.
    """

    def __init__(self, events: queue.Queue[Event]) -> None:
        self.events = events
        self.window: Window | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def publish(self, event: Event) -> None:
        self.events.put(event)

    def start(self, window: Window) -> None:
        self.window = window
        self._thread = threading.Thread(target=self._pump, name="ui-bridge", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _pump(self) -> None:
        while not self._stop.is_set():
            try:
                event = self.events.get(timeout=_POLL_S)
            except queue.Empty:
                continue
            self.deliver(event)

    def deliver(self, event: Event) -> None:
        if self.window is None:
            return
        try:
            self.window.evaluate_js(f"window.archipelleEvent({event_json(event)})")
        except Exception:  # la fenêtre peut disparaître pendant la fermeture
            _log.debug("Événement non délivré", exc_info=True)
