"""Module « compatible OpenAI » : requêtes émises et flux décodés par le vrai SDK."""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx2
import pytest

from archipelle.core.cancel import CancelToken
from archipelle.core.outcome import Cancelled
from archipelle.providers import base
from archipelle.providers.openai_compat import NO_KEY_PLACEHOLDER, OpenAICompatProvider
from archipelle.providers.pivot import (
    AssistantMessage,
    PivotItem,
    ReasoningBlock,
    SystemNotice,
    ToolCall,
    ToolResult,
    ToolResultGroup,
    TreeMessage,
    UserMessage,
)
from tests.providers.conftest import Recorder, make_request, profile, sse


def chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    return {
        "id": "c",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "m",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


USAGE: dict[str, Any] = {
    "id": "c",
    "object": "chat.completion.chunk",
    "created": 1,
    "model": "m",
    "choices": [],
    "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
}

TOOL_STREAM = sse(
    chunk({"role": "assistant", "content": "", "reasoning_content": "Je dois "}),
    chunk({"reasoning_content": "lire le bail."}),
    chunk({"content": "Je lis "}),
    chunk({"content": "le fichier."}),
    chunk({"tool_calls": [{"index": 0, "id": "call_a", "type": "function",
                           "function": {"name": "read_file", "arguments": '{"pa'}}]}),
    chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'th": "bail.pdf"}'}}]}),
    chunk({"tool_calls": [{"index": 1, "id": "call_b", "type": "function",
                           "function": {"name": "list_files", "arguments": "{}"}}]}),
    chunk({}, "tool_calls"),
    USAGE,
    done=True,
)  # fmt: skip


def provider(recorder: Recorder, provider_id: str = "deepseek", key: str | None = "sk-test",
             base_url: str | None = None) -> OpenAICompatProvider:  # fmt: skip
    return OpenAICompatProvider(
        profile(provider_id), key, base_url=base_url, http_client=recorder.httpx2_client()
    )


HISTORY: list[PivotItem] = [
    TreeMessage("d1", "arbre"),
    UserMessage("Quel loyer ?"),
    AssistantMessage(
        "",
        "deepseek",
        "deepseek-flash",
        tool_calls=[ToolCall.from_arguments("call_1", "read_file", {"path": "bail.pdf"})],
        reasoning=[ReasoningBlock("deepseek", "deepseek-flash", {"text": "réflexion 1"})],
    ),
    ToolResultGroup([ToolResult("call_1", "read_file", "texte complet", marker="bail.pdf retiré")]),
    AssistantMessage(
        "Le loyer est de 850 €.",
        "deepseek",
        "deepseek-v4-pro",
        reasoning=[ReasoningBlock("deepseek", "deepseek-v4-pro", {"text": "autre modèle"})],
    ),
    SystemNotice("workdir", "Nouveau dossier"),
    UserMessage("Et les charges ?"),
]


def test_payload_translation_with_reasoning_echo(recorder: Recorder) -> None:
    payload = provider(recorder).build_payload(make_request(HISTORY, model="deepseek-flash"))
    messages = payload["messages"]
    assert [m["role"] for m in messages] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
    ]
    assert messages[1]["content"].startswith('<archipelle-notice kind="tree">')
    assert messages[1]["content"].endswith("Quel loyer ?")
    call_message = messages[2]
    assert call_message["content"] is None
    assert call_message["tool_calls"][0] == {
        "id": "call_1",
        "type": "function",
        "function": {"name": "read_file", "arguments": '{"path": "bail.pdf"}'},
    }
    assert call_message["reasoning_content"] == "réflexion 1"
    assert messages[3] == {"role": "tool", "tool_call_id": "call_1", "content": "bail.pdf retiré"}
    # Raisonnement d'un autre modèle : retiré, mais le champ reste présent (exigé par DeepSeek).
    assert messages[4]["reasoning_content"] == ""
    assert messages[4]["content"] == "Le loyer est de 850 €."
    assert "Nouveau dossier" in messages[5]["content"] and "charges" in messages[5]["content"]
    assert payload["max_tokens"] == 1000 and "max_completion_tokens" not in payload
    assert payload["tool_choice"] == "auto" and payload["stream"] is True
    assert payload["tools"][0]["function"]["name"] == "read_file"
    assert payload["stream_options"] == {"include_usage": True}


def test_payload_without_reasoning_echo(recorder: Recorder) -> None:
    payload = provider(recorder, "openai").build_payload(
        make_request(HISTORY, model="deepseek-flash", tool_choice="none")
    )
    assert all("reasoning_content" not in m for m in payload["messages"])
    assert payload["max_completion_tokens"] == 1000 and "max_tokens" not in payload
    assert payload["tool_choice"] == "none"


def test_payload_bridge_after_interrupted_tools(recorder: Recorder) -> None:
    items: list[PivotItem] = [
        UserMessage("Q"),
        AssistantMessage("", "openai", "m", tool_calls=[ToolCall.from_raw("c1", "read_file", "")]),
        ToolResultGroup([ToolResult("c1", "read_file", "interrompu", synthetic=True)]),
        UserMessage("Q2"),
    ]
    messages = provider(recorder, "openai").build_payload(make_request(items))["messages"]
    assert [m["role"] for m in messages] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
    ]
    assert messages[2]["tool_calls"][0]["function"]["arguments"] == "{}"
    assert messages[4]["content"]


def test_payload_without_tools(recorder: Recorder) -> None:
    payload = provider(recorder, "openai").build_payload(make_request(tools=[]))
    assert "tools" not in payload and "tool_choice" not in payload


