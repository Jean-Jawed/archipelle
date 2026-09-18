"""Mistral via le SDK ``mistralai`` (CDC §13).

Particularités :

- un message ``tool`` par résultat d'outil, avec le nom de l'outil ;
- l'API refuse un message utilisateur juste après des résultats d'outils : un court
  message assistant de liaison est inséré ;
- les modèles à raisonnement renvoient des morceaux ``thinking`` dans le contenu ; ils
  sont conservés dans le pivot mais jamais renvoyés à l'API.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import Any, cast

import httpx
from mistralai.client import Mistral
from mistralai.client.errors import NoResponseError, ResponseValidationError
from mistralai.client.errors.mistralerror import MistralError

from archipelle.core.cancel import CancelToken
from archipelle.core.logging_setup import log_content, loggable_payload
from archipelle.providers.base import (
    ChatRequest,
    MissingKey,
    NetworkError,
    ProviderError,
    ServerError,
    classify_status,
    error_message_of,
)
from archipelle.providers.catalog import ProviderProfile
from archipelle.providers.common import (
    AssistantSegment,
    ToolCallAccumulator,
    ToolSegment,
    UserSegment,
    as_list,
    normalize_stop_reason,
    segments,
)
from archipelle.providers.pivot import AssistantMessage, ReasoningBlock, Usage
from archipelle.providers.streaming import StreamGuard, run_streaming

_log = logging.getLogger(__name__)


def _decode_body(body: str) -> object:
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        return body


class MistralProvider:
    def __init__(
        self,
        profile: ProviderProfile,
        api_key: str | None,
        *,
        base_url: str | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.profile = profile
        self.api_key = (api_key or "").strip() or None
        self.base_url = (base_url or profile.base_url or "").strip() or None
        self._http_client = http_client

    @property
    def id(self) -> str:
        return self.profile.id

    # --- traduction pivot → API ---------------------------------------------------

    def build_payload(self, request: ChatRequest) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        for segment in segments(request.items, bridge_after_tools=True):
            if isinstance(segment, UserSegment):
                messages.append({"role": "user", "content": segment.text})
            elif isinstance(segment, AssistantSegment):
                messages.append(self._assistant(segment))
            else:
                messages.extend(self._tool_messages(segment))
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "max_tokens": request.max_output_tokens,
        }
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": spec.parameters,
                    },
                }
                for spec in request.tools
            ]
            payload["tool_choice"] = request.tool_choice
        return payload

    @staticmethod
    def _assistant(segment: AssistantSegment) -> dict[str, Any]:
        message = segment.message
        entry: dict[str, Any] = {"role": "assistant"}
        if message.text or not message.tool_calls:
            entry["content"] = message.text
        if message.tool_calls:
            entry["tool_calls"] = [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.raw_arguments or "{}"},
                }
                for call in message.tool_calls
            ]
        return entry

    @staticmethod
    def _tool_messages(segment: ToolSegment) -> list[dict[str, Any]]:
        return [
            {
                "role": "tool",
                "tool_call_id": result.call_id,
                "name": result.name,
                "content": result.content,
            }
            for result in segment.results
        ]

    # --- appel --------------------------------------------------------------------

    def complete(self, request: ChatRequest, cancel: CancelToken) -> AssistantMessage:
        if not self.api_key:
            raise MissingKey(self.profile.label)
        http_client = self._http_client or httpx.Client(timeout=request.idle_timeout_s + 10)
        client = Mistral(
            api_key=self.api_key,
            server_url=self.base_url,
            client=http_client,
            timeout_ms=int((request.idle_timeout_s + 10) * 1000),
        )
        payload = self.build_payload(request)
        log_content(_log, "requête mistral", loggable_payload(payload))

        def work(guard: StreamGuard) -> AssistantMessage:
            try:
                return self._stream(client, payload, request.model, guard)
            finally:
                if self._http_client is None:
                    http_client.close()

        return run_streaming(
            work, cancel=cancel, idle_timeout_s=request.idle_timeout_s, provider=self.profile.label
        )

    def _stream(
        self, client: Mistral, payload: dict[str, Any], model: str, guard: StreamGuard
    ) -> AssistantMessage:
        label = self.profile.label
        stream_fn: Any = client.chat.stream
        try:
            events = cast(Iterable[Any], guard.attach(stream_fn(**payload)))
            guard.touch()
            assembler = _Assembler()
            for event in events:
                guard.touch()
                assembler.add(getattr(event, "data", event))
        except ResponseValidationError as exc:  # sous-classe de MistralError
            raise ServerError(label, str(exc)) from exc
        except MistralError as exc:
            message = error_message_of(_decode_body(exc.body), exc.message)
            raise classify_status(label, exc.status_code, message, exc.headers) from exc
        except (NoResponseError, httpx.TransportError) as exc:
            raise NetworkError(label, f"{type(exc).__name__}: {exc}") from exc
        except ProviderError:
            raise
        except Exception as exc:
            if guard.closed:
                raise NetworkError(label, "flux interrompu") from exc
            raise
        return assembler.result(self.profile.id, model)


class _Assembler:
    def __init__(self) -> None:
        self.text: list[str] = []
        self.thinking: list[str] = []
        self.calls = ToolCallAccumulator()
        self.finish: str | None = None
        self.usage: Usage | None = None

    def add(self, chunk: Any) -> None:
        usage = getattr(chunk, "usage", None)
        if usage is not None:
            self.usage = Usage(
                int(getattr(usage, "prompt_tokens", 0) or 0),
                int(getattr(usage, "completion_tokens", 0) or 0),
            )
        for choice in as_list(getattr(chunk, "choices", None)):
            if getattr(choice, "index", 0) not in (0, None):
                continue
            delta = getattr(choice, "delta", None)
            if delta is not None:
                self._content(getattr(delta, "content", None))
                for call in as_list(getattr(delta, "tool_calls", None)):
                    function = getattr(call, "function", None)
                    arguments = getattr(function, "arguments", None) if function else None
                    if isinstance(arguments, dict):  # le SDK accepte aussi un objet
                        arguments = json.dumps(arguments, ensure_ascii=False)
                    self.calls.add(
                        _int_or_none(getattr(call, "index", None)),
                        getattr(call, "id", None),
                        getattr(function, "name", None) if function else None,
                        cast(str | None, arguments),
                    )
            finish = getattr(choice, "finish_reason", None)
            if finish:
                self.finish = str(getattr(finish, "value", finish))

    def _content(self, content: Any) -> None:
        if isinstance(content, str):
            self.text.append(content)
            return
        for part in as_list(content):
            kind = getattr(part, "type", None)
            if kind == "text":
                self.text.append(str(getattr(part, "text", "") or ""))
            elif kind == "thinking":
                for piece in as_list(getattr(part, "thinking", None)):
                    text = getattr(piece, "text", None)
                    if isinstance(text, str):
                        self.thinking.append(text)

    def result(self, provider: str, model: str) -> AssistantMessage:
        reasoning = (
            [ReasoningBlock(provider, model, {"text": "".join(self.thinking)})]
            if self.thinking
            else []
        )
        return AssistantMessage(
            text="".join(self.text),
            provider=provider,
            model=model,
            tool_calls=self.calls.calls(),
            reasoning=reasoning,
            usage=self.usage,
            stop_reason=normalize_stop_reason(self.finish),
        )


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
