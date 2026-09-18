"""Interface commune des fournisseurs et erreurs normalisées (docs/ARCHITECTURE.md §6).

Chaque module traduit les erreurs de son SDK vers cette liste ; ``retry.py`` est le seul
endroit qui décide de retenter.
"""

from __future__ import annotations

import email.utils
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

from archipelle.core.cancel import CancelToken
from archipelle.core.outcome import AppError
from archipelle.core.toolspec import ToolSpec
from archipelle.providers.pivot import AssistantMessage, PivotItem

type ToolChoice = Literal["auto", "none"]

DEFAULT_IDLE_TIMEOUT_S = 120.0  # 🔧 CDC §7bis


@dataclass(frozen=True)
class ChatRequest:
    model: str
    system: str
    items: list[PivotItem]
    tools: list[ToolSpec]
    tool_choice: ToolChoice
    max_output_tokens: int
    idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT_S


class Provider(Protocol):
    @property
    def id(self) -> str: ...

    def complete(self, request: ChatRequest, cancel: CancelToken) -> AssistantMessage:
        """Appel en streaming, réponse assemblée. Lève ``Cancelled`` ou ``ProviderError``."""
        ...


# --- erreurs -------------------------------------------------------------------------


class ProviderError(AppError):
    """Erreur d'un fournisseur, avec un message destiné à l'utilisateur."""

    key_name = "unknown"
    retryable = False

    def __init__(self, provider: str, detail: str = "", **params: Any) -> None:
        super().__init__(f"errors.provider.{self.key_name}", provider=provider, **params)
        self.provider = provider
        self.detail = detail

    def __str__(self) -> str:
        return f"{type(self).__name__}({self.provider}): {self.detail}"


class AuthError(ProviderError):
    key_name = "auth"


class QuotaExceeded(ProviderError):
    """Crédit ou quota épuisé : une nouvelle tentative ne changerait rien."""

    key_name = "quota"


class RateLimited(ProviderError):
    key_name = "rate_limited"
    retryable = True

    def __init__(self, provider: str, detail: str = "", retry_after: float | None = None) -> None:
        super().__init__(provider, detail)
        self.retry_after = retry_after


class NetworkError(ProviderError):
    key_name = "network"
    retryable = True


class IdleTimeout(NetworkError):
    """Aucune donnée reçue pendant le délai d'inactivité (CDC §7bis)."""

    key_name = "timeout"

    def __init__(self, provider: str, seconds: float) -> None:
        super().__init__(provider, f"{seconds:.0f} s sans données", seconds=int(seconds))


class ServerError(NetworkError):
    """API indisponible ou surchargée (5xx) : traitée comme une panne réseau."""

    key_name = "server"


class ModelNotFound(ProviderError):
    key_name = "model_not_found"


class ToolsUnsupported(ProviderError):
    key_name = "tools_unsupported"


class ContextTooLong(ProviderError):
    key_name = "context_too_long"


class BadRequest(ProviderError):
    key_name = "bad_request"


class MissingKey(ProviderError):
    key_name = "missing_key"


class InvalidConfiguration(ProviderError):
    key_name = "invalid_configuration"

    def __init__(self, provider: str, detail: str = "") -> None:
        super().__init__(provider, detail, reason=detail)


# --- classification ------------------------------------------------------------------

_TOOLS_UNSUPPORTED = re.compile(
    r"(does not support|doesn't support|not support(ed)?|unsupported|no support)"
    r"[^.]{0,40}(tool|function)"
    r"|(tool|function)[ _-]?(use|call(ing|s)?|choice)?[^.]{0,30}"
    r"(not|isn't|is not) (supported|available|enabled)",
    re.IGNORECASE,
)
_CONTEXT_TOO_LONG = re.compile(
    r"context[ _-]?(length|window)|maximum context|too many tokens|prompt is too long"
    r"|input is too long|reduce the length|token limit|context_length_exceeded",
    re.IGNORECASE,
)
_MODEL_NOT_FOUND = re.compile(
    r"model[^.]{0,60}(not (be )?found|does not exist|not exist|unknown|invalid|not available"
    r"|no longer|deprecated|retired)|model_not_found|unknown model|invalid model",
    re.IGNORECASE,
)
_QUOTA = re.compile(
    r"insufficient[_ ]quota|insufficient[_ ](balance|credit)|credit balance|billing"
    r"|exceeded your current quota|payment required",
    re.IGNORECASE,
)