def test_streamed_answer_is_assembled(recorder: Recorder) -> None:
    recorder.stream(TOOL_STREAM)
    answer = provider(recorder).complete(make_request(model="deepseek-flash"), CancelToken())
    assert answer.text == "Je lis le fichier."
    assert [(c.call_id, c.name, c.arguments) for c in answer.tool_calls] == [
        ("call_a", "read_file", {"path": "bail.pdf"}),
        ("call_b", "list_files", {}),
    ]
    assert answer.reasoning == [
        ReasoningBlock("deepseek", "deepseek-flash", {"text": "Je dois lire le bail."})
    ]
    assert answer.stop_reason == "tool_calls"
    assert answer.usage is not None and answer.usage.input_tokens == 120
    assert (answer.provider, answer.model) == ("deepseek", "deepseek-flash")
    assert recorder.urls == ["https://api.deepseek.com/chat/completions"]
    assert recorder.headers[0]["authorization"] == "Bearer sk-test"
    assert recorder.requests[0]["model"] == "deepseek-flash"


def test_alternative_reasoning_field_and_plain_answer(recorder: Recorder) -> None:
    recorder.stream(
        sse(
            chunk({"role": "assistant", "reasoning": "réflexion locale"}),
            chunk({"content": "Réponse [[notes.txt]]"}, "stop"),
            done=True,
        )
    )
    answer = provider(recorder, "custom", key=None, base_url="http://localhost:11434/v1").complete(
        make_request(), CancelToken()
    )
    assert answer.text == "Réponse [[notes.txt]]" and not answer.tool_calls
    assert answer.reasoning[0].payload == {"text": "réflexion locale"}
    assert answer.stop_reason == "end" and answer.usage is None
    assert recorder.urls == ["http://localhost:11434/v1/chat/completions"]
    assert recorder.headers[0]["authorization"] == f"Bearer {NO_KEY_PLACEHOLDER}"


def test_configuration_errors(recorder: Recorder) -> None:
    with pytest.raises(base.MissingKey):
        provider(recorder, "openai", key="  ").complete(make_request(), CancelToken())
    with pytest.raises(base.InvalidConfiguration):
        provider(recorder, "custom", key=None).complete(make_request(), CancelToken())
    assert recorder.requests == []


@pytest.mark.parametrize(
    ("status", "body", "headers", "expected"),
    [
        (401, {"error": {"message": "Incorrect API key provided", "type": "invalid_request_error"}},
         {}, base.AuthError),
        (404, {"error": {"message": "The model `gpt-x` does not exist", "code": "model_not_found"}},
         {}, base.ModelNotFound),
        (429, {"error": {"message": "Rate limit reached"}}, {"retry-after": "3"}, base.RateLimited),
        (429, {"error": {"message": "You exceeded your current quota",
                         "code": "insufficient_quota"}},
         {}, base.QuotaExceeded),
        (400, {"error": {"message": "registry.ollama.ai/library/gemma3 does not support tools"}},
         {}, base.ToolsUnsupported),
        (400, {"error": {"message": "This model's maximum context length is 32768 tokens"}},
         {}, base.ContextTooLong),
        (400, {"error": {"message": "The reasoning_content in thinking mode must be passed back"}},
         {}, base.BadRequest),
        (503, {"error": {"message": "Service unavailable"}}, {}, base.ServerError),
        (402, {"error": {"message": "Insufficient Balance"}}, {}, base.QuotaExceeded),
    ],
)  # fmt: skip
def test_http_errors_are_normalized(
    recorder: Recorder,
    status: int,
    body: dict[str, Any],
    headers: dict[str, str],
    expected: type[base.ProviderError],
) -> None:
    recorder.error(status, body, headers)
    with pytest.raises(expected) as info:
        provider(recorder).complete(make_request(), CancelToken())
    assert info.value.provider == "DeepSeek"
    assert len(recorder.requests) == 1  # aucune tentative intégrée au SDK
    if isinstance(info.value, base.RateLimited):
        assert info.value.retry_after == 3


def test_connection_error(recorder: Recorder) -> None:
    request = httpx2.Request("POST", "https://api.deepseek.com/chat/completions")
    error = httpx2.ConnectError("refusé", request=request)
    recorder.responses.append(lambda: (0, {}, error))
    with pytest.raises(base.NetworkError):
        provider(recorder).complete(make_request(), CancelToken())


def test_error_event_inside_stream(recorder: Recorder) -> None:
    recorder.stream(
        sse(chunk({"content": "début"}), {"error": {"message": "Upstream overloaded"}}, done=True)
    )
    with pytest.raises(base.ServerError):
        provider(recorder).complete(make_request(), CancelToken())


def test_idle_stream_times_out(recorder: Recorder) -> None:
    recorder.slow_stream(sse(chunk({"content": "début"})), pause_s=3)
    started = time.monotonic()
    with pytest.raises(base.IdleTimeout):
        provider(recorder).complete(make_request(idle=0.3), CancelToken())
    assert time.monotonic() - started < 2


def test_stop_button_interrupts_stream(recorder: Recorder) -> None:
    recorder.slow_stream(sse(chunk({"content": "début"})), pause_s=3)
    token = CancelToken()
    threading.Timer(0.3, token.cancel).start()
    started = time.monotonic()
    with pytest.raises(Cancelled):
        provider(recorder).complete(make_request(idle=30), token)
    assert time.monotonic() - started < 2
