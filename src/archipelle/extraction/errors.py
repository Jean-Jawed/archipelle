"""Traduction des erreurs système en ``ExtractionError``."""

from __future__ import annotations

import errno
from collections.abc import Generator
from contextlib import contextmanager

from archipelle.extraction.types import ErrorCode, ExtractionError

# Erreurs Windows des fichiers cloud non synchronisés (ERROR_CLOUD_FILE_*), plage 358-406.
_WINDOWS_CLOUD_ERRORS = range(358, 407)
# Erreurs observées sur des fichiers iCloud ou réseau non disponibles localement.
_OFFLINE_ERRNOS = {errno.EDEADLK, errno.ETIMEDOUT, errno.EHOSTDOWN, errno.ENETUNREACH}


def from_os_error(exc: OSError) -> ExtractionError:
    if isinstance(exc, FileNotFoundError):
        return ExtractionError(ErrorCode.NOT_FOUND)
    if isinstance(exc, PermissionError):
        return ExtractionError(ErrorCode.PERMISSION)
    winerror = getattr(exc, "winerror", None)
    if (isinstance(winerror, int) and winerror in _WINDOWS_CLOUD_ERRORS) or (
        exc.errno in _OFFLINE_ERRNOS
    ):
        return ExtractionError(ErrorCode.OFFLINE)
    return ExtractionError(ErrorCode.READ_ERROR, exc.strerror or type(exc).__name__)


@contextmanager
def os_errors() -> Generator[None]:
    try:
        yield
    except ExtractionError:
        raise
    except OSError as exc:
        raise from_os_error(exc) from exc


# Les fichiers Office chiffrés sont des conteneurs OLE et non des archives ZIP.
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ZIP_MAGIC = b"PK"


def check_office_container(path: str) -> None:
    """Refuse proprement un .docx/.xlsx/.pptx chiffré (ou un ancien format renommé)."""
    with os_errors(), open(path, "rb") as handle:  # noqa: PTH123
        head = handle.read(8)
    if head.startswith(OLE_MAGIC):
        raise ExtractionError(ErrorCode.PASSWORD)
    if not head.startswith(ZIP_MAGIC):
        raise ExtractionError(ErrorCode.CORRUPT)
