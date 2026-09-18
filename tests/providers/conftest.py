from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import httpx2
import pytest

from archipelle.core.toolspec import ToolSpec
from archipelle.providers.base import ChatRequest
from archipelle.providers.catalog import ProviderProfile, load_catalog
from archipelle.providers.pivot import PivotItem, UserMessage


def sse(*events: dict[str, Any] | str, named: bool = False, done: bool = False) -> bytes:
    """Corps SSE : ``named`` ajoute la ligne ``event:`` (format Anthropic)."""
    lines: list[str] = []
    for event in events:
        payload = event if isinstance(event, str) else json.dumps(event)
        if named and isinstance(event, dict):
            lines.append(f"event: {event['type']}")
        lines.append(f"data: {payload}")
        lines.append("")
    if done:
        lines += ["data: [DONE]", ""]
    return ("\n".join(lines) + "\n").encode()


@dataclass
class Recorder:
    """Transport HTTP simulé : enregistre les requêtes, rejoue des réponses."""

    responses: list[Callable[[], tuple[int, dict[str, str], Any]]] = field(
        default_factory=list[Callable[[], tuple[int, dict[str, str], Any]]]
    )
    requests: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    headers: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    urls: list[str] = field(default_factory=list[str])

    def stream(self, body: bytes) -> Recorder:
        self.responses.append(lambda: (200, {"content-type": "text/event-stream"}, body))
        return self

    def slow_stream(self, first: bytes, pause_s: float) -> Recorder:
        def chunks() -> Iterator[bytes]:
            yield first
            time.sleep(pause_s)
            yield b"data: [DONE]\n\n"

        self.responses.append(lambda: (200, {"content-type": "text/event-stream"}, chunks()))
        return self

    def error(self, status: int, body: object, headers: dict[str, str] | None = None) -> Recorder:
        content = json.dumps(body).encode() if not isinstance(body, bytes) else body
        all_headers = {"content-type": "application/json", **(headers or {})}
        self.responses.append(lambda: (status, all_headers, content))
        return self

    def _record(self, method: str, url: str, headers: dict[str, str], content: bytes) -> None:
        del method
        self.urls.append(url)
        self.headers.append({k.lower(): v for k, v in headers.items()})
        self.requests.append(json.loads(content) if content else {})

    def httpx2_client(self) -> httpx2.Client:
        def handler(request: httpx2.Request) -> httpx2.Response:
            self._record(request.method, str(request.url), dict(request.headers), request.read())
            status, headers, content = self.responses.pop(0)()
            if isinstance(content, Exception):
                raise content
            return httpx2.Response(status, headers=headers, content=content)

        return httpx2.Client(transport=httpx2.MockTransport(handler))

    def httpx_client(self) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            self._record(request.method, str(request.url), dict(request.headers), request.read())
            status, headers, content = self.responses.pop(0)()
            if isinstance(content, Exception):
                raise content
            return httpx.Response(status, headers=headers, content=content)

        return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


TOOLS = [
    ToolSpec(
        "read_file",
        "Lit un fichier.",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    )
]


def make_request(
    items: list[PivotItem] | None = None,
    *,
    model: str = "m",
    tool_choice: str = "auto",
    idle: float = 5.0,
    tools: list[ToolSpec] | None = None,
) -> ChatRequest:
    return ChatRequest(
        model=model,
        system="Tu es Archipelle.",
        items=items if items is not None else [UserMessage("Bonjour")],
        tools=TOOLS if tools is None else tools,
        tool_choice=tool_choice,  # type: ignore[arg-type]
        max_output_tokens=1000,
        idle_timeout_s=idle,
    )


def profile(provider_id: str) -> ProviderProfile:
    return load_catalog().provider(provider_id)
