"""Chat Completions via le SDK ``openai`` : OpenAI, DeepSeek, Kimi et adresse personnalisée.

Particularités (CDC §13) :

- un message ``tool`` par résultat d'outil ;
- le raisonnement arrive dans un champ que le SDK ne définit pas (``reasoning_content``,
  ou ``reasoning`` chez certains serveurs locaux) : il est lu dans les champs
  supplémentaires, y compris en streaming ;
- ``reasoning_echo = "always"`` (DeepSeek, Kimi) : ``reasoning_content`` est renvoyé sur
  chaque message assistant, vide si le modèle actif n'en a pas produit, faute de quoi
  ces API refusent la suite d'une boucle d'outils ;
- le nom du paramètre de taille de réponse dépend du fournisseur (profil du catalogue).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any, cast

import httpx2
import openai

from archipelle.core.cancel import CancelToken
from archipelle.core.logging_setup import log_content, loggable_payload
from archipelle.providers.base import (
    ChatRequest,
    InvalidConfiguration,
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
    ToolCallAccumulator,
    ToolSegment,
    UserSegment,
    as_list,
    normalize_stop_reason,
    segments,
)
from archipelle.providers.pivot import AssistantMessage, ReasoningBlock, Usage, reasoning_for
from archipelle.providers.streaming import StreamGuard, run_streaming

_log = logging.getLogger(__name__)
_REASONING_FIELDS = ("reasoning_content", "reasoning")
NO_KEY_PLACEHOLDER = "sk-no-key"  # serveurs locaux sans authentification


def _extra(model: Any) -> dict[str, Any]:
    extra = getattr(model, "model_extra", None)
    return cast(dict[str, Any], extra) if isinstance(extra, dict) else {}


class OpenAICompatProvider:
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
        if request.system:
            messages.append({"role": "system", "content": request.system})
        for segment in segments(request.items, bridge_after_tools=True):
            if isinstance(segment, UserSegment):
                messages.append({"role": "user", "content": segment.text})
            elif isinstance(segment, AssistantSegment):
                messages.append(self._assistant(segment, request.model))
            else:
                messages.extend(self._tool_messages(segment))
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "stream": True,
            self.profile.max_tokens_param: request.max_output_tokens,
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
        if self.profile.stream_usage:
            payload["stream_options"] = {"include_usage": True}
        return payload

    def _assistant(self, segment: AssistantSegment, model: str) -> dict[str, Any]:
        message = segment.message
        entry: dict[str, Any] = {"role": "assistant"}
        if message.tool_calls:
            entry["content"] = message.text or None
            entry["tool_calls"] = [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.raw_arguments or "{}"},
                }
                for call in message.tool_calls
            ]
        else:
            entry["content"] = message.text
        if self.profile.reasoning_echo == "always":
            blocks = reasoning_for(message, self.profile.id, model)
            entry["reasoning_content"] = "".join(
                str(block.payload.get("text", "")) for block in blocks
            )
        return entry

    @staticmethod
    def _tool_messages(segment: ToolSegment) -> list[dict[str, Any]]:
        return [
            {"role": "tool", "tool_call_id": result.call_id, "content": result.content}
            for result in segment.results
        ]

    # --- appel --------------------------------------------------------------------

    def _client(self, idle_timeout_s: float) -> openai.OpenAI:
        if self.base_url is None:
            raise InvalidConfiguration(self.profile.label, "adresse de l'API manquante")
        if self.profile.key_required and not self.api_key:
            raise MissingKey(self.profile.label)
        timeout = httpx2.Timeout(idle_timeout_s + 10)  # filet de sécurité ; la veille est ailleurs
        return openai.OpenAI(
            api_key=self.api_key or NO_KEY_PLACEHOLDER,
            base_url=self.base_url,
            max_retries=0,
            timeout=timeout,
            http_client=self._http_client,
        )

    def complete(self, request: ChatRequest, cancel: CancelToken) -> AssistantMessage:
        client = self._client(request.idle_timeout_s)
        payload = self.build_payload(request)
        log_content(_log, f"requête {self.profile.id}", loggable_payload(payload))

        def work(guard: StreamGuard) -> AssistantMessage:
            return self._stream(client, payload, request.model, guard)

        return run_streaming(
            work, cancel=cancel, idle_timeout_s=request.idle_timeout_s, provider=self.profile.label
        )

    def _stream(
        self, client: openai.OpenAI, payload: dict[str, Any], model: str, guard: StreamGuard
    ) -> AssistantMessage:
        label = self.profile.label
        create: Any = client.chat.completions.create
        try:
            stream = cast(Iterable[Any], guard.attach(create(**payload)))
            guard.touch()
            assembler = _Assembler()
            for chunk in stream:
                guard.touch()
                assembler.add(chunk)
        except openai.APIStatusError as exc:
            message = error_message_of(exc.body, exc.message)
            if exc.status_code < 400:  # erreur envoyée au milieu du flux
                raise classify_stream_error(label, message, error_type_of(exc.body)) from exc
            raise classify_status(label, exc.status_code, message, exc.response.headers) from exc
        except (openai.APIConnectionError, httpx2.TransportError) as exc:
            raise NetworkError(label, f"{type(exc).__name__}: {exc}") from exc
        except openai.APIError as exc:  # erreur signalée au milieu du flux
            message = error_message_of(exc.body, exc.message)
            raise classify_stream_error(label, message, error_type_of(exc.body)) from exc
        except ProviderError:
            raise
        except Exception as exc:
            if guard.closed:  # flux fermé par le bouton stop ou la veille d'inactivité
                raise NetworkError(label, "flux interrompu") from exc
            raise
        return assembler.result(self.profile.id, model)


class _Assembler:
    def __init__(self) -> None:
        self.text: list[str] = []
        self.reasoning: list[str] = []
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
                self._delta(delta)
            if getattr(choice, "finish_reason", None):
                self.finish = str(getattr(choice, "finish_reason", ""))

    def _delta(self, delta: Any) -> None:
        content = getattr(delta, "content", None)
        if isinstance(content, str) and content:
            self.text.append(content)
        extra = _extra(delta)
        for field_name in _REASONING_FIELDS:
            value = extra.get(field_name)
            if isinstance(value, str) and value:
                self.reasoning.append(value)
                break
        for call in as_list(getattr(delta, "tool_calls", None)):
            function = getattr(call, "function", None)
            self.calls.add(
                getattr(call, "index", None),
                getattr(call, "id", None),
                getattr(function, "name", None) if function else None,
                getattr(function, "arguments", None) if function else None,
            )

    def result(self, provider: str, model: str) -> AssistantMessage:
        reasoning = (
            [ReasoningBlock(provider, model, {"text": "".join(self.reasoning)})]
            if self.reasoning
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
