"""EasyOCR на CPU внутри Worker (2.1.4, 3.2.7). Документы не покидают сервер."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.ocr.provider import OcrUnavailable

if TYPE_CHECKING:
    from PIL import Image

MODEL_DIR = "/models"


class EasyOcrProvider:
    def __init__(self, langs: list[str], model_dir: str = MODEL_DIR) -> None:
        import easyocr

        # Веса скачиваются при сборке образа; во время работы доступ в интернет не нужен.
        self._reader = easyocr.Reader(langs, gpu=False, model_storage_directory=model_dir, download_enabled=False)

    def recognize(self, image: "Image.Image") -> str:
        try:
            import numpy

            lines = self._reader.readtext(numpy.asarray(image), detail=0, paragraph=False)
            return "\n".join(lines)
        except Exception as exc:  # любое исключение — OcrUnavailable
            raise OcrUnavailable(type(exc).__name__) from exc
