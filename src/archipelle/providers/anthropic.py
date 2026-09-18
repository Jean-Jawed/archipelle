"""Anthropic via le SDK ``anthropic`` (CDC §13).

Particularités :

- tous les résultats d'outils d'une étape dans un seul message utilisateur, suivis le
  cas échéant du texte utilisateur ; les messages de même rôle sont fusionnés ;
- quand une réponse contient des blocs de raisonnement (``thinking``,
  ``redacted_thinking``), son contenu complet est conservé tel quel, signatures
  comprises, et renvoyé à l'identique au même modèle : l'API l'exige pendant une boucle
  d'outils. Pour un autre modèle, la réponse est reconstruite sans ces blocs ;
- le prompt système et les définitions d'outils sont marqués pour la mise en cache.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any, cast

import anthropic
import httpx2

from archipelle.core.cancel import CancelToken
from archipelle.core.i18n import t
from archipelle.core.logging_setup import log_content, loggable_payload
from archipelle.providers.base import (
    ChatRequest,
    MissingKey,
    NetworkError,
    ProviderError,
    classify_status,
    classify_stream_error,
    error_message_of,
    error_type_of,
)
from archipelle.providers.catalog import ProviderProfile
from archipelle.providers.common import (
    AssistantSegment,
    ToolSegment,
    UserSegment,
    normalize_stop_reason,
    segments,
)
from archipelle.providers.pivot import (
    AssistantMessage,
    ReasoningBlock,
    ToolCall,
    Usage,
    reasoning_for,
)
from archipelle.providers.streaming import StreamGuard, run_streaming

_log = logging.getLogger(__name__)
_CACHE = {"type": "ephemeral"}
_REASONING_TYPES = ("thinking", "redacted_thinking")


class AnthropicProvider:
    def __init__(
        self,
        profile: ProviderProfile,
        api_key: str | None,
        *,
        base_url: str | None = None,
        http_client: httpx2.Client | None = None,
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

        def push(role: str, blocks: list[dict[str, Any]]) -> None:
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"].extend(blocks)
            else:
                messages.append({"role": role, "content": blocks})

        for segment in segments(request.items):
            if isinstance(segment, UserSegment):
                push("user", [{"type": "text", "text": segment.text or " "}])
            elif isinstance(segment, AssistantSegment):
                push("assistant", self._assistant_blocks(segment.message, request.model))
            else:
                push("user", self._tool_blocks(segment))

        payload: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_output_tokens,
            "messages": messages,
            "stream": True,
        }
        if request.system:
            payload["system"] = [
                {"type": "text", "text": request.system, "cache_control": dict(_CACHE)}
            ]
        if request.tools:
            tools: list[dict[str, Any]] = [
                {
                    "name": spec.name,
                    "description": spec.description,
                    "input_schema": spec.parameters,
                }
                for spec in request.tools
            ]
            tools[-1]["cache_control"] = dict(_CACHE)
            payload["tools"] = tools
            payload["tool_choice"] = {"type": request.tool_choice}
        return payload

    def _assistant_blocks(self, message: AssistantMessage, model: str) -> list[dict[str, Any]]:
        for block in reasoning_for(message, self.profile.id, model):
            content = block.payload.get("content")
            if isinstance(content, list) and content:
                return [dict(item) for item in cast(list[dict[str, Any]], content)]
        blocks: list[dict[str, Any]] = []
        if message.text:
            blocks.append({"type": "text", "text": message.text})
        for call in message.tool_calls:
            blocks.append(
                {"type": "tool_use", "id": call.call_id, "name": call.name, "input": call.arguments}
            )
        if not blocks:
            blocks.append({"type": "text", "text": t("providers.empty_answer")})
        return blocks

    @staticmethod
    def _tool_blocks(segment: ToolSegment) -> list[dict[str, Any]]:
        return [
            {
                "type": "tool_result",
                "tool_use_id": result.call_id,
                "content": result.content or t("providers.empty_result"),
                "is_error": result.is_error,
            }
            for result in segment.results
        ]

    # --- appel --------------------------------------------------------------------

    def complete(self, request: ChatRequest, cancel: CancelToken) -> AssistantMessage:
        if not self.api_key:
            raise MissingKey(self.profile.label)
        client = anthropic.Anthropic(
            api_key=self.api_key,
            base_url=self.base_url,
            max_retries=0,
            timeout=httpx2.Timeout(request.idle_timeout_s + 10),
            http_client=self._http_client,
        )
        payload = self.build_payload(request)
        log_content(_log, "requête anthropic", loggable_payload(payload))

        def work(guard: StreamGuard) -> AssistantMessage:
            return self._stream(client, payload, request.model, guard)

        return run_streaming(
            work, cancel=cancel, idle_timeout_s=request.idle_timeout_s, provider=self.profile.label
        )

    def _stream(
        self,
        client: anthropic.Anthropic,
        payload: dict[str, Any],
        model: str,
        guard: StreamGuard,
    ) -> AssistantMessage:
        label = self.profile.label
        create: Any = client.messages.create
        try:
            events = cast(Iterable[Any], guard.attach(create(**payload)))
            guard.touch()
            assembler = _Assembler()
            for event in events:
                guard.touch()
                assembler.add(event)
        except anthropic.APIStatusError as exc:
            message = error_message_of(exc.body, exc.message)
            if exc.status_code < 400:  # erreur envoyée au milieu du flux
                raise classify_stream_error(label, message, error_type_of(exc.body)) from exc
            raise classify_status(label, exc.status_code, message, exc.response.headers) from exc
        except (anthropic.APIConnectionError, httpx2.TransportError) as exc:
            raise NetworkError(label, f"{type(exc).__name__}: {exc}") from exc
        except anthropic.APIError as exc:
            message = error_message_of(exc.body, exc.message)
            raise classify_stream_error(label, message, error_type_of(exc.body)) from exc
        except ProviderError:
            raise
        except Exception as exc:
            if guard.closed:
                raise NetworkError(label, "flux interrompu") from exc
            raise
        return assembler.result(self.profile.id, model)


def _dump(value: Any) -> dict[str, Any]:
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return cast(dict[str, Any], dump(mode="json", exclude_none=True))
    return dict(cast(dict[str, Any], value))


class _Assembler:
    def __init__(self) -> None:
        self.blocks: dict[int, dict[str, Any]] = {}
        self.json_parts: dict[int, list[str]] = {}
        self.stop_reason: str | None = None
        self.input_tokens = 0
        self.output_tokens = 0

    def add(self, event: Any) -> None:
        kind = getattr(event, "type", None)
        if kind == "message_start":
            usage = getattr(getattr(event, "message", None), "usage", None)
            if usage is not None:
                self.input_tokens = sum(
                    int(getattr(usage, name, 0) or 0)
                    for name in (
                        "input_tokens",
                        "cache_read_input_tokens",
                        "cache_creation_input_tokens",
                    )
                )
                self.output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        elif kind == "content_block_start":
            block = _dump(event.content_block)
            if block.get("type") == "tool_use":
                self.json_parts[event.index] = []
            self.blocks[event.index] = block
        elif kind == "content_block_delta":
            self._delta(int(event.index), event.delta)
        elif kind == "message_delta":
            reason = getattr(getattr(event, "delta", None), "stop_reason", None)
            if reason:
                self.stop_reason = str(reason)
            usage = getattr(event, "usage", None)
            if usage is not None and getattr(usage, "output_tokens", None) is not None:
                self.output_tokens = int(usage.output_tokens)

    def _delta(self, index: int, delta: Any) -> None:
        block = self.blocks.setdefault(index, {"type": "text", "text": ""})
        kind = getattr(delta, "type", None)
        if kind == "text_delta":
            block["text"] = str(block.get("text", "")) + str(delta.text)
        elif kind == "input_json_delta":
            self.json_parts.setdefault(index, []).append(str(delta.partial_json))
        elif kind == "thinking_delta":
            block["thinking"] = str(block.get("thinking", "")) + str(delta.thinking)
        elif kind == "signature_delta":
            block["signature"] = str(delta.signature)

    def result(self, provider: str, model: str) -> AssistantMessage:
        content: list[dict[str, Any]] = []
        texts: list[str] = []
        calls: list[ToolCall] = []
        for index in sorted(self.blocks):
            block = self.blocks[index]
            kind = block.get("type")
            if kind == "tool_use":
                raw = "".join(self.json_parts.get(index, []))
                if raw:
                    call = ToolCall.from_raw(str(block["id"]), str(block["name"]), raw)
                else:
                    arguments = cast(dict[str, Any], block.get("input") or {})
                    call = ToolCall.from_arguments(str(block["id"]), str(block["name"]), arguments)
                calls.append(call)
                content.append(
                    {"type": "tool_use", "id": call.call_id, "name": call.name,
                     "input": call.arguments}
                )  # fmt: skip
            elif kind == "text":
                text = str(block.get("text", ""))
                texts.append(text)
                if text:
                    content.append({"type": "text", "text": text})
            elif kind in _REASONING_TYPES:
                content.append(block)
            else:
                _log.debug("Bloc de réponse ignoré : %s", kind)
        reasoning: list[ReasoningBlock] = []
        if any(block.get("type") in _REASONING_TYPES for block in content):
            reasoning.append(ReasoningBlock(provider, model, {"content": content}))
        return AssistantMessage(
            text="".join(texts),
            provider=provider,
            model=model,
            tool_calls=calls,
            reasoning=reasoning,
            usage=Usage(self.input_tokens, self.output_tokens),
            stop_reason=normalize_stop_reason(self.stop_reason),
        )
