"""AT-03: CSRF."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app import tokens
from app.models import Heir, HeirKey, Record
from tests.helpers import add_heir, error_codes, make_request, page_csrf, reload, session_data, signup


@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_at03_post_without_or_with_wrong_csrf_token_is_forbidden(make_client, mail, db, token):
    owner = signup(make_client(), mail)
    heir_id, key = add_heir(owner, "Иван")
    heir_key = db.execute(select(HeirKey).where(HeirKey.heir_id == heir_id)).scalar_one()
    request = make_request(db, db.get(Heir, heir_id), heir_key)
    cancel_token = tokens.make_cancel_token(request.id)

    heir_client = make_client()
    heir_client.get("/heir")  # у сессии есть действующий csrf, передаётся другой
    anonymous = make_client()
    anonymous.get(f"/cancel/{cancel_token}")

    cases = [
        (owner, "/records", {"type": "text", "title": "Заметка", "content": "секрет"}),
        (owner, "/heirs", {"name": "Пётр"}),
        (heir_client, "/heir/login", {"key": key}),
        (anonymous, f"/cancel/{cancel_token}", {}),
    ]
    for client, path, data in cases:
        if token is not None:
            data = {**data, "csrf_token": token}
        response = client.post(path, data=data)
        assert response.status_code == 403, path
        assert error_codes(response.text) == ["CSRF_FAILED"], path

    assert db.scalar(select(func.count()).select_from(Record)) == 0
    assert db.scalar(select(func.count()).select_from(Heir)) == 1
    assert reload(db, request).status == "PENDING_REVIEW"
    assert reload(db, heir_key).revoked_at is None
    assert "hkid" not in session_data(heir_client)  # сессия наследника не создана


def test_at03_valid_csrf_token_accepted(make_client, mail):
    owner = signup(make_client(), mail)
    data = {"csrf_token": page_csrf(owner, "/vault"), "type": "text", "title": "Заметка", "content": "x"}
    assert owner.post("/records", data=data).status_code == 303
