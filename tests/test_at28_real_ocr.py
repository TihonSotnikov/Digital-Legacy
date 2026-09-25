"""AT-28: реальный OCR (маркер ocr): синтетические свидетельства и EasyOcrProvider."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app import clock
from app.config import get_settings
from app.ocr.prepare import prepare_image
from app.rules import OwnerNames, check
from scripts.gen_fixtures import make_certificate

pytestmark = pytest.mark.ocr

OWNER = OwnerNames("Смирнова", "Анна", "Сергеевна")
MIME = {"png": "image/png", "pdf": "application/pdf"}


@pytest.fixture(scope="module")
def easyocr_provider():
    from app.ocr.easyocr_provider import EasyOcrProvider

    return EasyOcrProvider(get_settings().ocr_langs)


def recognize_and_check(provider, tmp_path, last_name: str, fmt: str):
    today = clock.now().astimezone(get_settings().display_tz).date()
    path = make_certificate(
        tmp_path / f"certificate.{fmt}",
        last_name,
        "Анна",
        "Сергеевна",
        death_date=today - timedelta(days=3),
        issue_date=today - timedelta(days=1),
        fmt=fmt,
    )
    image = prepare_image(path.read_bytes(), MIME[fmt])
    text = provider.recognize(image)
    return check(text, OWNER, today, today - timedelta(days=10))


def test_at28_png_certificate_with_owner_name_accepted(easyocr_provider, tmp_path):
    result = recognize_and_check(easyocr_provider, tmp_path, "Смирнова", "png")
    assert result.accepted, (result.reasons, result.flags)


def test_at28_png_certificate_with_other_surname_name_mismatch(easyocr_provider, tmp_path):
    result = recognize_and_check(easyocr_provider, tmp_path, "Кузнецова", "png")
    assert not result.accepted
    assert result.reasons == ["NAME_MISMATCH"], result.flags


def test_at28_pdf_certificate_accepted(easyocr_provider, tmp_path):
    result = recognize_and_check(easyocr_provider, tmp_path, "Смирнова", "pdf")
    assert result.accepted, (result.reasons, result.flags)
