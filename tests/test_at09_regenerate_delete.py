"""AT-09: перевыпуск и удаление."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import ACTIVE_STATUSES, Heir, HeirKey
from tests.helpers import (
    add_heir,
    error_codes,
    events,
    heir_login,
    key_from_page,
    make_request,
    page_csrf,
    reload,
    session_data,
    signup,
)


def test_at09_regenerate_revokes_old_key_and_new_key_works(make_client, mail, db):
    owner = signup(make_client(), mail)
    heir_id, old_key = add_heir(owner, "Иван")
    old_heir_key = db.execute(select(HeirKey).where(HeirKey.heir_id == heir_id)).scalar_one()

    response = owner.post(f"/heirs/{heir_id}/regenerate-key", data={"csrf_token": page_csrf(owner, "/heirs")})
    assert response.status_code == 303
    assert response.headers["location"] == f"/heirs/{heir_id}/key"
    key_page = owner.get(response.headers["location"]).text
    assert "Ключ перевыпущен" in key_page  # flash показывается на странице результата
    new_key = key_from_page(key_page).replace(" ", "")
    assert new_key != old_key

    response = heir_login(make_client(), old_key)
    assert response.status_code == 401
    assert error_codes(response.text) == ["INVALID_KEY"]

    heir = make_client()
    response = heir_login(heir, new_key)
    assert response.status_code == 303
    assert response.headers["location"] == "/heir/portal"
    assert session_data(heir)["hkid"] != str(old_heir_key.id)

    assert reload(db, old_heir_key).revoked_at is not None
    (revoked,) = events(db, "HEIR_KEY_REVOKED")
    assert revoked.meta == {"heir_id": str(heir_id), "heir_key_id": str(old_heir_key.id), "reason": "regenerate"}
    assert len(events(db, "HEIR_KEY_ISSUED")) == 2
    active = db.execute(select(HeirKey).where(HeirKey.heir_id == heir_id, HeirKey.revoked_at.is_(None))).scalars().all()
    assert len(active) == 1


@pytest.mark.parametrize("status", sorted(ACTIVE_STATUSES))
def test_at09_regenerate_or_delete_with_active_request_conflicts(make_client, mail, db, status):
    owner = signup(make_client(), mail)
    heir_id, key = add_heir(owner, "Иван")
    heir_key = db.execute(select(HeirKey).where(HeirKey.heir_id == heir_id)).scalar_one()
    make_request(db, db.get(Heir, heir_id), heir_key, status=status)

    for action in ("regenerate-key", "delete"):
        response = owner.post(f"/heirs/{heir_id}/{action}", data={"csrf_token": page_csrf(owner, "/heirs")})
        assert response.status_code == 409, action
        assert error_codes(response.text) == ["HEIR_HAS_ACTIVE_REQUEST"], action

    db.expire_all()
    assert db.get(Heir, heir_id) is not None
    assert reload(db, heir_key).revoked_at is None
    assert heir_login(make_client(), key).status_code == 303
    assert events(db, "HEIR_KEY_REVOKED") == [] and events(db, "HEIR_DELETED") == []


@pytest.mark.parametrize("status", ["RELEASED", "REJECTED"])
def test_at09_regenerate_and_delete_allowed_after_finished_request(make_client, mail, db, status):
    owner = signup(make_client(), mail)
    heir_id, key = add_heir(owner, "Иван")
    heir_key = db.execute(select(HeirKey).where(HeirKey.heir_id == heir_id)).scalar_one()
    make_request(db, db.get(Heir, heir_id), heir_key, status=status)

    response = owner.post(f"/heirs/{heir_id}/regenerate-key", data={"csrf_token": page_csrf(owner, "/heirs")})
    assert response.status_code == 303
    assert heir_login(make_client(), key).status_code == 401

    response = owner.post(f"/heirs/{heir_id}/delete", data={"csrf_token": page_csrf(owner, "/heirs")})
    assert response.status_code == 303
    db.expire_all()
    assert db.get(Heir, heir_id) is None
