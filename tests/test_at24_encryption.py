"""AT-24: шифрование."""

from __future__ import annotations

import base64
import logging
import os
import subprocess
import sys

import pytest
from sqlalchemy import update

from app import crypto
from app.models import Record
from tests.conftest import ROOT
from tests.helpers import add_text_record, error_codes, signup


@pytest.fixture
def two_records(client, mail, db):
    signup(client, mail)
    first = add_text_record(client, "Первая", "Первый секрет")
    second = add_text_record(client, "Вторая", "Второй секрет")
    return first, second


def ciphertext(db, record_id) -> bytes:
    db.expire_all()
    return db.get(Record, record_id).ciphertext


def test_at24_changed_byte_raises_decryption_error(client, db, two_records):
    first, _ = two_records
    blob = bytearray(ciphertext(db, first))
    blob[len(blob) // 2] ^= 0xFF
    db.execute(update(Record).where(Record.id == first).values(ciphertext=bytes(blob)))
    db.commit()
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(ciphertext(db, first), crypto.record_aad(first))
    response = client.get(f"/records/{first}")
    assert response.status_code == 500
    assert error_codes(response.text) == ["INTERNAL_ERROR"]


def test_at24_swapped_ciphertexts_raise_decryption_error(client, db, two_records):
    first, second = two_records
    blob_first, blob_second = ciphertext(db, first), ciphertext(db, second)
    db.execute(update(Record).where(Record.id == first).values(ciphertext=blob_second))
    db.execute(update(Record).where(Record.id == second).values(ciphertext=blob_first))
    db.commit()
    for record_id in (first, second):
        with pytest.raises(crypto.DecryptionError):
            crypto.decrypt(ciphertext(db, record_id), crypto.record_aad(record_id))
        assert client.get(f"/records/{record_id}").status_code == 500


def test_at24_other_master_key_gives_500_and_error_log(client, two_records, settings, monkeypatch, caplog):
    first, _ = two_records
    monkeypatch.setattr(settings, "MASTER_KEY", base64.b64encode(os.urandom(32)).decode())
    with caplog.at_level(logging.DEBUG):
        response = client.get(f"/records/{first}")
    assert response.status_code == 500
    assert error_codes(response.text) == ["INTERNAL_ERROR"]
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors and any("расшифрования" in r.getMessage() for r in errors)
    assert "Первый секрет" not in caplog.text


@pytest.mark.parametrize("command", [["-c", "import app.main"], ["-m", "app.worker"]])
def test_at24_master_key_of_wrong_length_fails_at_startup(command):
    env = {**os.environ, "MASTER_KEY": base64.b64encode(os.urandom(31)).decode()}
    result = subprocess.run([sys.executable, *command], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode != 0
    assert "MASTER_KEY должен быть base64 от ровно 32 байт" in result.stderr
