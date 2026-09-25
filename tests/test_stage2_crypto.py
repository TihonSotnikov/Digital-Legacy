"""Ядро (этап 2): unit-тесты crypto.py и storage.py, соответствие моделей миграциям."""

from __future__ import annotations

import os
import uuid

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext

from app import crypto, storage
from app.db import get_engine
from app.models import Base


def test_encrypt_decrypt_roundtrip_with_random_nonce():
    aad = crypto.record_aad(uuid.uuid4())
    first = crypto.encrypt("Пароль от почты".encode(), aad)
    second = crypto.encrypt("Пароль от почты".encode(), aad)
    assert first != second and first[:12] != second[:12]
    assert len(first) == 12 + len("Пароль от почты".encode()) + 16
    assert crypto.decrypt(first, aad).decode() == "Пароль от почты"


@pytest.mark.parametrize("position", [0, 11, 12, 20, -17, -1])
def test_crypto_changed_byte_raises_decryption_error(position):
    aad = crypto.record_aad(uuid.uuid4())
    blob = bytearray(crypto.encrypt(b"secret data " * 3, aad))
    blob[position] ^= 0x01
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(bytes(blob), aad)


def test_crypto_swapped_aad_raises_decryption_error():
    record_a, record_b = uuid.uuid4(), uuid.uuid4()
    blob_a = crypto.encrypt(b"A", crypto.record_aad(record_a))
    blob_b = crypto.encrypt(b"B", crypto.record_aad(record_b))
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(blob_a, crypto.record_aad(record_b))
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(blob_b, crypto.record_aad(record_a))
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(blob_a, crypto.doc_aad(record_a))


def test_crypto_aad_uses_canonical_lowercase_uuid():
    value = uuid.uuid4()
    assert crypto.record_aad(str(value).upper()) == f"record:{str(value).lower()}"
    assert crypto.doc_aad(value) == f"doc:{value}"


@pytest.mark.parametrize("length", [0, 16, 24, 31, 33])
def test_crypto_wrong_key_length_raises(length):
    with pytest.raises(ValueError):
        crypto.encrypt(b"data", "record:x", key=os.urandom(length) if length else b"")
    with pytest.raises(ValueError):
        crypto.decrypt(b"\x00" * 40, "record:x", key=os.urandom(length) if length else b"")


def test_crypto_other_master_key_raises():
    aad = crypto.doc_aad(uuid.uuid4())
    blob = crypto.encrypt(b"document", aad, key=os.urandom(32))
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(blob, aad, key=os.urandom(32))


def test_crypto_truncated_blob_raises():
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(b"\x00" * 27, "record:x")


def test_crypto_key_version_other_than_1_fails():
    crypto.check_key_version(1)
    with pytest.raises(crypto.DecryptionError):
        crypto.check_key_version(2)


def test_heir_key_generation_hash_and_normalization():
    key = crypto.generate_heir_key()
    assert crypto.HEIR_KEY_RE.fullmatch(key)
    assert crypto.hash_heir_key(key) == __import__("hashlib").sha256(key.encode("ascii")).hexdigest()
    grouped = crypto.format_heir_key(key)
    assert grouped.count(" ") == 7 and all(len(part) == 8 for part in grouped.split(" "))
    messy = " " + grouped.upper().replace(" ", " \t\n ") + " "
    assert crypto.normalize_heir_key(messy) == key
    assert crypto.normalize_heir_key(key[:63]) is None
    assert crypto.normalize_heir_key(key[:63] + "g") is None
    assert crypto.normalize_heir_key(None) is None


def test_storage_paths_are_built_from_uuid_only(files_dir):
    record_id = uuid.uuid4()
    assert storage.record_path(record_id) == files_dir / "records" / f"{record_id}.bin"
    assert storage.doc_path(str(record_id).upper()) == files_dir / "docs" / f"{record_id}.bin"
    with pytest.raises(ValueError):
        storage.record_path("../../etc/passwd")


def test_storage_encrypted_roundtrip_and_delete(files_dir):
    record_id, request_id = uuid.uuid4(), uuid.uuid4()
    payload = os.urandom(4096)
    storage.save_record_file(record_id, payload)
    storage.save_document(request_id, b"%PDF-1.4 document")
    on_disk = storage.record_path(record_id).read_bytes()
    assert payload not in on_disk and on_disk != payload
    assert storage.load_record_file(record_id) == payload
    assert storage.load_document(request_id) == b"%PDF-1.4 document"
    assert sorted(p.name for p in (files_dir / "records").iterdir()) == [f"{record_id}.bin"]
    # Документ под AAD записи не расшифровывается.
    storage.record_path(record_id).write_bytes(storage.doc_path(request_id).read_bytes())
    with pytest.raises(crypto.DecryptionError):
        storage.load_record_file(record_id)
    storage.delete(storage.record_path(record_id))
    storage.delete(storage.record_path(record_id))  # отсутствие файла — не ошибка
    assert not storage.record_path(record_id).exists()


def test_models_match_migrated_schema():
    with get_engine().connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []
