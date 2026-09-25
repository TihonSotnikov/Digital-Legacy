"""Завершение P0 (этап 8): CSRF на всех POST-маршрутах, no-store, заголовки безопасности."""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import select

from app import tokens
from app.models import Heir, HeirKey
from app.routes import SECURITY_HEADERS
from tests.helpers import (
    add_file_record,
    add_heir,
    add_text_record,
    error_codes,
    heir_login,
    make_request,
    signup,
)


@pytest.fixture
def world(make_client, mail, db):
    owner = signup(make_client(), mail)
    text_id = add_text_record(owner, "Текст", "секрет")
    file_id = add_file_record(owner, "Файл", "f.pdf", b"%PDF-1.4 file")
    heir_id, key = add_heir(owner, "Пётр", [text_id, file_id])
    heir_key = db.execute(select(HeirKey).where(HeirKey.heir_id == heir_id)).scalar_one()
    request = make_request(db, db.get(Heir, heir_id), heir_key, status="RELEASED")
    heir = make_client()
    heir_login(heir, key)
    anonymous = make_client()
    anonymous.get("/")
    return {
        "owner": owner,
        "heir": heir,
        "anonymous": anonymous,
        "ids": {
            "record_id": text_id,
            "file_id": file_id,
            "heir_id": heir_id,
            "request_id": request.id,
            "token": tokens.make_cancel_token(request.id),
        },
    }


POST_CASES = {
    "/register": "anonymous",
    "/login": "anonymous",
    "/logout": "owner",
    "/verify-email/resend": "owner",
    "/records": "owner",
    "/records/{record_id}/edit": "owner",
    "/records/{record_id}/delete": "owner",
    "/heirs": "owner",
    "/heirs/{heir_id}/edit": "owner",
    "/heirs/{heir_id}/regenerate-key": "owner",
    "/heirs/{heir_id}/delete": "owner",
    "/requests/{request_id}/cancel": "owner",
    "/account/delete": "owner",
    "/cancel/{token}": "anonymous",
    "/heir/login": "anonymous",
    "/heir/request": "heir",
    "/heir/logout": "heir",
}


def test_every_post_route_is_covered(app):
    post_routes = {r.path for r in app.routes if isinstance(r, APIRoute) and "POST" in r.methods}
    assert post_routes - set(POST_CASES) == set(), "новый POST-маршрут без проверки CSRF в тесте"


@pytest.mark.parametrize("path", sorted(POST_CASES))
def test_post_without_csrf_token_is_forbidden(world, path):
    client = world[POST_CASES[path]]
    ids = world["ids"]
    response = client.post(path.format(**ids), data={})
    assert response.status_code == 403, path
    assert error_codes(response.text) == ["CSRF_FAILED"]
    assert "Сессия устарела. Обновите страницу и повторите действие" in response.text


def test_secret_pages_are_not_cached(world):
    owner, heir, ids = world["owner"], world["heir"], world["ids"]
    responses = [
        owner.get(f"/records/{ids['record_id']}"),
        owner.get(f"/records/{ids['record_id']}/edit"),
        owner.get(f"/records/{ids['file_id']}/download"),
        owner.get(f"/heirs/{ids['heir_id']}/key"),
        heir.get("/heir/portal"),
        heir.get(f"/heir/files/{ids['file_id']}"),
        world["anonymous"].get(f"/cancel/{ids['token']}"),
    ]
    for response in responses:
        assert response.status_code == 200, response.url
        assert response.headers["Cache-Control"] == "no-store", response.url


def test_security_headers_on_all_kinds_of_responses(world):
    owner, ids = world["owner"], world["ids"]
    responses = [
        owner.get("/vault"),
        owner.get("/healthz"),
        owner.get("/static/app.js"),
        owner.get("/no-such-page"),
        owner.get(f"/records/{ids['file_id']}/download"),
        world["anonymous"].get("/vault"),  # 303
    ]
    for response in responses:
        for name, value in SECURITY_HEADERS.items():
            assert response.headers.get(name) == value, (response.url, name)
