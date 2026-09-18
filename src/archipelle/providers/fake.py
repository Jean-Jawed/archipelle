"""Faux fournisseur : rejoue un script JSON (tests, mode démo ``ARCHIPELLE_FAKE_SCRIPT``).

Format du script ::

    {
      "steps": [
        {"text": "Je cherche…", "reasoning": "…",
         "tool_calls": [{"id": "c1", "name": "search_fulltext",
                         "arguments": {"keywords": ["loyer"]}}],
         "delay_s": 0.2},
        {"error": "rate_limited", "retry_after": 1},
        {"stall_s": 999},
        {"text": "Réponse finale [[bail.pdf]]", "repeat": 2}
      ],
      "when_exhausted": "Fin du scénario de démonstration."
    }

Chaque appel à ``complete`` consomme une étape (``repeat`` la rejoue plusieurs fois).
``arguments`` peut aussi être une chaîne JSON, éventuellement invalide, pour simuler un
modèle qui produit des paramètres illisibles. Erreurs simulées : ``auth``, ``quota``,
``rate_limited``, ``network``, ``server``, ``timeout``, ``model_not_found``,
``tools_unsupported``, ``context_too_long``, ``bad_request``. ``stall_s`` simule un flux
sans données : au-delà du délai d'inactivité de la requête, ``IdleTimeout`` est levé.
Le script ne tient pas compte de ``tool_choice`` : un script peut donc simuler un modèle
qui l'ignore, ou qui demande trop d'outils. Toutes les requêtes reçues sont conservées
dans ``requests`` pour les vérifications.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from archipelle.core.cancel import CancelToken
from archipelle.core.outcome import Cancelled
from archipelle.providers.base import (
    AuthError,
    BadRequest,
    ChatRequest,
    ContextTooLong,
    IdleTimeout,
    ModelNotFound,
    NetworkError,
    ProviderError,
    QuotaExceeded,
    RateLimited,
    ServerError,
    ToolsUnsupported,
)
from archipelle.providers.pivot import AssistantMessage, ReasoningBlock, ToolCall, Usage

FAKE_PROVIDER_ID = "fake"

_ERRORS: dict[str, type[ProviderError]] = {
    "auth": AuthError,
    "quota": QuotaExceeded,
    "network": NetworkError,
    "server": ServerError,
    "model_not_found": ModelNotFound,
    "tools_unsupported": ToolsUnsupported,
    "context_too_long": ContextTooLong,
    "bad_request": BadRequest,
}


class ScriptError(ValueError):
    pass


@dataclass(frozen=True)
class FakeStep:
    text: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list[ToolCall])
    delay_s: float = 0.0
    stall_s: float = 0.0
    error: str | None = None
    retry_after: float | None = None
    repeat: int = 1


def _parse_call(raw: dict[str, Any], position: int) -> ToolCall:
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ScriptError(f"appel d'outil sans nom (position {position})")
    call_id = str(raw.get("id") or f"fake_{position}")
    arguments = raw.get("arguments", {})
    if isinstance(arguments, str):
        return ToolCall.from_raw(call_id, name, arguments)
    if not isinstance(arguments, dict):
        raise ScriptError(f"arguments invalides pour {name}")
    return ToolCall.from_arguments(call_id, name, cast(dict[str, Any], arguments))


def parse_script(data: dict[str, Any]) -> tuple[list[FakeStep], str]:
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ScriptError("le script doit contenir une liste « steps » non vide")
    steps: list[FakeStep] = []
    counter = 0
    for index, raw_value in enumerate(cast(list[Any], raw_steps)):
        if not isinstance(raw_value, dict):
            raise ScriptError(f"étape {index} invalide")
        raw = cast(dict[str, Any], raw_value)
        error = raw.get("error")
        if error is not None and error not in _ERRORS and error not in ("rate_limited", "timeout"):
            raise ScriptError(f"erreur inconnue à l'étape {index} : {error}")
        calls: list[ToolCall] = []
        for call in cast(list[Any], raw.get("tool_calls") or []):
            counter += 1
            calls.append(_parse_call(cast(dict[str, Any], call), counter))
        steps.append(
            FakeStep(
                text=str(raw.get("text") or ""),
                reasoning=str(raw.get("reasoning") or ""),
                tool_calls=calls,
                delay_s=float(raw.get("delay_s") or 0.0),
                stall_s=float(raw.get("stall_s") or 0.0),
                error=cast(str | None, error),
                retry_after=cast(float | None, raw.get("retry_after")),
                repeat=max(1, int(raw.get("repeat") or 1)),
            )
        )
    return steps, str(data.get("when_exhausted") or "")


class FakeProvider:
    def __init__(
        self,
        steps: list[FakeStep],
        *,
        provider_id: str = FAKE_PROVIDER_ID,
        label: str = "Fournisseur de démonstration",
        when_exhausted: str = "",
    ) -> None:
        self._steps = [step for step in steps for _ in range(step.repeat)]
        self._provider_id = provider_id
        self.label = label
        self.when_exhausted = when_exhausted
        self.requests: list[ChatRequest] = []
        self._lock = threading.Lock()

    @classmethod
    def from_script(cls, data: dict[str, Any], **kwargs: Any) -> FakeProvider:
        steps, exhausted = parse_script(data)
        return cls(steps, when_exhausted=exhausted, **kwargs)

    @classmethod
    def from_file(cls, path: Path, **kwargs: Any) -> FakeProvider:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ScriptError(f"script illisible : {exc}") from exc
        if not isinstance(data, dict):
            raise ScriptError("le script doit être un objet JSON")
        return cls.from_script(cast(dict[str, Any], data), **kwargs)

    @property
    def id(self) -> str:
        return self._provider_id

    @property
    def remaining(self) -> int:
        return len(self._steps)

    def complete(self, request: ChatRequest, cancel: CancelToken) -> AssistantMessage:
        with self._lock:
            self.requests.append(request)
            step = self._steps.pop(0) if self._steps else None
        cancel.raise_if_cancelled()
        if step is None:
            return AssistantMessage(
                self.when_exhausted or "…", self._provider_id, request.model, stop_reason="end"
            )
        if step.stall_s:
            waited = min(step.stall_s, request.idle_timeout_s)
            _wait(cancel, waited)
            if step.stall_s >= request.idle_timeout_s:
                raise IdleTimeout(self.label, request.idle_timeout_s)
        if step.delay_s:
            _wait(cancel, step.delay_s)
        if step.error == "rate_limited":
            raise RateLimited(self.label, "simulé", step.retry_after)
        if step.error == "timeout":
            raise IdleTimeout(self.label, request.idle_timeout_s)
        if step.error is not None:
            raise _ERRORS[step.error](self.label, "simulé")
        reasoning = (
            [ReasoningBlock(self._provider_id, request.model, {"text": step.reasoning})]
            if step.reasoning
            else []
        )
        return AssistantMessage(
            text=step.text,
            provider=self._provider_id,
            model=request.model,
            tool_calls=list(step.tool_calls),
            reasoning=reasoning,
            usage=Usage(sum(len(str(i)) for i in request.items) // 4, len(step.text) // 4),
            stop_reason="tool_calls" if step.tool_calls else "end",
        )


def _wait(cancel: CancelToken, seconds: float) -> None:
    """Attente découpée, comme un flux qui arrive par morceaux, interrompue par l'arrêt."""
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        if cancel.wait(min(remaining, 0.05)):
            raise Cancelled()
