"""Événements publiés par le worker et affichés par l'interface (CDC §7bis, §11).

L'interface ignore tout événement dont le ``run_id`` n'est pas celui du tour actif.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class EventType(StrEnum):
    TURN_STARTED = "turn_started"
    ITERATION = "iteration"
    STEP_STARTED = "step_started"  # « étape_démarrée »
    STEP_PROGRESS = "step_progress"
    FILE_CONSULTED = "file_consulted"  # « fichier_lu »
    FILE_IGNORED = "file_ignored"
    ERROR = "error"  # « erreur »
    FINAL_ANSWER = "final_answer"  # « réponse_finale »
    TURN_INTERRUPTED = "turn_interrupted"
    TURN_ENDED = "turn_ended"
    APP_NOTICE = "app_notice"  # message hors tour (Tesseract absent, etc.)


@dataclass(frozen=True)
class Event:
    run_id: str
    conversation_id: str
    type: EventType
    payload: dict[str, Any] = field(default_factory=dict[str, Any])
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["type"] = self.type.value
        return data

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)
