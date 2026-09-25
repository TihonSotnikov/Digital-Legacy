"""AT-25: удаление аккаунта."""

from __future__ import annotations

from sqlalchemy import func, select

from app import storage
from app.models import AuditEvent, Heir, HeirKey, InheritanceRequest, Record, User, heir_record_access
from tests.helpers import (
    DEFAULT_PASSWORD,
    add_file_record,
    add_heir,
    add_text_record,
    error_codes,
    heir_login,
    only_request,
    page_csrf,
    session_data,
    signup,
    submit_document,
)


def count(db, table, *where) -> int:
    return db.scalar(select(func.count()).select_from(table).where(*where))


def test_at25_account_deletion(make_client, mail, db, files_dir):
    owner = signup(make_client(), mail)
    text_id = add_text_record(owner, "Банк", "PIN 1111", "owner@example.com")
    file_id = add_file_record(owner, "Скан", "scan.png", b"\x89PNG" + b"x" * 500, "owner@example.com")
    heir_id, key = add_heir(owner, "Пётр", [text_id, file_id])
    heir = make_client()
    heir_login(heir, key)
    submit_document(heir)
    request_id = only_request(db).id
    assert storage.record_path(file_id).exists() and storage.doc_path(request_id).exists()

    other = signup(make_client(), mail, email="other@example.com")
    other_file = add_file_record(other, "Чужой файл", "other.pdf", b"%PDF other", "other@example.com")

    page = owner.get("/account").text
    assert "owner@example.com" in page
    assert "Все записи, файлы и наследники будут удалены без возможности восстановления. Ключи наследников перестанут работать" in page

    response = owner.post("/account/delete", data={"csrf_token": page_csrf(owner, "/account"), "password": "wrong-password-1"})
    assert response.status_code == 401
    assert error_codes(response.text) == ["INVALID_CREDENTIALS"]
    assert "Неверный пароль" in response.text
    assert db.scalar(select(func.count()).select_from(Record)) == 3

    response = owner.post("/account/delete", data={"csrf_token": page_csrf(owner, "/account"), "password": DEFAULT_PASSWORD})
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert "Аккаунт удалён" in owner.get("/").text

    db.expire_all()
    user_ids = select(User.id).where(User.email == "owner@example.com")
    assert db.execute(user_ids).first() is None
    assert count(db, Record, Record.id.in_([text_id, file_id])) == 0
    assert count(db, Heir, Heir.id == heir_id) == 0
    assert count(db, HeirKey, HeirKey.heir_id == heir_id) == 0
    assert count(db, heir_record_access, heir_record_access.c.heir_id == heir_id) == 0
    assert count(db, InheritanceRequest, InheritanceRequest.id == request_id) == 0
    other_id = db.execute(select(User.id).where(User.email == "other@example.com")).scalar_one()
    assert count(db, AuditEvent, AuditEvent.user_id != other_id) == 0
    assert count(db, User) == 1 and count(db, Record) == 1  # данные другого Владельца не затронуты

    assert not storage.record_path(file_id).exists()
    assert not storage.doc_path(request_id).exists()
    assert storage.record_path(other_file).exists()

    assert "uid" not in session_data(owner)
    assert owner.get("/vault").status_code == 303

    response = heir_login(make_client(), key)
    assert response.status_code == 401
    assert error_codes(response.text) == ["INVALID_KEY"]
    assert heir.get("/heir/portal").status_code == 303  # сессия наследника тоже перестала работать
