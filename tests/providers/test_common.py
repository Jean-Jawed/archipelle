"""Pivot, segmentation, erreurs, nouvelles tentatives, streaming, catalogue, fabrique."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from archipelle.core.cancel import CancelToken
from archipelle.core.outcome import Cancelled
from archipelle.providers import base
from archipelle.providers.catalog import CatalogError, ExtraModel, load_catalog, parse_catalog
from archipelle.providers.common import (
    AssistantSegment,
    ToolCallAccumulator,
    ToolSegment,
    UserSegment,
    normalize_stop_reason,
    segments,
)
from archipelle.providers.factory import (
    ProviderConfig,
    create_provider,
    is_cleartext_remote,
    is_local_url,
    validate_base_url,
)
from archipelle.providers.pivot import (
    AssistantMessage,
    PivotFormatError,
    PivotItem,
    ReasoningBlock,
    SystemNotice,
    ToolCall,
    ToolResult,
    ToolResultGroup,
    TreeMessage,
    Usage,
    UserMessage,
    from_dict,
    notice_text,
    reasoning_for,
    to_dict,
)
from archipelle.providers.retry import RetryPolicy, call_with_retry
from archipelle.providers.streaming import StreamGuard, run_streaming

CALL = ToolCall.from_arguments("c1", "read_file", {"path": "a.txt"})
CALL2 = ToolCall.from_arguments("c2", "list_files", {})


def _assistant(*calls: ToolCall, text: str = "", model: str = "m") -> AssistantMessage:
    return AssistantMessage(text, "p", model, tool_calls=list(calls))


# --- pivot ---------------------------------------------------------------------------


def test_pivot_roundtrip() -> None:
    items = [
        TreeMessage("abc", "arbre"),
        SystemNotice("workdir_changed", "Nouveau dossier"),
        UserMessage("Question é"),
        AssistantMessage(
            "texte",
            "anthropic",
            "claude",
            tool_calls=[CALL, ToolCall.from_raw("c3", "compute", "{pas du json")],
            reasoning=[ReasoningBlock("anthropic", "claude", {"content": [{"type": "thinking"}]})],
            usage=Usage(10, 5),
            stop_reason="tool_calls",
        ),
        ToolResultGroup(
            [
                ToolResult("c1", "read_file", "contenu", marker="retiré"),
                ToolResult("c3", "compute", "erreur", is_error=True, synthetic=True),
            ]
        ),
    ]
    assert [from_dict(to_dict(item)) for item in items] == items


def test_pivot_rejects_bad_data() -> None:
    with pytest.raises(PivotFormatError):
        from_dict({"type": "inconnu"})
    with pytest.raises(PivotFormatError):
        from_dict({"type": "assistant", "text": "x"})


def test_tool_call_arguments() -> None:
    broken = ToolCall.from_raw("c", "read_file", '{"path": ')
    assert broken.arguments == {} and not broken.arguments_valid
    assert ToolCall.from_raw("c", "list_files", "").arguments_valid
    assert ToolCall.from_raw("c", "x", "[1, 2]").arguments == {}
    assert CALL.raw_arguments == '{"path": "a.txt"}'


def test_result_marker_and_notices() -> None:
    assert ToolResult("c", "n", "long", marker="court").sent_content == "court"
    assert ToolResult("c", "n", "long").sent_content == "long"
    assert notice_text(TreeMessage("d", "x")).startswith('<archipelle-notice kind="tree">')
    assert 'kind="scope"' in notice_text(SystemNotice("scope", "y"))


def test_reasoning_filter() -> None:
    message = AssistantMessage(
        "",
        "deepseek",
        "flash",
        reasoning=[
            ReasoningBlock("deepseek", "flash", {"text": "a"}),
            ReasoningBlock("deepseek", "pro", {"text": "b"}),
            ReasoningBlock("kimi", "flash", {"text": "c"}),
        ],
    )
    assert [r.payload["text"] for r in reasoning_for(message, "deepseek", "flash")] == ["a"]


# --- segmentation --------------------------------------------------------------------


def test_segments_merge_user_side_messages() -> None:
    result = segments([TreeMessage("d", "arbre"), SystemNotice("k", "note"), UserMessage("Q")])
    assert len(result) == 1
    assert isinstance(result[0], UserSegment)
    assert result[0].text.endswith("\n\nQ") and "arbre" in result[0].text


def test_segments_pair_results_in_call_order() -> None:
    group = ToolResultGroup(
        [ToolResult("c2", "list_files", "b"), ToolResult("c1", "read_file", "a")]
    )
    result = segments([UserMessage("Q"), _assistant(CALL, CALL2), group])
    tools = result[2]
    assert isinstance(tools, ToolSegment)
    assert [r.call_id for r in tools.results] == ["c1", "c2"]


def test_segments_repair_missing_and_orphan_results() -> None:
    orphan = ToolResultGroup([ToolResult("zz", "x", "orphelin")])
    result = segments([UserMessage("Q"), _assistant(CALL), UserMessage("Q2"), orphan])
    tools = result[2]
    assert isinstance(tools, ToolSegment)
    assert tools.results[0].synthetic and tools.results[0].call_id == "c1"
    assert isinstance(result[3], UserSegment)
    assert len(result) == 4  # le groupe orphelin est écarté

    mixed = ToolResultGroup([ToolResult("c1", "read_file", "ok"), ToolResult("zz", "x", "?")])
    paired = segments([_assistant(CALL), mixed])[1]
    assert isinstance(paired, ToolSegment) and [r.content for r in paired.results] == ["ok"]


def test_segments_bridge_after_tools() -> None:
    items: list[PivotItem] = [
        UserMessage("Q"),
        _assistant(CALL),
        ToolResultGroup([ToolResult("c1", "r", "x")]),
        SystemNotice("stop", "Recherche interrompue"),
        UserMessage("Q2"),
    ]
    plain = segments(items)
    assert [type(s).__name__ for s in plain] == [
        "UserSegment", "AssistantSegment", "ToolSegment", "UserSegment"
    ]  # fmt: skip
    bridged = segments(items, bridge_after_tools=True)
    assert [type(s).__name__ for s in bridged] == [
        "UserSegment", "AssistantSegment", "ToolSegment", "AssistantSegment", "UserSegment"
    ]  # fmt: skip
    bridge = bridged[3]
    assert isinstance(bridge, AssistantSegment) and bridge.bridge and bridge.message.text
    last = bridged[4]
    assert isinstance(last, UserSegment) and len(last.texts) == 2


def test_accumulator_split_arguments() -> None:
    acc = ToolCallAccumulator()
    acc.add(0, "id1", "read_file", '{"pa')
    acc.add(1, "id2", "list_files", "")
    acc.add(0, None, None, 'th": "a"}')
    acc.add(1, None, None, "{}")
    calls = acc.calls()
    assert [(c.call_id, c.name, c.arguments) for c in calls] == [
        ("id1", "read_file", {"path": "a"}),
        ("id2", "list_files", {}),
    ]


def test_accumulator_tolerates_server_quirks() -> None:
    repeated = ToolCallAccumulator()
    repeated.add(0, "id1", "read_file", '{"path"')
    repeated.add(0, "id1", "read_file", ': "a"}')  # nom et identifiant répétés
    assert [(c.name, c.arguments) for c in repeated.calls()] == [("read_file", {"path": "a"})]

    no_index = ToolCallAccumulator()
    no_index.add(None, "a", "read_file", '{"path": "x"}')
    no_index.add(None, "b", "read_file", '{"path": "y"}')
    assert [c.arguments["path"] for c in no_index.calls()] == ["x", "y"]

    same_index = ToolCallAccumulator()
    same_index.add(0, "a", "read_file", '{"path": "x"}')
    same_index.add(0, "b", "list_files", "{}")
    assert [c.name for c in same_index.calls()] == ["read_file", "list_files"]

    no_id = ToolCallAccumulator()
    no_id.add(0, None, "compute", "{}")
    no_id.add(0, None, None, "")
    no_id.add(1, None, None, "orphelin sans nom")
    assert [c.call_id for c in no_id.calls()] == ["call_1"]


def test_stop_reasons() -> None:
    assert normalize_stop_reason("stop") == "end"
    assert normalize_stop_reason("tool_use") == "tool_calls"
    assert normalize_stop_reason("max_tokens") == "length"
    assert normalize_stop_reason("content_filter") == "other"
    assert normalize_stop_reason(None) is None


# --- classification des erreurs ------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "message", "expected"),
    [
        (401, "invalid api key", base.AuthError),
        (403, "forbidden", base.AuthError),
        (429, "rate limit", base.RateLimited),
        (429, "You exceeded your current quota", base.QuotaExceeded),
        (402, "payment", base.QuotaExceeded),
        (400, "Your credit balance is too low", base.QuotaExceeded),
        (404, "The model `gpt-9` does not exist", base.ModelNotFound),
        (400, "Invalid model: foo", base.ModelNotFound),
        (400, "This model does not support tools", base.ToolsUnsupported),
        (400, "registry.ollama.ai/library/gemma does not support tools", base.ToolsUnsupported),
        (400, "Function calling is not supported for this model", base.ToolsUnsupported),
        (400, "prompt is too long: 250000 tokens > 200000 maximum", base.ContextTooLong),
        (400, "This model's maximum context length is 8192 tokens", base.ContextTooLong),
        (413, "request too large", base.ContextTooLong),
        (400, "Unexpected role 'user' after role 'tool'", base.BadRequest),
        (422, "invalid body", base.BadRequest),
        (500, "internal", base.ServerError),
        (529, "overloaded", base.ServerError),
        (408, "timeout", base.ServerError),
        (418, "teapot", base.BadRequest),
    ],
)
def test_classify_status(status: int, message: str, expected: type[base.ProviderError]) -> None:
    error = base.classify_status("Fournisseur", status, message)
    assert type(error) is expected
    assert error.provider == "Fournisseur"
    assert not error.message().startswith("errors.")


def test_retry_after_parsing() -> None:
    assert base.parse_retry_after({"Retry-After": "12"}) == 12
    assert base.parse_retry_after({"retry-after-ms": "1500"}) == 1.5
    assert base.parse_retry_after({}) is None
    assert base.parse_retry_after({"retry-after": "n'importe quoi"}) is None
    future = base.parse_retry_after({"retry-after": "Wed, 21 Oct 2099 07:28:00 GMT"})
    assert future is not None and future > 0
    error = base.classify_status("P", 429, "x", {"retry-after": "7"})
    assert isinstance(error, base.RateLimited) and error.retry_after == 7


def test_error_message_extraction() -> None:
    assert (
        base.error_message_of({"error": {"message": "clé invalide", "type": "auth"}})
        == "clé invalide"
    )
    assert (
        base.error_message_of({"detail": [{"msg": "champ", "message": "manquant"}]}) == "manquant"
    )
    assert base.error_message_of({"object": "error", "message": "rôle"}) == "rôle"
    assert base.error_message_of({"type": "overloaded_error"}) == "overloaded_error"
    assert base.error_message_of(None, "défaut") == "défaut"
    assert base.error_message_of("texte brut") == "texte brut"


def test_error_messages_are_translated() -> None:
    assert "Paramètres" in base.MissingKey("Mistral").message()
    assert "120 secondes" in base.IdleTimeout("Mistral", 120).message()
    assert "adresse manquante" in base.InvalidConfiguration("X", "adresse manquante").message()
    assert base.RateLimited("X").retryable and base.ServerError("X").retryable
    assert not base.AuthError("X").retryable and not base.QuotaExceeded("X").retryable


# --- nouvelles tentatives ------------------------------------------------------------

FAST = RetryPolicy(network_delays_s=(0.01, 0.02), rate_limit_delays_s=(0.01, 0.02), max_wait_s=0.05)


def _sequence(*outcomes: Any) -> Any:
    remaining = list(outcomes)

    def call() -> Any:
        outcome = remaining.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return call


def test_retry_recovers_after_transient_errors() -> None:
    seen: list[tuple[str, int]] = []
    result = call_with_retry(
        _sequence(base.NetworkError("P"), base.RateLimited("P", retry_after=0.01), "ok"),
        cancel=CancelToken(),
        policy=FAST,
        on_retry=lambda error, attempt, wait: seen.append((type(error).__name__, attempt)),
    )
    assert result == "ok"
    assert seen == [("NetworkError", 1), ("RateLimited", 2)]


def test_retry_gives_up_after_two_retries() -> None:
    calls = _sequence(
        base.ServerError("P"), base.ServerError("P"), base.ServerError("P"), "trop tard"
    )
    with pytest.raises(base.ServerError):
        call_with_retry(calls, cancel=CancelToken(), policy=FAST)


@pytest.mark.parametrize(
    "error",
    [base.AuthError("P"), base.ModelNotFound("P"), base.ToolsUnsupported("P"),
     base.QuotaExceeded("P"), base.BadRequest("P"), base.ContextTooLong("P")],
)  # fmt: skip
def test_no_retry_for_permanent_errors(error: base.ProviderError) -> None:
    attempts: list[int] = []

    def call() -> str:
        attempts.append(1)
        raise error

    with pytest.raises(type(error)):
        call_with_retry(call, cancel=CancelToken(), policy=FAST)
    assert len(attempts) == 1


def test_retry_delays() -> None:
    policy = RetryPolicy()
    assert policy.delay(base.NetworkError("P"), 0) < policy.delay(base.NetworkError("P"), 1)
    assert policy.delay(base.RateLimited("P"), 0) < policy.delay(base.RateLimited("P"), 1)
    assert policy.delay(base.RateLimited("P", retry_after=3600), 0) == policy.max_wait_s
    assert policy.delay(base.RateLimited("P", retry_after=0), 0) == 1.0


def test_retry_wait_is_cancellable() -> None:
    token = CancelToken()
    threading.Timer(0.1, token.cancel).start()
    started = time.monotonic()
    with pytest.raises(Cancelled):
        call_with_retry(
            _sequence(base.RateLimited("P", retry_after=30), "jamais"),
            cancel=token,
            policy=RetryPolicy(max_wait_s=30),
        )
    assert time.monotonic() - started < 2


# --- exécution interruptible ---------------------------------------------------------


class _Resource:
    def __init__(self) -> None:
        self.closed = threading.Event()

    def close(self) -> None:
        self.closed.set()


def test_run_streaming_returns_value_and_errors() -> None:
    assert run_streaming(lambda g: 42, cancel=CancelToken(), idle_timeout_s=1, provider="P") == 42

    def failing(guard: StreamGuard) -> int:
        raise base.AuthError("P")

    with pytest.raises(base.AuthError):
        run_streaming(failing, cancel=CancelToken(), idle_timeout_s=1, provider="P")


def test_run_streaming_cancel_closes_stream_immediately() -> None:
    token = CancelToken()
    resource = _Resource()

    def work(guard: StreamGuard) -> str:
        guard.attach(resource)
        resource.closed.wait(10)
        return "trop tard"

    threading.Timer(0.1, token.cancel).start()
    started = time.monotonic()
    with pytest.raises(Cancelled):
        run_streaming(work, cancel=token, idle_timeout_s=30, provider="P")
    assert time.monotonic() - started < 1
    assert resource.closed.is_set()


def test_run_streaming_idle_timeout_but_activity_keeps_alive() -> None:
    resource = _Resource()

    def stalled(guard: StreamGuard) -> str:
        guard.attach(resource)
        resource.closed.wait(10)
        return "trop tard"

    with pytest.raises(base.IdleTimeout):
        run_streaming(stalled, cancel=CancelToken(), idle_timeout_s=0.2, provider="P")
    assert resource.closed.is_set()

    def active(guard: StreamGuard) -> str:
        for _ in range(8):
            time.sleep(0.05)
            guard.touch()
        return "fini"

    assert run_streaming(active, cancel=CancelToken(), idle_timeout_s=0.2, provider="P") == "fini"


def test_run_streaming_refuses_when_already_cancelled() -> None:
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        run_streaming(lambda g: 1, cancel=token, idle_timeout_s=1, provider="P")


def test_guard_closes_late_resources() -> None:
    guard = StreamGuard()
    guard.close()
    late = _Resource()
    guard.attach(late)
    assert late.closed.is_set()


# --- catalogue -----------------------------------------------------------------------


def test_catalog_is_consistent() -> None:
    catalog = load_catalog()
    assert set(catalog.providers) == {
        "mistral",
        "openai",
        "anthropic",
        "deepseek",
        "kimi",
        "custom",
    }
    for profile in catalog.providers.values():
        if profile.is_custom:
            assert profile.base_url is None and not profile.key_required
            continue
        assert profile.models
        for mode in ("quick", "explore"):
            default = profile.default_model(mode)
            assert default is not None
            assert catalog.model(profile.id, default).in_catalog
        if profile.api == "openai_compat":
            assert profile.base_url and profile.base_url.startswith("https://")
    assert catalog.provider("mistral").region == "eu"
    assert catalog.provider("deepseek").outside_eu and catalog.provider("kimi").outside_eu
    assert catalog.provider("deepseek").reasoning_echo == "always"
    assert catalog.provider("openai").max_tokens_param == "max_completion_tokens"


def test_catalog_models_and_fallback() -> None:
    catalog = load_catalog()
    sonnet = catalog.model("anthropic", "claude-sonnet-5")
    assert sonnet.context_window == 1_000_000
    assert sonnet.usable_context == catalog.max_context_tokens == 200_000
    unknown = catalog.model("mistral", "modele-inconnu")
    assert not unknown.in_catalog and unknown.tools
    assert (unknown.context_window, unknown.max_output) == (32000, 4000)
    extra = [ExtraModel("llama-local", 8192, 1024)]
    local = catalog.model("custom", "llama-local", extra)
    assert local.usable_context == 8192 and local.max_output == 1024
    selectable = catalog.selectable_models("custom", extra)
    assert [m.id for m in selectable] == ["llama-local"]
    duplicate = catalog.selectable_models("mistral", [ExtraModel("mistral-small-latest")])
    assert [m.id for m in duplicate].count("mistral-small-latest") == 1
    with pytest.raises(CatalogError):
        catalog.provider("inconnu")


def _catalog_data(**model: Any) -> dict[str, Any]:
    entry = {"id": "m1", "context_window": 1000, "max_output": 100, **model}
    return {
        "providers": {
            "p": {"api": "mistral", "region": "eu", "reasoning_echo": "none",
                  "defaults": {"quick": "m1", "explore": None}, "models": [entry]}
        }
    }  # fmt: skip


def test_catalog_validation() -> None:
    parsed = parse_catalog(_catalog_data())
    assert parsed.model("p", "m1").usable_context == 1000
    assert [m.id for m in parsed.selectable_models("p")] == ["m1"]
    assert parse_catalog(_catalog_data(tools=False)).selectable_models("p") == []
    broken_cases: list[dict[str, Any]] = [
        _catalog_data(context_window=0),
        _catalog_data(max_output="beaucoup"),
        _catalog_data(id=""),
        {"providers": {}},
    ]
    for broken in broken_cases:
        with pytest.raises(CatalogError):
            parse_catalog(broken)
    wrong_default = _catalog_data()
    wrong_default["providers"]["p"]["defaults"]["quick"] = "absent"
    with pytest.raises(CatalogError):
        parse_catalog(wrong_default)
    wrong_api = _catalog_data()
    wrong_api["providers"]["p"]["api"] = "graphql"
    with pytest.raises(CatalogError):
        parse_catalog(wrong_api)


# --- fabrique ------------------------------------------------------------------------


def test_factory_selects_module() -> None:
    catalog = load_catalog()
    kinds = {
        pid: type(create_provider(ProviderConfig(catalog.provider(pid), "k"))).__name__
        for pid in ("mistral", "openai", "anthropic", "deepseek", "kimi")
    }
    assert kinds == {
        "mistral": "MistralProvider",
        "openai": "OpenAICompatProvider",
        "anthropic": "AnthropicProvider",
        "deepseek": "OpenAICompatProvider",
        "kimi": "OpenAICompatProvider",
    }
    custom = catalog.provider("custom")
    with pytest.raises(base.InvalidConfiguration):
        create_provider(ProviderConfig(custom))
    provider = create_provider(ProviderConfig(custom, None, "http://localhost:11434/v1"))
    assert provider.id == "custom"


def test_url_checks() -> None:
    assert validate_base_url("http://localhost:1234/v1") is None
    assert validate_base_url("ftp://serveur") is not None
    assert validate_base_url("localhost:1234") is not None
    assert validate_base_url("  ") is not None
    assert is_local_url("http://127.0.0.1:8080/v1") and is_local_url("http://[::1]:8080")
    assert not is_local_url("https://api.exemple.fr/v1")
    assert is_cleartext_remote("http://192.168.1.20:11434/v1")
    assert not is_cleartext_remote("http://localhost:11434/v1")
    assert not is_cleartext_remote("https://serveur.exemple.fr/v1")


@pytest.mark.parametrize(
    ("message", "error_type", "expected"),
    [
        ("Overloaded", "overloaded_error", base.ServerError),
        ("slow down", "rate_limit_error", base.RateLimited),
        ("bad key", "authentication_error", base.AuthError),
        ("prompt is too long", "invalid_request_error", base.ContextTooLong),
        ("something odd", "invalid_request_error", base.BadRequest),
        ("Upstream failure", None, base.ServerError),
        ("maximum context length exceeded", None, base.ContextTooLong),
    ],
)
def test_classify_stream_error(
    message: str, error_type: str | None, expected: type[base.ProviderError]
) -> None:
    assert type(base.classify_stream_error("P", message, error_type)) is expected


def test_error_type_extraction() -> None:
    assert (
        base.error_type_of({"type": "error", "error": {"type": "overloaded_error"}})
        == "overloaded_error"
    )
    assert base.error_type_of({"error": {"code": "insufficient_quota"}}) == "insufficient_quota"
    assert base.error_type_of({"type": "error"}) is None
    assert base.error_type_of("texte") is None
