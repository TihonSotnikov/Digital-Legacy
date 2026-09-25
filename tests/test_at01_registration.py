"""AT-01: регистрация и подтверждение email."""

from __future__ import annotations

from sqlalchemy import select

from app import clock
from app.models import User
from tests.helpers import (
    DEFAULT_PASSWORD,
    error_codes,
    events,
    page_csrf,
    register,
    reload,
    verification_path,
)


def test_at01_registration_email_lowercase_and_e1(client, mail, db):
    response = register(client, email="Owner.Name@Example.COM")
    assert response.status_code == 303
    assert response.headers["location"] == "/vault"

    user = db.execute(select(User)).scalar_one()
    assert user.email == "owner.name@example.com"
    assert user.email_verified_at is None
    assert user.password_hash.startswith("$argon2id$")
    assert [e.event_type for e in events(db)] == ["USER_REGISTERED"]

    (e1,) = [m for m in mail.outbox if m.email_id == "E1"]
    assert e1.to == "owner.name@example.com"
    assert e1.subject == "Подтвердите email"
    assert "/verify-email/" in e1.body
    assert "48" in e1.body  # срок действия ссылки

    # Регистрация выполняет вход: сейф доступен.
    assert client.get("/vault").status_code == 200


def test_at01_heir_creation_forbidden_before_verification_and_link_verifies(client, mail, db):
    register(client)
    response = client.post("/heirs", data={"csrf_token": page_csrf(client, "/vault"), "name": "Иван"})
    assert response.status_code == 403
    assert "EMAIL_NOT_VERIFIED" in error_codes(response.text)

    user = db.execute(select(User)).scalar_one()
    response = client.get(verification_path(mail, user.email))
    assert response.status_code == 303
    assert response.headers["location"] == "/vault"
    user = reload(db, user)
    assert user.email_verified_at == clock.now()
    assert [e.event_type for e in events(db)] == ["USER_REGISTERED", "EMAIL_VERIFIED"]
    assert "Email подтверждён" in client.get("/vault").text

    response = client.post("/heirs", data={"csrf_token": page_csrf(client, "/vault"), "name": "Иван"})
    assert response.status_code == 303


def test_at01_verification_link_invalid_after_49_hours(client, mail, db):
    register(client)
    user = db.execute(select(User)).scalar_one()
    path = verification_path(mail, user.email)
    clock.advance(49 * 3600)
    response = client.get(path)
    assert response.status_code == 400
    assert "INVALID_TOKEN" in error_codes(response.text)
    assert reload(db, user).email_verified_at is None


def test_at01_verification_link_without_session_redirects_to_login(make_client, mail, db):
    register(make_client())
    path = verification_path(mail, "owner@example.com")
    response = make_client().get(path)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_at01_duplicate_email_short_password_and_missing_consent(make_client, db):
    assert register(make_client(), email="taken@example.com").status_code == 303

    other = make_client()
    response = register(other, email="TAKEN@example.com")
    assert response.status_code == 400
    assert error_codes(response.text) == ["EMAIL_TAKEN"]

    response = register(other, email="short@example.com", password="123456789")
    assert response.status_code == 400
    assert error_codes(response.text) == ["VALIDATION_ERROR"]
    assert 'value="short@example.com"' in response.text  # введённые данные сохраняются
    assert "123456789" not in response.text  # кроме паролей

    response = register(other, email="noconsent@example.com", consent=False)
    assert response.status_code == 400
    assert error_codes(response.text) == ["VALIDATION_ERROR"]

    assert db.execute(select(User.email)).scalars().all() == ["taken@example.com"]


def test_at01_registration_without_middle_name(client, db):
    response = register(client, email="nomiddle@example.com", middle_name=None)
    assert response.status_code == 303
    user = db.execute(select(User)).scalar_one()
    assert (user.last_name, user.first_name, user.middle_name) == ("Смирнова", "Анна", None)


def test_at01_name_parts_validated(client, db):
    response = register(client, last_name="Smith2", first_name="", password=DEFAULT_PASSWORD)
    assert response.status_code == 400
    assert error_codes(response.text) == ["VALIDATION_ERROR", "VALIDATION_ERROR"]
    assert db.execute(select(User)).first() is None
