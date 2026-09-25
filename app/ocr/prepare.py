"""Подготовка изображения документа к распознаванию (3.2.7)."""

from __future__ import annotations

import io

import pymupdf as fitz  # PyMuPDF (модуль fitz устарел)
from PIL import Image, ImageOps

from app.config import get_settings

MAX_IMAGE_PIXELS = 50_000_000
IMAGE_TYPES = ("image/jpeg", "image/png")
PDF_TYPE = "application/pdf"

fitz.TOOLS.mupdf_display_errors(False)  # сообщения MuPDF о повреждённых файлах не нужны в логах


class DocumentUnreadable(Exception):
    """Файл не открывается: DOCUMENT_UNREADABLE."""


def _open_image(data: bytes) -> Image.Image:
    with Image.open(io.BytesIO(data)) as image:
        return ImageOps.exif_transpose(image).convert("RGB")


def _render_pdf(data: bytes, dpi: int) -> Image.Image:
    document = fitz.open(stream=data, filetype="pdf")
    try:
        if document.is_encrypted or document.needs_pass or document.page_count == 0:
            raise DocumentUnreadable("PDF зашифрован или не содержит страниц")
        page = document.load_page(0)  # проверяется только первая страница
        zoom = dpi / 72
        if page.rect.width * zoom * page.rect.height * zoom > MAX_IMAGE_PIXELS:
            # Та же защита от «бомб», что и для изображений: не рендерить гигантскую страницу.
            raise DocumentUnreadable("страница PDF слишком велика")
        pixmap = page.get_pixmap(dpi=dpi, alpha=False)
        return Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    finally:
        document.close()


def prepare_image(data: bytes, mime_type: str) -> Image.Image:
    """Изображение RGB с большей стороной не более OCR_MAX_IMAGE_SIDE; иначе DocumentUnreadable."""
    settings = get_settings()
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    try:
        if mime_type in IMAGE_TYPES:
            image = _open_image(data)
        elif mime_type == PDF_TYPE:
            image = _render_pdf(data, settings.PDF_RENDER_DPI)
        else:
            raise DocumentUnreadable(f"неподдерживаемый тип {mime_type}")
    except DocumentUnreadable:
        raise
    except Exception as exc:  # DecompressionBombError, повреждённые данные, ошибки MuPDF
        raise DocumentUnreadable(type(exc).__name__) from None

    longest = max(image.size)
    if longest > settings.OCR_MAX_IMAGE_SIDE:
        scale = settings.OCR_MAX_IMAGE_SIDE / longest
        size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
        image = image.resize(size, Image.Resampling.LANCZOS)
    return image
