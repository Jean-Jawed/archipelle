"""Création du module fournisseur adapté à un profil du catalogue."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from archipelle.providers.anthropic import AnthropicProvider
from archipelle.providers.base import InvalidConfiguration, Provider
from archipelle.providers.catalog import ProviderProfile
from archipelle.providers.mistral import MistralProvider
from archipelle.providers.openai_compat import OpenAICompatProvider

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


@dataclass(frozen=True)
class ProviderConfig:
    profile: ProviderProfile
    api_key: str | None = None
    base_url: str | None = None  # adresse saisie (fournisseur personnalisé)


def create_provider(config: ProviderConfig, *, http_client: Any = None) -> Provider:
    """``http_client`` : client HTTP injecté (tests) — ``httpx2.Client`` pour OpenAI et
    Anthropic, ``httpx.Client`` pour Mistral."""
    profile = config.profile
    base_url = config.base_url or profile.base_url
    if profile.is_custom:
        problem = validate_base_url(base_url)
        if problem is not None:
            raise InvalidConfiguration(profile.label, problem)
    match profile.api:
        case "openai_compat":
            return OpenAICompatProvider(
                profile, config.api_key, base_url=base_url, http_client=http_client
            )
        case "mistral":
            return MistralProvider(
                profile, config.api_key, base_url=base_url, http_client=http_client
            )
        case "anthropic":
            return AnthropicProvider(
                profile, config.api_key, base_url=base_url, http_client=http_client
            )


def validate_base_url(url: str | None) -> str | None:
    """Motif du refus d'une adresse saisie, ou ``None`` si elle est acceptable."""
    if not url or not url.strip():
        return "adresse manquante"
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "adresse invalide"
    return None


def is_local_url(url: str) -> bool:
    """Vrai si l'adresse désigne la machine elle-même (aucun transfert, CDC §13)."""
    host = (urlsplit(url.strip()).hostname or "").lower()
    return host in _LOCAL_HOSTS or host.startswith("127.")


def is_cleartext_remote(url: str) -> bool:
    """Adresse en ``http://`` qui ne désigne pas la machine : avertissement (CDC §13)."""
    return urlsplit(url.strip()).scheme == "http" and not is_local_url(url)
