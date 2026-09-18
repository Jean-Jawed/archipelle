"""Masquage des clés API, pour les logs et pour l'interface (CDC §9, §14).

Les clés connues sont enregistrées dans un registre global ; toute chaîne qui passe par
``mask_known_secrets`` voit ces clés remplacées par leur forme masquée. Des motifs
génériques couvrent en plus les formats de clés courants, au cas où une clé non encore
enregistrée apparaîtrait dans un message d'erreur.
"""

from __future__ import annotations

import re
import threading

_MIN_SECRET_LENGTH = 8
_lock = threading.Lock()
_known: set[str] = set()

# Motifs à un groupe : le groupe 1 (le préfixe) est conservé, la suite est masquée.
_PREFIXED_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{12,}"),
    re.compile(
        r"(?i)((?:api[_-]?key|x-api-key|authorization)[\"']?\s*[:=]\s*[\"']?)"
        r"[A-Za-z0-9._\-]{12,}"
    ),
)
# Motifs sans groupe : la correspondance entière est remplacée par sa forme masquée.
_WHOLE_PATTERNS = (re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}"),)


def mask_secret(secret: str) -> str:
    """Forme affichable d'une clé : ``sk-…a4f2``. Une clé courte est entièrement masquée."""
    secret = secret.strip()
    if len(secret) < 12:
        return "••••"
    dash = secret.find("-")
    prefix = secret[: dash + 1] if 0 < dash <= 6 else ""
    return f"{prefix}…{secret[-4:]}"


def register_secret(secret: str | None) -> None:
    if secret and len(secret.strip()) >= _MIN_SECRET_LENGTH:
        with _lock:
            _known.add(secret.strip())


def unregister_secret(secret: str | None) -> None:
    if secret:
        with _lock:
            _known.discard(secret.strip())


def mask_known_secrets(text: str) -> str:
    with _lock:
        secrets = sorted(_known, key=len, reverse=True)
    for secret in secrets:
        if secret in text:
            text = text.replace(secret, mask_secret(secret))
    for pattern in _WHOLE_PATTERNS:
        text = pattern.sub(lambda m: mask_secret(m.group(0)), text)
    for pattern in _PREFIXED_PATTERNS:
        text = pattern.sub(lambda m: m.group(1) + "••••", text)
    return text
