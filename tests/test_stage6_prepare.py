"""Проверка документа (этап 6): подготовка изображения и fake OCR."""

from __future__ import annotations

import io
import struct
import zlib

import pymupdf as fitz
import pytest
from PIL import Image

from app.ocr.fake import FakeOcrProvider
from app.ocr.prepare import DocumentUnreadable, prepare_image
from app.ocr.provider import OcrUnavailable


def image_bytes(size, fmt="PNG", **save_kwargs) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 10, 10)).save(buffer, fmt, **save_kwargs)
    return buffer.getvalue()


def pdf_bytes(width_pt=595, height_pt=842, pages=1, **save_kwargs) -> bytes:
    document = fitz.open()
    for _ in range(pages):
        page = document.new_page(width=width_pt, height=height_pt)
        page.insert_text((72, 72), "Test", fontsize=12)
    data = document.tobytes(**save_kwargs)
    document.close()
    return data


def png_with_header(width: int, height: int) -> bytes:
    """PNG с корректной сигнатурой и заголовком IHDR заданного размера (данные пустые)."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"")) + chunk(b"IEND", b"")


EMPTY_PDF = (
    b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    b"2 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\n"
    b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
)


def test_png_and_jpeg_converted_to_rgb():
    image = prepare_image(image_bytes((300, 200)), "image/png")
    assert image.mode == "RGB" and image.size == (300, 200)
    image = prepare_image(image_bytes((300, 200), "JPEG"), "image/jpeg")
    assert image.mode == "RGB" and image.size == (300, 200)


def test_exif_orientation_applied():
    exif = Image.Exif()
    exif[0x0112] = 6  # поворот на 90°
    data = image_bytes((300, 200), "JPEG", exif=exif.tobytes())
    assert prepare_image(data, "image/jpeg").size == (200, 300)


def test_large_image_downscaled_proportionally(settings):
    image = prepare_image(image_bytes((4000, 1000)), "image/png")
    assert max(image.size) == settings.OCR_MAX_IMAGE_SIDE
    assert image.size == (2000, 500)


def test_pdf_first_page_rendered_at_configured_dpi(settings):
    image = prepare_image(pdf_bytes(width_pt=300, height_pt=400, pages=2), "application/pdf")
    assert image.mode == "RGB"
    expected = (round(300 * settings.PDF_RENDER_DPI / 72), round(400 * settings.PDF_RENDER_DPI / 72))
    assert abs(image.size[0] - expected[0]) <= 1 and abs(image.size[1] - expected[1]) <= 1


def test_a4_pdf_rendered_then_downscaled(settings):
    image = prepare_image(pdf_bytes(), "application/pdf")  # A4 при 200 dpi — 1654×2339
    assert max(image.size) == settings.OCR_MAX_IMAGE_SIDE


@pytest.mark.parametrize(
    "data, mime",
    [
        (b"\x89PNG\r\n\x1a\n" + b"\x00garbage" * 50, "image/png"),
        (b"\xff\xd8\xff" + b"broken jpeg" * 20, "image/jpeg"),
        (b"%PDF-1.7 broken", "application/pdf"),
        (EMPTY_PDF, "application/pdf"),
        (png_with_header(20000, 20000), "image/png"),  # DecompressionBombError
        (pdf_bytes(width_pt=14400, height_pt=14400), "application/pdf"),  # гигантская страница
    ],
    ids=["corrupted-png", "corrupted-jpeg", "broken-pdf", "pdf-without-pages", "decompression-bomb", "huge-pdf-page"],
)
def test_unreadable_documents(data, mime):
    with pytest.raises(DocumentUnreadable):
        prepare_image(data, mime)


def test_encrypted_pdf_unreadable():
    data = pdf_bytes(encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="user")
    with pytest.raises(DocumentUnreadable):
        prepare_image(data, "application/pdf")


def test_fake_ocr_returns_queued_results_in_order():
    fake = FakeOcrProvider()
    fake.push("первый", OcrUnavailable("сбой"), "второй")
    assert fake.recognize(None) == "первый"
    with pytest.raises(OcrUnavailable):
        fake.recognize(None)
    assert fake.recognize(None) == "второй"
    with pytest.raises(OcrUnavailable):
        fake.recognize(None)  # очередь пуста
    assert fake.calls == 4


def test_ocr_provider_selected_by_configuration(ocr):
    assert isinstance(ocr, FakeOcrProvider)
