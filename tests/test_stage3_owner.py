"""Владелец (этап 3): недоставленное E1, повторная отправка, cookie сессии, интерфейс сейфа."""

from __future__ import annotations

from sqlalchemy import select

from app.models import User
from tests.helpers import add_text_record, error_codes, page_csrf, register, signup, verification_path


def test_registration_completes_when_e1_not_sent(client, mail, db):
    mail.fail = True
    response = register(client)
    assert response.status_code == 303
    assert db.execute(select(User)).scalar_one().email == "owner@example.com"
    page = client.get("/vault").text
    assert error_codes(page) == ["EMAIL_SEND_FAILED"]
    assert "Не удалось отправить письмо. Попробуйте позже" in page
    assert 'action="/verify-email/resend"' in page and "Отправить письмо ещё раз" in page


def test_resend_verification_and_rate_limit(client, mail):
    mail.fail = True
    register(client)
    mail.fail = False
    for _ in range(3):
        response = client.post(
            "/verify-email/resend", data={"csrf_token": page_csrf(client, "/vault"), "next": "/heirs"}
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/heirs"
    assert len([m for m in mail.outbox if m.email_id == "E1"]) == 3
    assert "Письмо отправлено" in client.get("/vault").text
    response = client.post("/verify-email/resend", data={"csrf_token": page_csrf(client, "/vault")})
    assert response.status_code == 429
    assert error_codes(response.text) == ["RATE_LIMITED"]


def test_resend_link_verifies(client, mail, db):
    mail.fail = True
    register(client)
    mail.fail = False
    client.post("/verify-email/resend", data={"csrf_token": page_csrf(client, "/vault")})
    assert client.get(verification_path(mail, "owner@example.com")).status_code == 303
    assert db.execute(select(User)).scalar_one().email_verified_at is not None
    assert "Подтвердите email" not in client.get("/vault").text


def test_session_cookie_flags(client):
    response = register(client)
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "path=/" in cookie
    assert "secure" not in cookie  # COOKIE_SECURE=false в профиле test


def test_login_clears_previous_session_and_rotates_csrf(make_client, mail):
    client = signup(make_client(), mail)
    before = page_csrf(client, "/vault")
    client.post("/logout", data={"csrf_token": before})
    from tests.helpers import login

    assert login(client, "owner@example.com").status_code == 303
    assert page_csrf(client, "/vault") != before


def test_vault_empty_state_and_cards(client, mail):
    signup(client, mail)
    page = client.get("/vault").text
    assert "Ваш сейф пуст. Добавьте первую запись, чтобы начать формирование цифрового наследия." in page
    assert "Добавить первую запись" in page
    add_text_record(client, "Банк", "PIN 1234")
    page = client.get("/vault").text
    assert "Банк" in page and "PIN 1234" not in page  # содержимое в списке не показывается
    assert "Открыть" in page and "Изменить" in page and "Удалить" in page


def test_record_forms(client, mail):
    signup(client, mail)
    text_form = client.get("/records/new?type=text").text
    assert "Название хранится в открытом виде — не указывайте в нём секреты" in text_form
    assert "0 / 10 000" in text_form
    file_form = client.get("/records/new?type=file").text
    assert 'enctype="multipart/form-data"' in file_form and "Загрузить" in file_form
    assert client.get("/records/new?type=other").status_code == 404
