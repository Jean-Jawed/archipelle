"""Module Anthropic : requêtes émises et flux décodés par le vrai SDK ``anthropic``."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from archipelle.core.cancel import CancelToken
from archipelle.core.outcome import Cancelled
from archipelle.providers import base
from archipelle.providers.anthropic import AnthropicProvider
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

MODEL = "claude-sonnet-5"


def start(input_tokens: int = 100) -> dict[str, Any]:
    return {
        "type": "message_start",
        "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "content": [],
            "model": MODEL, "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": input_tokens, "output_tokens": 1,
                      "cache_read_input_tokens": 50, "cache_creation_input_tokens": 0},
        },
    }  # fmt: skip


def block_start(index: int, block: dict[str, Any]) -> dict[str, Any]:
    return {"type": "content_block_start", "index": index, "content_block": block}


def delta(index: int, value: dict[str, Any]) -> dict[str, Any]:
    return {"type": "content_block_delta", "index": index, "delta": value}


def stop(index: int) -> dict[str, Any]:
    return {"type": "content_block_stop", "index": index}


def end(reason: str, output_tokens: int = 42) -> list[dict[str, Any]]:
    return [
        {"type": "message_delta", "delta": {"stop_reason": reason, "stop_sequence": None},
         "usage": {"output_tokens": output_tokens}},
        {"type": "message_stop"},
    ]  # fmt: skip


TOOL_STREAM = sse(
    start(),
    {"type": "ping"},
    block_start(0, {"type": "thinking", "thinking": "", "signature": ""}),
    delta(0, {"type": "thinking_delta", "thinking": "Je dois "}),
    delta(0, {"type": "thinking_delta", "thinking": "lire le bail."}),
    delta(0, {"type": "signature_delta", "signature": "SIG-abc"}),
    stop(0),
    block_start(1, {"type": "text", "text": ""}),
    delta(1, {"type": "text_delta", "text": "Je lis "}),
    delta(1, {"type": "text_delta", "text": "le bail."}),
    stop(1),
    block_start(2, {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {}}),
    delta(2, {"type": "input_json_delta", "partial_json": '{"path": '}),
    delta(2, {"type": "input_json_delta", "partial_json": '"bail.pdf"}'}),
    stop(2),
    block_start(3, {"type": "tool_use", "id": "toolu_2", "name": "list_files", "input": {}}),
    stop(3),
    *end("tool_use"),
    named=True,
)


def provider(recorder: Recorder, key: str | None = "sk-ant-test") -> AnthropicProvider:
    return AnthropicProvider(profile("anthropic"), key, http_client=recorder.httpx2_client())


def test_streamed_answer_keeps_reasoning_verbatim(recorder: Recorder) -> None:
    recorder.stream(TOOL_STREAM)
    answer = provider(recorder).complete(make_request(model=MODEL), CancelToken())
    assert answer.text == "Je lis le bail."
    assert [(c.call_id, c.name, c.arguments) for c in answer.tool_calls] == [
        ("toolu_1", "read_file", {"path": "bail.pdf"}),
        ("toolu_2", "list_files", {}),
    ]
    assert answer.stop_reason == "tool_calls"
    assert answer.usage is not None
    assert (answer.usage.input_tokens, answer.usage.output_tokens) == (150, 42)
    [block] = answer.reasoning
    assert (block.provider, block.model) == ("anthropic", MODEL)
    content = block.payload["content"]
    assert [c["type"] for c in content] == ["thinking", "text", "tool_use", "tool_use"]
    assert content[0] == {
        "type": "thinking",
        "thinking": "Je dois lire le bail.",
        "signature": "SIG-abc",
    }
    assert recorder.urls == ["https://api.anthropic.com/v1/messages"]
    assert recorder.headers[0]["x-api-key"] == "sk-ant-test"


def test_answer_without_reasoning(recorder: Recorder) -> None:
    recorder.stream(
        sse(
            start(),
            block_start(0, {"type": "text", "text": ""}),
            delta(0, {"type": "text_delta", "text": "Réponse [[notes.txt]]"}),
            stop(0),
            *end("end_turn", 5),
            named=True,
        )
    )
    answer = provider(recorder).complete(make_request(model=MODEL), CancelToken())
    assert answer.text == "Réponse [[notes.txt]]"
    assert answer.reasoning == [] and answer.tool_calls == []
    assert answer.stop_reason == "end"


def test_payload_translation_and_reasoning_echo(recorder: Recorder) -> None:
    recorder.stream(TOOL_STREAM)
    first = provider(recorder).complete(make_request(model=MODEL), CancelToken())
    items: list[PivotItem] = [
        TreeMessage("d", "arbre"),
        UserMessage("Quel loyer ?"),
        first,
        ToolResultGroup(
            [
                ToolResult("toolu_1", "read_file", "texte", marker="retiré"),
                ToolResult("toolu_2", "list_files", "", is_error=True),
            ]
        ),
        SystemNotice("scope", "Sous-dossier choisi"),
    ]
    payload = provider(recorder).build_payload(make_request(items, model=MODEL, tool_choice="none"))
    messages = payload["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert [b["type"] for b in messages[0]["content"]] == ["text"]
    # Même modèle : contenu renvoyé à l'identique, raisonnement et signature compris.
    assert messages[1]["content"] == first.reasoning[0].payload["content"]
    results = messages[2]["content"]
    assert [b["type"] for b in results] == ["tool_result", "tool_result", "text"]
    # Le contenu part tel quel : le marqueur ne sert qu'à l'archive et au budget.
    assert results[0] == {"type": "tool_result", "tool_use_id": "toolu_1",
                          "content": "texte", "is_error": False}  # fmt: skip
    assert results[1]["is_error"] is True and results[1]["content"]  # jamais vide
    assert "Sous-dossier choisi" in results[2]["text"]
    assert payload["tool_choice"] == {"type": "none"}
    assert payload["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert payload["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert payload["tools"][0]["input_schema"]["required"] == ["path"]
    assert payload["max_tokens"] == 1000 and payload["stream"] is True

    # Autre modèle : réponse reconstruite sans les blocs de raisonnement.
    other = provider(recorder).build_payload(make_request(items, model="claude-opus-5"))
    assert [b["type"] for b in other["messages"][1]["content"]] == ["text", "tool_use", "tool_use"]
    assert other["messages"][1]["content"][1]["input"] == {"path": "bail.pdf"}


def test_empty_assistant_message_gets_placeholder(recorder: Recorder) -> None:
    items: list[PivotItem] = [
        UserMessage("Q"),
        AssistantMessage("", "anthropic", MODEL),
        UserMessage("Q2"),
        AssistantMessage(
            "", "anthropic", MODEL, tool_calls=[ToolCall.from_raw("t", "read_file", "{bad")],
            reasoning=[ReasoningBlock("anthropic", MODEL, {"content": []})],
        ),
    ]  # fmt: skip
    messages = provider(recorder).build_payload(make_request(items, model=MODEL))["messages"]
    assert messages[1]["content"][0]["text"]
    assert messages[3]["content"] == [
        {"type": "tool_use", "id": "t", "name": "read_file", "input": {}}
    ]
    assert messages[4]["role"] == "user"  # résultat synthétique ajouté


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (401, {"type": "error", "error": {"type": "authentication_error",
                                          "message": "invalid x-api-key"}}, base.AuthError),
        (404, {"type": "error", "error": {"type": "not_found_error",
                                          "message": "model: claude-9"}}, base.ModelNotFound),
        (400, {"type": "error", "error": {"type": "invalid_request_error",
               "message": "prompt is too long: 1200000 tokens > 1000000 maximum"}},
         base.ContextTooLong),
        (400, {"type": "error", "error": {"type": "invalid_request_error",
               "message": "Your credit balance is too low to access the Anthropic API."}},
         base.QuotaExceeded),
        (429, {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}},
         base.RateLimited),
        (529, {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
         base.ServerError),
    ],
)  # fmt: skip
def test_http_errors_are_normalized(
    recorder: Recorder, status: int, body: dict[str, Any], expected: type[base.ProviderError]
) -> None:
    recorder.error(status, body, {"retry-after": "4"})
    with pytest.raises(expected):
        provider(recorder).complete(make_request(model=MODEL), CancelToken())
    assert len(recorder.requests) == 1


def test_error_event_inside_stream(recorder: Recorder) -> None:
    recorder.stream(
        sse(
            start(),
            {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
            named=True,
        )
    )
    with pytest.raises(base.ServerError):
        provider(recorder).complete(make_request(model=MODEL), CancelToken())


def test_missing_key_idle_and_stop(recorder: Recorder) -> None:
    with pytest.raises(base.MissingKey):
        provider(recorder, key="").complete(make_request(model=MODEL), CancelToken())

    recorder.slow_stream(sse(start(), named=True), pause_s=3)
    started = time.monotonic()
    with pytest.raises(base.IdleTimeout):
        provider(recorder).complete(make_request(model=MODEL, idle=0.3), CancelToken())
    assert time.monotonic() - started < 2

    recorder.slow_stream(sse(start(), named=True), pause_s=3)
    token = CancelToken()
    threading.Timer(0.3, token.cancel).start()
    with pytest.raises(Cancelled):
        provider(recorder).complete(make_request(model=MODEL, idle=30), token)
