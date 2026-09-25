"""AT-11: подача запроса."""

from __future__ import annotations

import io
from datetime import timedelta

import pytest
from PIL import Image
from sqlalchemy import func, select

from app import clock, storage
from app.models import Heir, HeirKey, InheritanceRequest
from app.routes import MB
from tests.helpers import add_heir, error_codes, events, heir_login, make_request, page_csrf, signup


def png_bytes(size=(64, 48)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (255, 255, 255)).save(buffer, "PNG")
    return buffer.getvalue()


@pytest.fixture
def heir_setup(make_client, mail, db):
    owner = signup(make_client(), mail)
    heir_id, key = add_heir(owner, "Иван")
    heir = make_client()
    assert heir_login(heir, key).status_code == 303
    heir_key = db.execute(select(HeirKey).where(HeirKey.heir_id == heir_id)).scalar_one()
    return heir, db.get(Heir, heir_id), heir_key, key


def submit(client, filename: str, data: bytes, mime: str = "image/png", contact: str = ""):
    return client.post(
        "/heir/request",
        data={"csrf_token": page_csrf(client, "/heir/portal"), "contact_email": contact},
        files={"document": (filename, data, mime)},
    )


def test_at11_valid_png_creates_pending_request(heir_setup, db, files_dir):
    heir, heir_row, heir_key, _ = heir_setup
    document = png_bytes()
    response = submit(heir, "свидетельство.png", document, contact="Heir@Example.com")
    assert response.status_code == 303
    assert response.headers["location"] == "/heir/portal"

    request = db.execute(select(InheritanceRequest)).scalar_one()
    assert request.status == "PENDING_REVIEW"
    assert (request.heir_id, request.heir_key_id) == (heir_row.id, heir_key.id)
    assert (request.doc_mime_type, request.doc_stored, request.ocr_attempts) == ("image/png", True, 0)
    assert request.heir_contact_email == "heir@example.com"
    assert request.created_at == clock.now()

    path = storage.doc_path(request.id)
    assert path.parent == files_dir / "docs"
    on_disk = path.read_bytes()
    assert on_disk != document and document not in on_disk and document[:24] not in on_disk
    assert storage.load_document(request.id) == document

    (created,) = events(db, "REQUEST_CREATED")
    assert created.meta == {"heir_id": str(heir_row.id)} and created.request_id == request.id
    assert created.user_id == heir_row.user_id

    portal = heir.get("/heir/portal")
    assert "Документ на проверке. Обычно это занимает до 2 минут" in portal.text

    response = submit(heir, "second.png", png_bytes())
    assert response.status_code == 409
    assert error_codes(response.text) == ["ACTIVE_REQUEST_EXISTS"]
    assert db.scalar(select(func.count()).select_from(InheritanceRequest)) == 1


def test_at11_docx_unsupported_and_oversize_document(heir_setup, db, settings):
    heir, *_ = heir_setup
    response = submit(heir, "certificate.docx", b"PK\x03\x04" + b"0" * 200, "application/octet-stream")
    assert response.status_code == 415
    assert error_codes(response.text) == ["UNSUPPORTED_FILE_TYPE"]
    assert "JPG, PNG, PDF" in response.text

    oversize = b"\x89PNG\r\n\x1a\n" + b"\x00" * (settings.MAX_DOC_MB * MB)
    response = submit(heir, "big.png", oversize)
    assert response.status_code == 413
    assert error_codes(response.text) == ["FILE_TOO_LARGE"]
    assert db.scalar(select(func.count()).select_from(InheritanceRequest)) == 0


def test_at11_fourth_submission_after_three_rejections_limited_until_24_hours(heir_setup, db, make_client):
    heir, heir_row, heir_key, key = heir_setup
    for hours_ago in (3, 2, 1):
        make_request(db, heir_row, heir_key, status="REJECTED", created_at=clock.now() - timedelta(hours=hours_ago))

    portal = heir.get("/heir/portal").text
    assert error_codes(portal) == ["NAME_MISMATCH", "REQUEST_LIMIT_EXCEEDED"]
    assert 'action="/heir/request"' not in portal  # форма заменена сообщением

    response = submit(heir, "cert.png", png_bytes())
    assert response.status_code == 429
    assert error_codes(response.text) == ["REQUEST_LIMIT_EXCEEDED"]
    assert db.scalar(select(func.count()).select_from(InheritanceRequest)) == 3

    clock.advance(24 * 3600 - 3600)  # ровно 24 часа после последнего отклонения
    heir = make_client()
    assert heir_login(heir, key).status_code == 303
    response = submit(heir, "cert.png", png_bytes())
    assert response.status_code == 303
    statuses = db.execute(select(InheritanceRequest.status)).scalars().all()
    assert sorted(statuses) == ["PENDING_REVIEW", "REJECTED", "REJECTED", "REJECTED"]
