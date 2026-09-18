"""Images (.png, .jpg, .jpeg, .tiff) : OCR de chaque image (CDC §3)."""

from __future__ import annotations

from PIL import Image, ImageSequence, UnidentifiedImageError

from archipelle.extraction import ocr
from archipelle.extraction.errors import os_errors
from archipelle.extraction.types import (
    DocumentNote,
    ErrorCode,
    ExtractionError,
    ExtractionMethod,
    NoteEntry,
    OcrConfig,
    WholeDocument,
)


def ocr_image(path: str, config: OcrConfig) -> WholeDocument:
    texts: list[str] = []
    with os_errors():
        try:
            with Image.open(path) as image:
                for frame in ImageSequence.Iterator(image):
                    prepared = frame if frame.mode in ("L", "RGB") else frame.convert("RGB")
                    text = ocr.image_to_text(prepared, config)
                    if text:
                        texts.append(text)
        except UnidentifiedImageError as exc:
            raise ExtractionError(ErrorCode.CORRUPT) from exc
        except Image.DecompressionBombError as exc:
            raise ExtractionError(ErrorCode.READ_ERROR, "image trop grande") from exc
    text = "\n\n".join(texts)
    notes = [] if text.strip() else [NoteEntry(DocumentNote.NO_TEXT)]
    return WholeDocument(text=text, method=ExtractionMethod.OCR, notes=notes)
