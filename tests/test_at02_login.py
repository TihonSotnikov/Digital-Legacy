"""AT-02: вход и защита маршрутов."""

from __future__ import annotations

import urllib.parse

from app import clock
from tests.helpers import DEFAULT_PASSWORD, error_codes, events, login, make_user, page_csrf, reload


def test_at02_wrong_password_then_success(client, db):
    user = make_user(db, email="owner@example.com")

    response = login(client, "owner@example.com", "wrong-password-1")
    assert response.status_code == 401
    assert error_codes(response.text) == ["INVALID_CREDENTIALS"]
    assert "Неверный email или пароль" in response.text
    assert [e.event_type for e in events(db)] == ["LOGIN_FAILED"]
    assert events(db)[0].user_id == user.id

    clock.advance(3600)
    response = login(client, "OWNER@example.com", DEFAULT_PASSWORD)
    assert response.status_code == 303
    assert response.headers["location"] == "/vault"
    assert reload(db, user).last_login_at == clock.now()
    assert [e.event_type for e in events(db)] == ["LOGIN_FAILED", "LOGIN_SUCCEEDED"]
    assert client.get("/vault").status_code == 200


def test_at02_unknown_email_is_invalid_credentials_without_event(client, db):
    response = login(client, "nobody@example.com", DEFAULT_PASSWORD)
    assert response.status_code == 401
    assert error_codes(response.text) == ["INVALID_CREDENTIALS"]
    assert events(db) == []


def test_at02_sixth_attempt_from_same_ip_rate_limited(client, db):
    make_user(db, email="owner@example.com")
    for _ in range(5):
        assert login(client, "owner@example.com", "wrong-password-1").status_code == 401
    response = login(client, "owner@example.com", DEFAULT_PASSWORD)
    assert response.status_code == 429
    assert error_codes(response.text) == ["RATE_LIMITED"]


def test_at02_owner_route_without_session_redirects_to_login(client):
    response = client.get("/vault")
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Fvault"

    response = client.get("/records/new?type=file")
    assert response.status_code == 303
    location = urllib.parse.urlsplit(response.headers["location"])
    assert location.path == "/login"
    assert urllib.parse.parse_qs(location.query) == {"next": ["/records/new?type=file"]}
    assert "Сессия истекла, войдите снова" in client.get(response.headers["location"]).text


def test_at02_next_parameter_accepted_only_for_local_paths(make_client, db):
    make_user(db, email="owner@example.com")

    response = login(make_client(), "owner@example.com", next_url="/heirs")
    assert response.headers["location"] == "/heirs"

    response = login(make_client(), "owner@example.com", next_url="//example.com")
    assert response.status_code == 303
    assert response.headers["location"] == "/vault"

    response = login(make_client(), "owner@example.com", next_url="https://example.com/")
    assert response.headers["location"] == "/vault"

    page = make_client().get("/login", params={"next": "//example.com"})
    assert 'name="next" value=""' in page.text


def test_at02_logout(client, db):
    make_user(db, email="owner@example.com")
    login(client, "owner@example.com")
    response = client.post("/logout", data={"csrf_token": page_csrf(client, "/vault")})
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert client.get("/vault").status_code == 303
