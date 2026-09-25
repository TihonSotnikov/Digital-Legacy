"""Зашифрованные файлы на диске: records/{record_id}.bin и docs/{request_id}.bin (2.1.3)."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from app import crypto
from app.config import get_settings


def files_root() -> Path:
    return Path(get_settings().FILES_DIR)


def record_path(record_id: uuid.UUID | str) -> Path:
    # Путь строится только из UUID: пользовательские имена файлов не используются.
    return files_root() / "records" / f"{uuid.UUID(str(record_id))}.bin"


def doc_path(request_id: uuid.UUID | str) -> Path:
    return files_root() / "docs" / f"{uuid.UUID(str(request_id))}.bin"


def write_atomic(path: Path, data: bytes) -> None:
    """Запись через временный файл в том же каталоге с последующим переименованием."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def delete(path: Path) -> None:
    """Отсутствие файла ошибкой не считается (2.3.3)."""
    path.unlink(missing_ok=True)


def save_record_file(record_id: uuid.UUID, plaintext: bytes) -> None:
    write_atomic(record_path(record_id), crypto.encrypt(plaintext, crypto.record_aad(record_id)))


def load_record_file(record_id: uuid.UUID) -> bytes:
    """Файл записи; DecryptionError при подмене или повреждении."""
    return crypto.decrypt(record_path(record_id).read_bytes(), crypto.record_aad(record_id))


def save_document(request_id: uuid.UUID, plaintext: bytes) -> None:
    write_atomic(doc_path(request_id), crypto.encrypt(plaintext, crypto.doc_aad(request_id)))


def load_document(request_id: uuid.UUID) -> bytes:
    """Документ; FileNotFoundError, если файла нет, DecryptionError при повреждении."""
    return crypto.decrypt(doc_path(request_id).read_bytes(), crypto.doc_aad(request_id))
