"""OCR via Tesseract (CDC §3).

``detect()`` s'exécute dans le processus principal, au démarrage ; ``image_to_text()``
uniquement dans les sous-processus d'extraction.
"""

from __future__ import annotations

import logging
import math
import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from archipelle.core import resources
from archipelle.extraction.types import ErrorCode, ExtractionError, OcrConfig

if TYPE_CHECKING:
    from PIL.Image import Image

_log = logging.getLogger(__name__)
PREFERRED_LANGUAGES = ("fra", "eng")
# Emplacement d'installation par défaut de Tesseract sous Windows, souvent absent du PATH.
_KNOWN_LOCATIONS = (
    Path("C:/Program Files/Tesseract-OCR/tesseract.exe"),
    Path("C:/Program Files (x86)/Tesseract-OCR/tesseract.exe"),
    Path("/opt/homebrew/bin/tesseract"),
    Path("/usr/local/bin/tesseract"),
)


def _candidates() -> list[str]:
    found: list[str] = []
    bundled = resources.bundled_binary_dir()
    if bundled is not None:
        for name in ("tesseract", "tesseract.exe"):
            if (bundled / name).is_file():
                found.append(str(bundled / name))
    on_path = shutil.which("tesseract")
    if on_path:
        found.append(on_path)
    found.extend(str(p) for p in _KNOWN_LOCATIONS if p.is_file())
    return found


def _list_languages(command: str) -> set[str] | None:
    try:
        completed = subprocess.run(
            [command, "--list-langs"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    lines = (completed.stdout + completed.stderr).splitlines()
    return {line.strip() for line in lines if line.strip() and " " not in line.strip()}


def detect() -> OcrConfig | None:
    """Configuration OCR utilisable, ou ``None`` si Tesseract est absent."""
    for command in _candidates():
        languages = _list_languages(command)
        if languages is None:
            continue
        usable = [lang for lang in PREFERRED_LANGUAGES if lang in languages]
        if not usable:
            _log.warning("Tesseract trouvé (%s) sans langue fra ni eng", command)
            continue
        if "fra" not in usable:
            _log.warning("Pack de langue français de Tesseract absent")
        _log.info("Tesseract : %s (langues %s)", command, "+".join(usable))
        return OcrConfig(command=command, languages="+".join(usable))
    _log.warning("Tesseract introuvable : OCR indisponible")
    return None


def image_to_text(image: Image, config: OcrConfig) -> str:
    import pytesseract  # import local : réservé aux sous-processus

    os.environ.setdefault("OMP_THREAD_LIMIT", "1")
    engine: Any = pytesseract
    engine.pytesseract.tesseract_cmd = config.command
    try:
        text: Any = engine.image_to_string(
            image, lang=config.languages, timeout=math.ceil(config.page_timeout_s)
        )
    except pytesseract.TesseractNotFoundError as exc:
        raise ExtractionError(ErrorCode.OCR_UNAVAILABLE) from exc
    except pytesseract.TesseractError as exc:  # sous-classe de RuntimeError : à traiter avant
        raise ExtractionError(ErrorCode.READ_ERROR, str(exc)) from exc
    except RuntimeError as exc:  # pytesseract signale le dépassement de temps ainsi
        if "timeout" in str(exc).lower():
            raise ExtractionError(ErrorCode.OCR_TIMEOUT) from exc
        raise ExtractionError(ErrorCode.READ_ERROR, str(exc)) from exc
    return str(text).replace("\r\n", "\n").replace("\f", "").strip()
