"""Module Mistral : requêtes émises et flux décodés par le vrai SDK ``mistralai``."""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx
import pytest

from archipelle.core.cancel import CancelToken
from archipelle.core.outcome import Cancelled
from archipelle.providers import base
from archipelle.providers.mistral import MistralProvider
from archipelle.providers.pivot import (
    AssistantMessage,
    PivotItem,
    ReasoningBlock,
    SystemNotice,
    ToolCall,
    ToolResult,
    ToolResultGroup,
    UserMessage,
)
from tests.providers.conftest import Recorder, make_request, profile, sse


def chunk(delta: dict[str, Any], finish: str | None = None, usage: bool = False) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "x",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "mistral-small-latest",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage:
        data["usage"] = {"prompt_tokens": 80, "completion_tokens": 12, "total_tokens": 92}
    return data


def provider(recorder: Recorder, key: str | None = "cle-mistral") -> MistralProvider:
    return MistralProvider(profile("mistral"), key, http_client=recorder.httpx_client())


def test_payload_translation(recorder: Recorder) -> None:
    items: list[PivotItem] = [
        UserMessage("Quel loyer ?"),
        AssistantMessage(
            "",
            "mistral",
            "mistral-medium-latest",
            tool_calls=[ToolCall.from_arguments("abcDEF123", "read_file", {"path": "bail.pdf"})],
            reasoning=[ReasoningBlock("mistral", "mistral-medium-latest", {"text": "réflexion"})],
        ),
        ToolResultGroup([ToolResult("abcDEF123", "read_file", "contenu")]),
        SystemNotice("stop", "Recherche interrompue"),
        UserMessage("Réessaie"),
    ]
    payload = provider(recorder).build_payload(
        make_request(items, model="mistral-medium-latest", tool_choice="none")
    )
    messages = payload["messages"]
    assert [m["role"] for m in messages] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
    ]
    assert "content" not in messages[2]  # appel d'outil sans texte
    assert messages[2]["tool_calls"][0]["function"] == {
        "name": "read_file",
        "arguments": '{"path": "bail.pdf"}',
    }
    assert all("reasoning" not in str(m.keys()) for m in messages)
    assert "réflexion" not in str(messages)  # le raisonnement n'est jamais renvoyé
    assert messages[3] == {
        "role": "tool",
        "tool_call_id": "abcDEF123",
        "name": "read_file",
        "content": "contenu",
    }
    assert messages[4]["content"]  # message de liaison exigé par l'API
    assert "Recherche interrompue" in messages[5]["content"]
    assert payload["tool_choice"] == "none" and payload["max_tokens"] == 1000


def test_streamed_tool_calls_and_thinking(recorder: Recorder) -> None:
    thinking = {"type": "thinking", "thinking": [{"type": "text", "text": "Je cherche le bail."}]}
    recorder.stream(
        sse(
            chunk({"role": "assistant", "content": [thinking]}),
            chunk({"content": [{"type": "text", "text": "Je lis "}]}),
            chunk({"content": "le bail."}),
            chunk({"tool_calls": [{"id": "abcDEF123", "index": 0, "function":
                   {"name": "read_file", "arguments": '{"path": "bail.pdf"}'}}]}),
            chunk({"tool_calls": [{"id": "ghiJKL456", "index": 1, "function":
                   {"name": "search_fulltext", "arguments": {"keywords": ["loyer"]}}}]}),
            chunk({"content": ""}, "tool_calls", usage=True),
            done=True,
        )
    )  # fmt: skip
    answer = provider(recorder).complete(make_request(model="mistral-small-latest"), CancelToken())
    assert answer.text == "Je lis le bail."
    assert [(c.call_id, c.name, c.arguments) for c in answer.tool_calls] == [
        ("abcDEF123", "read_file", {"path": "bail.pdf"}),
        ("ghiJKL456", "search_fulltext", {"keywords": ["loyer"]}),
    ]
    assert answer.reasoning[0].payload == {"text": "Je cherche le bail."}
    assert answer.stop_reason == "tool_calls"
    assert answer.usage is not None and answer.usage.output_tokens == 12
    assert [u.split("#")[0] for u in recorder.urls] == [
        "https://api.mistral.ai/v1/chat/completions"
    ]
    assert recorder.headers[0]["authorization"] == "Bearer cle-mistral"
    assert recorder.requests[0]["stream"] is True


def test_missing_key(recorder: Recorder) -> None:
    with pytest.raises(base.MissingKey):
        provider(recorder, key=None).complete(make_request(), CancelToken())


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (401, {"message": "Unauthorized", "request_id": "r"}, base.AuthError),
        (400, {"object": "error", "message": "Invalid model: mistral-geant",
               "type": "invalid_model", "code": "1500"}, base.ModelNotFound),
        (400, {"object": "error", "message": "Unexpected role 'user' after role 'tool'",
               "type": "invalid_request_message_order"}, base.BadRequest),
        (422, {"object": "error", "message": {"detail": [{"msg": "Field required"}]}},
         base.BadRequest),
        (429, {"message": "Requests rate limit exceeded"}, base.RateLimited),
        (503, {"message": "Service unavailable"}, base.ServerError),
    ],
)  # fmt: skip
def test_http_errors_are_normalized(
    recorder: Recorder, status: int, body: dict[str, Any], expected: type[base.ProviderError]
) -> None:
    recorder.error(status, body)
    with pytest.raises(expected):
        provider(recorder).complete(make_request(), CancelToken())
    assert len(recorder.requests) == 1  # aucune tentative intégrée au SDK


def test_connection_error(recorder: Recorder) -> None:
    error = httpx.ConnectError("refusé")
    recorder.responses.append(lambda: (0, {}, error))
    with pytest.raises(base.NetworkError):
        provider(recorder).complete(make_request(), CancelToken())


def test_idle_and_stop(recorder: Recorder) -> None:
    recorder.slow_stream(sse(chunk({"content": "début"})), pause_s=3)
    started = time.monotonic()
    with pytest.raises(base.IdleTimeout):
        provider(recorder).complete(make_request(idle=0.3), CancelToken())
    assert time.monotonic() - started < 2

    recorder.slow_stream(sse(chunk({"content": "début"})), pause_s=3)
    token = CancelToken()
    threading.Timer(0.3, token.cancel).start()
    with pytest.raises(Cancelled):
        provider(recorder).complete(make_request(idle=30), token)
