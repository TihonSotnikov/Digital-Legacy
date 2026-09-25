"""AT-31: изменение ФИО."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import ACTIVE_STATUSES, Heir, HeirKey, User
from tests.helpers import add_heir, error_codes, events, make_request, page_csrf, reload, signup


def owner_row(db) -> User:
    db.expire_all()
    return db.execute(select(User)).scalar_one()


def post_profile(client, last="Иванова", first="Мария", middle=""):
    data = {"csrf_token": page_csrf(client, "/account"), "last_name": last, "first_name": first, "middle_name": middle}
    return client.post("/account/profile", data=data)


def test_at31_profile_saved_without_active_requests(make_client, mail, db):
    owner = signup(make_client(), mail)
    page = owner.get("/account").text
    assert 'value="Смирнова"' in page and "ФИО нельзя изменить" not in page

    response = post_profile(owner, "Римская-Корсакова", "Мария", "")
    assert response.status_code == 303
    assert response.headers["location"] == "/account"
    user = owner_row(db)
    assert (user.last_name, user.first_name, user.middle_name) == ("Римская-Корсакова", "Мария", None)
    (event,) = events(db, "PROFILE_UPDATED")
    assert event.meta == {} and event.user_id == user.id
    assert "Изменения сохранены" in owner.get("/account").text


@pytest.mark.parametrize("status", sorted(ACTIVE_STATUSES))
def test_at31_profile_locked_with_active_request(make_client, mail, db, status):
    owner = signup(make_client(), mail)
    heir_id, _ = add_heir(owner, "Пётр")
    heir_key = db.execute(select(HeirKey).where(HeirKey.heir_id == heir_id)).scalar_one()
    make_request(db, db.get(Heir, heir_id), heir_key, status=status)

    page = owner.get("/account").text
    assert "ФИО нельзя изменить, пока есть активный запрос" in page and "<fieldset disabled>" in page

    response = post_profile(owner)
    assert response.status_code == 409
    assert error_codes(response.text) == ["PROFILE_LOCKED"]
    user = owner_row(db)
    assert (user.last_name, user.first_name) == ("Смирнова", "Анна")
    assert events(db, "PROFILE_UPDATED") == []


def test_at31_profile_allowed_after_finished_request(make_client, mail, db):
    owner = signup(make_client(), mail)
    heir_id, _ = add_heir(owner, "Пётр")
    heir_key = db.execute(select(HeirKey).where(HeirKey.heir_id == heir_id)).scalar_one()
    request = make_request(db, db.get(Heir, heir_id), heir_key, status="REJECTED")
    assert post_profile(owner).status_code == 303
    assert reload(db, request).status == "REJECTED"


def test_at31_profile_validation(make_client, mail, db):
    owner = signup(make_client(), mail)
    response = post_profile(owner, last="", first="Mary1")
    assert response.status_code == 400
    assert error_codes(response.text) == ["VALIDATION_ERROR", "VALIDATION_ERROR"]
    assert owner_row(db).last_name == "Смирнова"
