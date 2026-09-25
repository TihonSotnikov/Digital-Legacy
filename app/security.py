"""Пароли, сессии, CSRF и ограничения частоты запросов (1.1.3, 3.1.4, 3.2.4)."""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from app import clock

# --- Пароли ---------------------------------------------------------------------

MIN_PASSWORD_LENGTH = 10
_hasher = PasswordHasher()  # argon2id
_DUMMY_HASH = _hasher.hash("timing-equalizer-password")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    """Проверка пароля; при отсутствии пользователя сверяется с фиктивным хешем."""
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# --- Ограничения частоты (slowapi, хранение в памяти) ----------------------------

LOGIN_RATE_LIMIT = "5/15minutes"
REGISTER_RATE_LIMIT = "5/hour"
VERIFY_RESEND_RATE_LIMIT = "3/hour"
HEIR_LOGIN_RATE_LIMIT = "5/15minutes"
CANCEL_RATE_LIMIT = "10/15minutes"


def owner_rate_key(request: Request) -> str:
    uid = request.session.get("uid")
    return f"owner:{uid}" if uid else f"ip:{get_remote_address(request)}"


limiter = Limiter(key_func=get_remote_address, storage_uri="memory://")


# --- CSRF -----------------------------------------------------------------------


def ensure_csrf(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def csrf_valid(request: Request, submitted: str | None) -> bool:
    expected = request.session.get("csrf")
    if not expected or not submitted:
        return False
    return secrets.compare_digest(str(expected).encode(), str(submitted).encode())


# --- Сессии ---------------------------------------------------------------------


def login_owner(request: Request, user_id: uuid.UUID) -> None:
    """Вход Владельца: сессия очищается (защита от фиксации), новые uid и csrf."""
    request.session.clear()
    request.session["uid"] = str(user_id)
    request.session["csrf"] = secrets.token_urlsafe(32)


def login_heir(request: Request, heir_key_id: uuid.UUID) -> None:
    request.session.clear()
    request.session["hkid"] = str(heir_key_id)
    request.session["hk_auth_at"] = clock.now().isoformat()
    request.session["csrf"] = secrets.token_urlsafe(32)


def clear_heir_session(request: Request) -> None:
    request.session.pop("hkid", None)
    request.session.pop("hk_auth_at", None)


def heir_auth_time(request: Request) -> datetime | None:
    raw = request.session.get("hk_auth_at")
    try:
        return datetime.fromisoformat(raw) if raw else None
    except (TypeError, ValueError):
        return None


def session_uuid(request: Request, field: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(request.session.get(field) or "")
    except (TypeError, ValueError):
        return None


def flash(request: Request, text: str, level: str = "success", code: str | None = None) -> None:
    # Список переприсваивается целиком: Starlette сохраняет cookie только при изменении сессии.
    request.session["flash"] = [
        *request.session.get("flash", []),
        {"level": level, "text": text, "code": code},
    ]


def pop_flashes(request: Request) -> list[dict]:
    return list(request.session.pop("flash", None) or [])


def set_flash_key(request: Request, heir_id: uuid.UUID, key: str) -> None:
    request.session["flash_key"] = {"heir_id": str(heir_id), "key": key}


def pop_flash_key(request: Request, heir_id: uuid.UUID) -> str | None:
    """Ключ для однократного показа удаляется из сессии при первом показе."""
    data = request.session.get("flash_key")
    if isinstance(data, dict) and data.get("heir_id") == str(heir_id):
        request.session.pop("flash_key", None)
        return data.get("key")
    return None


def safe_next(value: str | None) -> str | None:
    """next принимается, только если начинается с / и не начинается с // (3.1.1)."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return None
    if "\\" in value or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return None
    return value
