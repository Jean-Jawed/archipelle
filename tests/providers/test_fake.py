"""Faux fournisseur scripté (tests de la boucle et mode démo)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from archipelle.core.cancel import CancelToken
from archipelle.core.outcome import Cancelled
from archipelle.providers import base
from archipelle.providers.fake import FakeProvider, ScriptError
from archipelle.providers.retry import RetryPolicy, call_with_retry
from tests.providers.conftest import make_request

SCRIPT: dict[str, Any] = {
    "steps": [
        {"text": "Je cherche.", "reasoning": "réfléchir",
         "tool_calls": [{"id": "c1", "name": "search_fulltext",
                         "arguments": {"keywords": ["loyer"]}},
                        {"name": "read_file", "arguments": '{"path": '}]},
        {"error": "rate_limited", "retry_after": 0.01},
        {"text": "Réponse [[bail.pdf]]", "repeat": 2},
    ],
    "when_exhausted": "Fin du scénario.",
}  # fmt: skip


def test_script_is_replayed_in_order() -> None:
    fake = FakeProvider.from_script(SCRIPT)
    assert fake.id == "fake" and fake.remaining == 4
    first = fake.complete(make_request(model="demo"), CancelToken())
    assert first.text == "Je cherche." and first.stop_reason == "tool_calls"
    assert [c.call_id for c in first.tool_calls] == ["c1", "fake_2"]
    assert first.tool_calls[0].arguments == {"keywords": ["loyer"]}
    assert not first.tool_calls[1].arguments_valid  # paramètres illisibles simulés
    assert first.reasoning[0].payload == {"text": "réfléchir"}
    assert first.reasoning[0].model == "demo"
    with pytest.raises(base.RateLimited) as info:
        fake.complete(make_request(), CancelToken())
    assert info.value.retry_after == 0.01
    for _ in range(2):
        assert fake.complete(make_request(), CancelToken()).text == "Réponse [[bail.pdf]]"
    exhausted = fake.complete(make_request(), CancelToken())
    assert exhausted.text == "Fin du scénario." and exhausted.stop_reason == "end"
    assert len(fake.requests) == 5


def test_tool_choice_is_ignored_on_purpose() -> None:
    fake = FakeProvider.from_script(SCRIPT)
    answer = fake.complete(make_request(tool_choice="none"), CancelToken())
    assert answer.tool_calls  # simule un modèle qui ne respecte pas tool_choice
    assert fake.requests[0].tool_choice == "none"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("auth", base.AuthError),
        ("quota", base.QuotaExceeded),
        ("network", base.NetworkError),
        ("server", base.ServerError),
        ("timeout", base.IdleTimeout),
        ("model_not_found", base.ModelNotFound),
        ("tools_unsupported", base.ToolsUnsupported),
        ("context_too_long", base.ContextTooLong),
        ("bad_request", base.BadRequest),
    ],
)
def test_simulated_errors(error: str, expected: type[base.ProviderError]) -> None:
    fake = FakeProvider.from_script({"steps": [{"error": error}]})
    with pytest.raises(expected):
        fake.complete(make_request(), CancelToken())


def test_retry_integration() -> None:
    fake = FakeProvider.from_script(
        {"steps": [{"error": "network"}, {"error": "rate_limited"}, {"text": "enfin"}]}
    )
    policy = RetryPolicy(network_delays_s=(0.01,), rate_limit_delays_s=(0.01,))
    answer = call_with_retry(
        lambda: fake.complete(make_request(), CancelToken()), cancel=CancelToken(), policy=policy
    )
    assert answer.text == "enfin" and len(fake.requests) == 3


def test_stall_and_delay() -> None:
    stalled = FakeProvider.from_script(
        {"steps": [{"stall_s": 999}, {"stall_s": 0.05, "text": "ok"}]}
    )
    started = time.monotonic()
    with pytest.raises(base.IdleTimeout):
        stalled.complete(make_request(idle=0.2), CancelToken())
    assert 0.15 < time.monotonic() - started < 2
    assert stalled.complete(make_request(idle=0.2), CancelToken()).text == "ok"

    slow = FakeProvider.from_script({"steps": [{"delay_s": 30, "text": "trop tard"}]})
    token = CancelToken()
    threading.Timer(0.1, token.cancel).start()
    started = time.monotonic()
    with pytest.raises(Cancelled):
        slow.complete(make_request(), token)
    assert time.monotonic() - started < 1


def test_script_validation(tmp_path: Path) -> None:
    broken_cases: list[dict[str, Any]] = [
        {},
        {"steps": []},
        {"steps": ["texte"]},
        {"steps": [{"error": "inconnue"}]},
        {"steps": [{"tool_calls": [{"arguments": {}}]}]},
        {"steps": [{"tool_calls": [{"name": "x", "arguments": 3}]}]},
    ]
    for broken in broken_cases:
        with pytest.raises(ScriptError):
            FakeProvider.from_script(broken)
    script = tmp_path / "demo.json"
    script.write_text(json.dumps(SCRIPT), encoding="utf-8")
    assert FakeProvider.from_file(script).remaining == 4
    script.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ScriptError):
        FakeProvider.from_file(script)
    with pytest.raises(ScriptError):
        FakeProvider.from_file(tmp_path / "absent.json")


def test_demo_script_is_valid() -> None:
    from archipelle.core import resources

    fake = FakeProvider.from_file(resources.resource_path("demo", "demo_script.json"))
    assert fake.remaining >= 3
