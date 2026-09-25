"""Портал наследника (этап 5): состояния, валидация, выход, изоляция портала."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app import clock, rules
from app.models import Heir, HeirKey, InheritanceRequest
from app.routes import format_datetime
from tests.helpers import (
    add_heir,
    add_text_record,
    error_codes,
    heir_login,
    make_request,
    page_csrf,
    session_data,
    signup,
)
from tests.test_at11_request_submission import png_bytes, submit


@pytest.fixture
def portal_setup(make_client, mail, db):
    owner = signup(make_client(), mail)
    record_id = add_text_record(owner, "Банк", "PIN 4321")
    heir_id, key = add_heir(owner, "Иван", [record_id])
    heir = make_client()
    heir_login(heir, key)
    heir_key = db.execute(select(HeirKey).where(HeirKey.heir_id == heir_id)).scalar_one()
    return owner, heir, db.get(Heir, heir_id), heir_key


def test_portal_initial_state(portal_setup):
    _, heir, _, _ = portal_setup
    page = heir.get("/heir/portal")
    assert page.headers["Cache-Control"] == "no-store"
    text = page.text
    assert "Загрузите свидетельство о смерти владельца данных" in text
    assert "Email для уведомлений (необязательно)" in text
    assert rules.PHOTO_ADVICE in text and "Отправить на проверку" in text
    assert "JPG, PNG, PDF до 10 МБ" in text
    assert "PIN 4321" not in text and "Банк" not in text  # данные до выдачи не показываются


@pytest.mark.parametrize("status", ["PENDING_REVIEW", "DOCUMENT_ACCEPTED"])
def test_portal_pending_state(portal_setup, db, status):
    _, heir, heir_row, heir_key = portal_setup
    make_request(db, heir_row, heir_key, status=status)
    text = heir.get("/heir/portal").text
    assert "Документ на проверке. Обычно это занимает до 2 минут" in text and "Обновить статус" in text
    assert 'action="/heir/request"' not in text


def test_portal_waiting_state_shows_date(portal_setup, db):
    _, heir, heir_row, heir_key = portal_setup
    until = clock.now() + timedelta(days=14)
    make_request(db, heir_row, heir_key, status="WAITING_CANCELLATION", waiting_until=until)
    text = heir.get("/heir/portal").text
    expected = (
        "Документ принят. Владелец данных уведомлён. Если запрос не будет отменён, "
        f"доступ откроется не ранее {format_datetime(until)}"
    )
    assert expected in text and "MSK" in format_datetime(until)


def test_portal_rejected_state_shows_reasons_advice_and_form(portal_setup, db):
    _, heir, heir_row, heir_key = portal_setup
    result = {"accepted": False, "reasons": ["TITLE_NOT_FOUND", "NAME_MISMATCH"]}
    make_request(db, heir_row, heir_key, status="REJECTED", check_result=result)
    text = heir.get("/heir/portal").text
    assert "Документ не прошёл проверку:" in text
    assert rules.REASON_MESSAGES["TITLE_NOT_FOUND"] in text
    assert rules.REASON_MESSAGES["NAME_MISMATCH"] in text
    assert text.index(rules.REASON_MESSAGES["TITLE_NOT_FOUND"]) < text.index(rules.REASON_MESSAGES["NAME_MISMATCH"])
    assert rules.PHOTO_ADVICE in text and "Загрузить другой документ" in text


def test_ocr_unavailable_rejections_not_counted(portal_setup, db):
    _, heir, heir_row, heir_key = portal_setup
    result = {"accepted": False, "reasons": ["OCR_UNAVAILABLE"], "attempts": 3}
    for _ in range(3):
        make_request(db, heir_row, heir_key, status="REJECTED", check_result=result)
    assert "Загрузить другой документ" in heir.get("/heir/portal").text
    assert submit(heir, "cert.png", png_bytes()).status_code == 303


def test_invalid_contact_email_rerenders_form(portal_setup, db):
    _, heir, _, _ = portal_setup
    response = submit(heir, "cert.png", png_bytes(), contact="не email")
    assert response.status_code == 400
    assert error_codes(response.text) == ["VALIDATION_ERROR"]
    assert 'value="не email"' in response.text
    assert db.execute(select(InheritanceRequest)).first() is None


def test_submit_without_document(portal_setup):
    _, heir, _, _ = portal_setup
    response = heir.post("/heir/request", data={"csrf_token": page_csrf(heir, "/heir/portal")})
    assert response.status_code == 400
    assert error_codes(response.text) == ["VALIDATION_ERROR"]


def test_already_released_key_cannot_submit(portal_setup, db):
    _, heir, heir_row, heir_key = portal_setup
    make_request(db, heir_row, heir_key, status="RELEASED")
    response = submit(heir, "cert.png", png_bytes())
    assert response.status_code == 409
    assert error_codes(response.text) == ["ALREADY_RELEASED"]


def test_active_request_of_heir_blocks_new_key_submission(make_client, mail, db):
    """Активный запрос наследника (по любому ключу) блокирует подачу — ACTIVE_REQUEST_EXISTS."""
    owner = signup(make_client(), mail)
    heir_id, key = add_heir(owner, "Иван")
    heir = make_client()
    heir_login(heir, key)
    assert submit(heir, "cert.png", png_bytes()).status_code == 303
    response = submit(heir, "cert.png", png_bytes())
    assert error_codes(response.text) == ["ACTIVE_REQUEST_EXISTS"]


def test_heir_logout(portal_setup):
    _, heir, _, _ = portal_setup
    response = heir.post("/heir/logout", data={"csrf_token": page_csrf(heir, "/heir/portal")})
    assert response.status_code == 303 and response.headers["location"] == "/heir"
    assert "hkid" not in session_data(heir)
    assert heir.get("/heir/portal").status_code == 303


def test_revoked_key_ends_heir_session(portal_setup):
    owner, heir, heir_row, _ = portal_setup
    owner.post(f"/heirs/{heir_row.id}/regenerate-key", data={"csrf_token": page_csrf(owner, "/heirs")})
    response = heir.get("/heir/portal")
    assert response.status_code == 303 and response.headers["location"] == "/heir"
    assert "Сессия истекла, введите ключ снова" in heir.get("/heir").text
