"""AT-08: выдача ключа."""

from __future__ import annotations

import hashlib
import re

from sqlalchemy import select, text

from app.models import HeirKey
from tests.helpers import events, key_from_page, page_csrf, session_data, signup


def all_database_text(db) -> str:
    """Все строки всех таблиц в текстовом представлении PostgreSQL."""
    tables = db.execute(
        text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    ).scalars().all()
    dump = []
    for table in tables:
        dump += db.execute(text(f'SELECT t::text FROM "{table}" t')).scalars().all()
    return "\n".join(dump)


def test_at08_key_shown_once_hashed_and_absent_from_database(client, mail, db):
    signup(client, mail)
    response = client.post("/heirs", data={"csrf_token": page_csrf(client, "/heirs"), "name": "Иван Петров"})
    assert response.status_code == 303
    location = response.headers["location"]
    assert re.fullmatch(r"/heirs/[0-9a-f-]{36}/key", location)
    assert "key" in session_data(client).get("flash_key", {})  # только транзитно, до показа

    page = client.get(location)
    assert page.status_code == 200
    assert page.headers["Cache-Control"] == "no-store"
    assert "Ключ для наследника Иван Петров" in page.text
    shown = key_from_page(page.text)
    assert re.fullmatch(r"([0-9a-f]{8} ){7}[0-9a-f]{8}", shown)  # группы по 8 символов
    key = re.sub(r"\s+", "", shown)
    assert re.fullmatch(r"[0-9a-f]{64}", key)
    assert 'data-copy-target="#heir-key" data-copy-strip' in page.text
    assert "Система показывает его только один раз" in page.text
    assert "flash_key" not in session_data(client)  # удалён из сессии при показе

    again = client.get(location)
    assert again.status_code == 200
    assert again.headers["Cache-Control"] == "no-store"
    assert key_from_page(again.text) is None
    assert key not in again.text and shown not in again.text
    assert "Ключ уже был показан. Если вы его не сохранили, перевыпустите ключ" in again.text

    heir_key = db.execute(select(HeirKey)).scalar_one()
    assert heir_key.key_hash == hashlib.sha256(key.encode("ascii")).hexdigest()
    assert heir_key.revoked_at is None

    dump = all_database_text(db)
    assert key not in dump and key.upper() not in dump and shown not in dump
    assert heir_key.key_hash in dump
    assert [e.event_type for e in events(db) if e.event_type.startswith("HEIR")] == ["HEIR_CREATED", "HEIR_KEY_ISSUED"]
    issued = events(db, "HEIR_KEY_ISSUED")[0]
    assert issued.meta == {"heir_id": str(heir_key.heir_id), "heir_key_id": str(heir_key.id)}
