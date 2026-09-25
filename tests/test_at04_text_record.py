"""AT-04: текстовая запись."""

from __future__ import annotations

from sqlalchemy import select

from app import crypto
from app.models import Record
from tests.helpers import add_text_record, error_codes, events, page_csrf, signup

SECRET_TEXT = "Пароль от почты: СекретныйПароль-2026\nЛогин: anna.smirnova"


def contains_plaintext_fragment(blob: bytes, plaintext: bytes, window: int = 6) -> bool:
    return any(plaintext[i : i + window] in blob for i in range(len(plaintext) - window + 1))


def test_at04_text_record_encrypted_viewed_edited_deleted(client, mail, db):
    signup(client, mail)
    record_id = add_text_record(client, "Почта", SECRET_TEXT)

    record = db.get(Record, record_id)
    assert record.type == "text" and record.key_version == 1
    assert record.size_bytes == len(SECRET_TEXT.encode())
    assert not contains_plaintext_fragment(record.ciphertext, SECRET_TEXT.encode())
    assert [e.meta for e in events(db, "RECORD_CREATED")] == [{"record_id": str(record_id), "type": "text"}]

    response = client.get(f"/records/{record_id}")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert "СекретныйПароль-2026" in response.text and "anna.smirnova" in response.text

    edit_form = client.get(f"/records/{record_id}/edit")
    assert edit_form.headers["Cache-Control"] == "no-store"
    old_ciphertext = record.ciphertext
    response = client.post(
        f"/records/{record_id}/edit",
        data={"csrf_token": page_csrf(client, "/vault"), "title": "Почта (новая)", "content": "Новый пароль"},
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/records/{record_id}"
    db.expire_all()
    record = db.get(Record, record_id)
    assert record.ciphertext != old_ciphertext
    assert record.title == "Почта (новая)"
    assert crypto.decrypt(record.ciphertext, crypto.record_aad(record_id)) == "Новый пароль".encode()
    assert len(events(db, "RECORD_UPDATED")) == 1

    response = client.post(f"/records/{record_id}/delete", data={"csrf_token": page_csrf(client, "/vault")})
    assert response.status_code == 303
    assert response.headers["location"] == "/vault"
    db.expire_all()
    assert db.get(Record, record_id) is None
    assert [e.meta["record_id"] for e in events(db, "RECORD_DELETED")] == [str(record_id)]


def test_at04_text_longer_than_max_is_validation_error(client, mail, db, settings):
    signup(client, mail)
    too_long = "ж" * (settings.MAX_TEXT_CHARS + 1)
    data = {"csrf_token": page_csrf(client, "/vault"), "type": "text", "title": "Длинная", "content": too_long}
    response = client.post("/records", data=data)
    assert response.status_code == 400
    assert error_codes(response.text) == ["VALIDATION_ERROR"]
    assert db.execute(select(Record)).first() is None

    data["content"] = "ж" * settings.MAX_TEXT_CHARS
    assert client.post("/records", data=data).status_code == 303
