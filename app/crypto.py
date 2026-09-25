"""AES-256-GCM (3.2.1), ключ наследника (3.2.2)."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import get_settings

NONCE_SIZE = 12
TAG_SIZE = 16
KEY_VERSION = 1  # records.key_version; зарезервировано для ротации ключа


class DecryptionError(Exception):
    """Неверный тег, другой ключ, чужой AAD или неизвестная версия ключа."""


def _cipher(key: bytes | None) -> AESGCM:
    key = get_settings().master_key if key is None else key
    if len(key) != 32:
        raise ValueError("MASTER_KEY должен быть ровно 32 байта (AES-256)")
    return AESGCM(key)


def encrypt(plaintext: bytes, aad: str, key: bytes | None = None) -> bytes:
    """nonce (12 случайных байт) || AESGCM.encrypt(nonce, plaintext, aad)."""
    nonce = os.urandom(NONCE_SIZE)
    return nonce + _cipher(key).encrypt(nonce, plaintext, aad.encode("ascii"))


def decrypt(blob: bytes, aad: str, key: bytes | None = None) -> bytes:
    cipher = _cipher(key)
    if len(blob) < NONCE_SIZE + TAG_SIZE:
        raise DecryptionError("шифротекст слишком короткий")
    try:
        return cipher.decrypt(blob[:NONCE_SIZE], blob[NONCE_SIZE:], aad.encode("ascii"))
    except InvalidTag:
        raise DecryptionError("ошибка проверки тега AES-GCM") from None


def record_aad(record_id: uuid.UUID | str) -> str:
    return f"record:{uuid.UUID(str(record_id))}"


def doc_aad(request_id: uuid.UUID | str) -> str:
    return f"doc:{uuid.UUID(str(request_id))}"


def check_key_version(key_version: int) -> None:
    if key_version != KEY_VERSION:
        raise DecryptionError(f"неподдерживаемая версия ключа: {key_version}")


# --- Ключ наследника ------------------------------------------------------------

HEIR_KEY_RE = re.compile(r"[0-9a-f]{64}")
_WHITESPACE_RE = re.compile(r"\s+")


def generate_heir_key() -> str:
    return secrets.token_hex(32)


def hash_heir_key(key: str) -> str:
    return hashlib.sha256(key.encode("ascii")).hexdigest()


def normalize_heir_key(raw: str | None) -> str | None:
    """Удалить пробельные символы, привести к нижнему регистру, проверить ^[0-9a-f]{64}$."""
    value = _WHITESPACE_RE.sub("", raw or "").lower()
    return value if HEIR_KEY_RE.fullmatch(value) else None


def format_heir_key(key: str) -> str:
    """Группы по 8 символов через пробел."""
    return " ".join(key[i : i + 8] for i in range(0, len(key), 8))
