"""AT-05: файловая запись."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import select

from app import storage
from app.models import Record
from app.routes import MB
from tests.helpers import add_file_record, error_codes, page_csrf, post_file, signup

PNG = b"\x89PNG\r\n\x1a\n" + os.urandom(4096)


def test_at05_file_encrypted_downloaded_and_deleted(client, mail, db, files_dir):
    signup(client, mail)
    record_id = add_file_record(client, "Скан паспорта", "паспорт скан.png", PNG)

    record = db.get(Record, record_id)
    assert (record.type, record.file_name, record.mime_type, record.size_bytes) == (
        "file", "паспорт скан.png", "image/png", len(PNG)
    )
    assert record.ciphertext is None
    path = storage.record_path(record_id)
    assert path.parent == files_dir / "records"
    on_disk = path.read_bytes()
    assert PNG not in on_disk and PNG[:16] not in on_disk and PNG[-64:] not in on_disk

    response = client.get(f"/records/{record_id}/download")
    assert response.status_code == 200
    assert response.content == PNG
    assert response.headers["Content-Disposition"].startswith("attachment;")
    assert "filename*=UTF-8''" in response.headers["Content-Disposition"]
    assert response.headers["Cache-Control"] == "no-store"

    response = client.post(f"/records/{record_id}/delete", data={"csrf_token": page_csrf(client, "/vault")})
    assert response.status_code == 303
    assert not path.exists()
    db.expire_all()
    assert db.get(Record, record_id) is None


@pytest.mark.parametrize(
    "filename, data",
    [
        ("program.exe", b"MZ" + os.urandom(100)),  # недопустимое расширение
        ("photo.png", b"\xff\xd8\xff" + os.urandom(100)),  # сигнатура JPG у .png
        ("contract.pdf", b"PK\x03\x04" + os.urandom(100)),  # сигнатура ZIP у .pdf
        ("notes.txt", b"\xff\xfe\x00 not utf-8 \xc3\x28"),  # некорректный UTF-8
        ("archive", b"PK\x03\x04" + os.urandom(100)),  # без расширения
    ],
)
def test_at05_unsupported_extension_or_signature(client, mail, db, files_dir, filename, data):
    signup(client, mail)
    response = post_file(client, "Файл", filename, data)
    assert response.status_code == 415
    assert error_codes(response.text) == ["UNSUPPORTED_FILE_TYPE"]
    assert "JPG, PNG, PDF, DOCX, XLSX, ZIP, TXT" in response.text
    assert db.execute(select(Record)).first() is None
    assert not (files_dir / "records").exists() or not any((files_dir / "records").iterdir())


@pytest.mark.parametrize(
    "filename, data, mime",
    [
        ("photo.JPG", b"\xff\xd8\xff\xe0" + os.urandom(64), "image/jpeg"),
        ("doc.docx", b"PK\x03\x04" + os.urandom(64), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("table.xlsx", b"PK\x03\x04" + os.urandom(64), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("files.zip", b"PK\x03\x04" + os.urandom(64), "application/zip"),
        ("scan.pdf", b"%PDF-1.7" + os.urandom(64), "application/pdf"),
        ("notes.txt", "Заметки в UTF-8".encode(), "text/plain"),
    ],
)
def test_at05_allowed_types_accepted(client, mail, db, filename, data, mime):
    signup(client, mail)
    record_id = add_file_record(client, "Файл", filename, data)
    assert db.get(Record, record_id).mime_type == mime


def test_at05_file_larger_than_max_file_mb(client, mail, db, settings):
    signup(client, mail)
    limit = settings.MAX_FILE_MB * MB
    response = post_file(client, "Большой", "big.pdf", b"%PDF" + b"\x00" * (limit - 4 + 1))
    assert response.status_code == 413
    assert error_codes(response.text) == ["FILE_TOO_LARGE"]
    assert f"Максимальный размер — {settings.MAX_FILE_MB} МБ" in response.text

    # Тело намного больше лимита: чтение прерывается до разбора формы.
    response = post_file(client, "Огромный", "huge.pdf", b"%PDF" + b"\x00" * (limit + 2 * MB))
    assert response.status_code == 413
    assert error_codes(response.text) == ["FILE_TOO_LARGE"]
    assert db.execute(select(Record)).first() is None

    response = post_file(client, "Ровно лимит", "exact.pdf", b"%PDF" + b"\x00" * (limit - 4))
    assert response.status_code == 303
