"""AT-07: изоляция Владельцев."""

from __future__ import annotations

from sqlalchemy import select

from app.models import Heir, HeirKey, Record, heir_record_access
from tests.helpers import (
    add_file_record,
    add_heir,
    add_text_record,
    error_codes,
    make_request,
    page_csrf,
    reload,
    signup,
)


def test_at07_owner_b_gets_404_for_owner_a_objects(make_client, mail, db, files_dir):
    owner_a = signup(make_client(), mail, email="a@example.com")
    text_a = add_text_record(owner_a, "Запись A", "секрет A", "a@example.com")
    file_a = add_file_record(owner_a, "Файл A", "a.png", b"\x89PNG" + b"a" * 100, "a@example.com")
    heir_a, _ = add_heir(owner_a, "Наследник A", [text_a])
    key_a = db.execute(select(HeirKey).where(HeirKey.heir_id == heir_a)).scalar_one()
    request_a = make_request(db, db.get(Heir, heir_a), key_a, status="WAITING_CANCELLATION")

    owner_b = signup(make_client(), mail, email="b@example.com")
    add_text_record(owner_b, "Запись B", "данные B", "b@example.com")
    heir_b, _ = add_heir(owner_b, "Наследник B")
    token = page_csrf(owner_b, "/vault")

    attempts = [
        ("get", f"/records/{text_a}", None),
        ("get", f"/records/{text_a}/edit", None),
        ("post", f"/records/{text_a}/edit", {"title": "взлом", "content": "взлом"}),
        ("post", f"/records/{text_a}/delete", {}),
        ("get", f"/records/{file_a}/download", None),
        ("post", f"/records/{file_a}/delete", {}),
        ("get", f"/heirs/{heir_a}/edit", None),
        ("post", f"/heirs/{heir_a}/edit", {"name": "взлом"}),
        ("get", f"/heirs/{heir_a}/key", None),
        ("post", f"/heirs/{heir_a}/regenerate-key", {}),
        ("post", f"/heirs/{heir_a}/delete", {}),
        ("post", f"/requests/{request_a.id}/cancel", {}),
        # назначение записи A наследнику B и новому наследнику B
        ("post", f"/heirs/{heir_b}/edit", {"name": "Наследник B", "record_ids": [str(text_a)]}),
        ("post", "/heirs", {"name": "Новый", "record_ids": [str(text_a)]}),
    ]
    for method, path, data in attempts:
        if method == "get":
            response = owner_b.get(path)
        else:
            response = owner_b.post(path, data={**data, "csrf_token": token})
        assert response.status_code == 404, (method, path)
        assert error_codes(response.text) == ["NOT_FOUND"], path

    # Чужие объекты не видны в списках Владельца B.
    assert "Запись A" not in owner_b.get("/vault").text
    assert "Наследник A" not in owner_b.get("/heirs").text
    assert "Наследник A" not in owner_b.get("/requests").text

    # Данные Владельца A не изменились.
    db.expire_all()
    assert db.get(Record, text_a).title == "Запись A"
    assert db.get(Record, file_a) is not None
    assert (files_dir / "records" / f"{file_a}.bin").exists()
    assert db.get(Heir, heir_a).name == "Наследник A"
    assert reload(db, key_a).revoked_at is None
    assert reload(db, request_a).status == "WAITING_CANCELLATION"
    assert db.execute(select(heir_record_access.c.record_id).where(heir_record_access.c.heir_id == heir_b)).first() is None
    assert db.execute(select(Heir).where(Heir.name == "Новый")).first() is None


def test_at07_malformed_ids_are_not_found(make_client, mail):
    owner = signup(make_client(), mail)
    for path in ("/records/not-a-uuid", "/heirs/123/edit", "/records/../vault/download"):
        response = owner.get(path)
        assert response.status_code == 404, path
