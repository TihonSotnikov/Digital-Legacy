"""Интерфейс распознавания OcrProvider и выбор реализации по OCR_PROVIDER (3.2.7)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from app.config import get_settings

if TYPE_CHECKING:
    from PIL import Image


class OcrUnavailable(Exception):
    """Техническая ошибка распознавания: попытка повторяется до OCR_MAX_ATTEMPTS."""


class OcrProvider(Protocol):
    def recognize(self, image: "Image.Image") -> str: ...


_provider: OcrProvider | None = None


def get_provider() -> OcrProvider:
    """Модель загружается один раз; easyocr импортируется лениво, только при OCR_PROVIDER=easyocr."""
    global _provider
    if _provider is None:
        settings = get_settings()
        if settings.OCR_PROVIDER == "easyocr":
            from app.ocr.easyocr_provider import EasyOcrProvider

            _provider = EasyOcrProvider(settings.ocr_langs)
        else:
            from app.ocr.fake import FakeOcrProvider

            _provider = FakeOcrProvider()
    return _provider


def reset_provider() -> None:
    global _provider
    _provider = None
