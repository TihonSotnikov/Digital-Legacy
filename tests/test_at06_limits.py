"""AT-06: лимиты."""

from __future__ import annotations

from sqlalchemy import func, select

from app.models import Heir, Record
from tests.helpers import add_heir, error_codes, page_csrf, post_file, signup


def test_at06_records_heirs_and_storage_limits(client, mail, db, settings, monkeypatch):
    monkeypatch.setattr(settings, "MAX_RECORDS", 2)
    monkeypatch.setattr(settings, "MAX_HEIRS", 1)
    monkeypatch.setattr(settings, "MAX_STORAGE_MB", 1)
    signup(client, mail)

    part = b"%PDF" + b"\x00" * (600 * 1024)
    assert post_file(client, "Файл 1", "one.pdf", part).status_code == 303

    # Превышение общего объёма: 0,6 МБ + 0,6 МБ > 1 МБ.
    response = post_file(client, "Файл 2", "two.pdf", part)
    assert response.status_code == 409
    assert error_codes(response.text) == ["LIMIT_EXCEEDED"]
    assert "Достигнут лимит: объём файлов (0,6 из 1 МБ)" in response.text

    def text_record(title: str):
        data = {"csrf_token": page_csrf(client, "/vault"), "type": "text", "title": title, "content": "x"}
        return client.post("/records", data=data)

    assert text_record("Запись 2").status_code == 303
    response = text_record("Запись 3")  # третья запись
    assert response.status_code == 409
    assert error_codes(response.text) == ["LIMIT_EXCEEDED"]
    assert "Достигнут лимит: записи (2 из 2)" in response.text
    assert db.scalar(select(func.count()).select_from(Record)) == 2

    add_heir(client, "Первый")
    response = client.post("/heirs", data={"csrf_token": page_csrf(client, "/heirs"), "name": "Второй"})
    assert response.status_code == 409
    assert error_codes(response.text) == ["LIMIT_EXCEEDED"]
    assert "Достигнут лимит: наследники (1 из 1)" in response.text
    assert db.scalar(select(func.count()).select_from(Heir)) == 1


def test_at06_vault_counters(client, mail, settings):
    signup(client, mail)
    post_file(client, "Файл", "file.pdf", b"%PDF" + b"\x00" * (1024 * 1024))
    page = client.get("/vault").text
    assert "Записей: 1 из 50 · Файлы: 1,0 из 100 МБ · Наследников: 0 из 5" in page