def parse_retry_after(headers: Mapping[str, str]) -> float | None:
    lowered = {k.lower(): v for k, v in headers.items()}
    raw_ms = lowered.get("retry-after-ms")
    if raw_ms:
        try:
            return max(0.0, float(raw_ms) / 1000)
        except ValueError:
            pass
    raw = lowered.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    return max(0.0, parsed.timestamp() - time.time())


def classify_status(
    provider: str, status: int, message: str, headers: Mapping[str, str] | None = None
) -> ProviderError:
    """Erreur normalisée pour une réponse HTTP en échec."""
    detail = f"HTTP {status}: {message}".strip()
    if _QUOTA.search(message) or status == 402:
        return QuotaExceeded(provider, detail)
    if status in (401, 403):
        return AuthError(provider, detail)
    if status == 429:
        return RateLimited(provider, detail, parse_retry_after(headers or {}))
    if status == 404:
        return ModelNotFound(provider, detail)
    if status == 413 or _CONTEXT_TOO_LONG.search(message):
        return ContextTooLong(provider, detail)
    if status in (400, 422):
        if _TOOLS_UNSUPPORTED.search(message):
            return ToolsUnsupported(provider, detail)
        if _MODEL_NOT_FOUND.search(message):
            return ModelNotFound(provider, detail)
        return BadRequest(provider, detail)
    if status == 408 or status >= 500:
        return ServerError(provider, detail)
    return BadRequest(provider, detail)


_STREAM_ERROR_STATUS = {
    "overloaded_error": 529,
    "api_error": 500,
    "server_error": 500,
    "rate_limit_error": 429,
    "authentication_error": 401,
    "permission_error": 403,
    "not_found_error": 404,
    "request_too_large": 413,
    "invalid_request_error": 400,
}


def classify_stream_error(
    provider: str, message: str, error_type: str | None = None
) -> ProviderError:
    """Erreur signalée au milieu d'un flux (la réponse HTTP avait commencé par 200).

    Sans type reconnu, l'incident est traité comme une panne du service, donc retenté.
    """
    status = _STREAM_ERROR_STATUS.get(error_type or "")
    if status is not None:
        return classify_status(provider, status, message)
    error = classify_status(provider, 400, message)
    return ServerError(provider, message) if type(error) is BadRequest else error


def error_type_of(body: object) -> str | None:
    """Type d'erreur d'un corps JSON (``{"error": {"type": …}}`` ou ``{"type": …}``)."""
    if not isinstance(body, Mapping):
        return None
    mapping: Mapping[str, Any] = body  # type: ignore[assignment]
    nested = mapping.get("error")
    if isinstance(nested, Mapping):
        inner: Mapping[str, Any] = nested  # type: ignore[assignment]
        value = inner.get("type") or inner.get("code")
        return str(value) if value else None
    value = mapping.get("type")
    return str(value) if value and value != "error" else None


def error_message_of(body: object, fallback: str = "") -> str:
    """Extrait le message d'un corps d'erreur JSON, quelle que soit sa forme."""
    if isinstance(body, str):
        return body or fallback
    if isinstance(body, Mapping):
        mapping: Mapping[str, Any] = body  # type: ignore[assignment]
        for key in ("error", "detail", "message"):
            value = mapping.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, Mapping | list):
                nested = error_message_of(cast(object, value))
                if nested:
                    return nested
        parts = [str(v) for k, v in mapping.items() if k in ("type", "code") and v]
        return " ".join(parts) or fallback
    if isinstance(body, list):
        items: list[Any] = body  # type: ignore[assignment]
        return " ".join(error_message_of(item) for item in items) or fallback
    return fallback
