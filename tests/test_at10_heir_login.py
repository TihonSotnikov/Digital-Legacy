"""AT-10: вход наследника."""

from __future__ import annotations

import re

import pytest

from app import clock, crypto
from tests.helpers import add_heir, error_codes, heir_login, page_csrf, signup

ALERT_RE = re.compile(r'data-error-code="INVALID_KEY">([^<]+)<')


@pytest.fixture
def keys(make_client, mail):
    owner = signup(make_client(), mail)
    heir_id, old_key = add_heir(owner, "Иван")
    response = owner.post(f"/heirs/{heir_id}/regenerate-key", data={"csrf_token": page_csrf(owner, "/heirs")})
    page = owner.get(response.headers["location"]).text
    new_key = re.search(r'id="heir-key">([^<]+)<', page).group(1).replace(" ", "")
    return old_key, new_key


def test_at10_invalid_keys_rejected_with_same_message(make_client, keys):
    revoked, current = keys
    candidates = {
        "63 символа": current[:63],
        "не-hex символ": current[:63] + "g",
        "неизвестный": crypto.generate_heir_key(),
        "отозванный": revoked,
    }
    messages = set()
    for label, key in candidates.items():
        response = heir_login(make_client(), key)
        assert response.status_code == 401, label
        assert error_codes(response.text) == ["INVALID_KEY"], label
        messages.add(ALERT_RE.search(response.text).group(1))
    assert messages == {"Ключ не найден или недействителен"}


def test_at10_key_with_spaces_and_uppercase_accepted(make_client, keys):
    _, current = keys
    spaced = "  " + " ".join(current[i : i + 8] for i in range(0, 64, 8)).upper() + "\n\t"
    heir = make_client()
    response = heir_login(heir, spaced)
    assert response.status_code == 303
    assert response.headers["location"] == "/heir/portal"
    portal = heir.get("/heir/portal")
    assert portal.status_code == 200
    assert "Вы вошли как наследник Иван" in portal.text


def test_at10_sixth_attempt_within_15_minutes_rate_limited(make_client, keys):
    _, current = keys
    heir = make_client()
    for _ in range(5):
        assert heir_login(heir, crypto.generate_heir_key()).status_code == 401
    response = heir_login(heir, current)
    assert response.status_code == 429
    assert error_codes(response.text) == ["RATE_LIMITED"]


def test_at10_portal_redirects_31_minutes_after_login(make_client, keys):
    _, current = keys
    heir = make_client()
    assert heir_login(heir, current).status_code == 303
    clock.advance(29 * 60)
    assert heir.get("/heir/portal").status_code == 200
    clock.advance(2 * 60)  # 31 минута после входа
    response = heir.get("/heir/portal")
    assert response.status_code == 303
    assert response.headers["location"] == "/heir"
    assert "Сессия истекла, введите ключ снова" in heir.get("/heir").text


def test_at10_portal_without_session_redirects(make_client):
    response = make_client().get("/heir/portal")
    assert response.status_code == 303
    assert response.headers["location"] == "/heir"
