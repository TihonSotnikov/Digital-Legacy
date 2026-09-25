"""Токены ссылок подтверждения email и отмены запроса (3.2.3). В БД не хранятся."""

from __future__ import annotations

import uuid

from itsdangerous import BadData, TimestampSigner, URLSafeSerializer, URLSafeTimedSerializer

from app import clock
from app.config import get_settings


class ClockTimestampSigner(TimestampSigner):
    """Время подписи и проверки срока берётся из app.clock (подменяется в тестах)."""

    def get_timestamp(self) -> int:
        return int(clock.now().timestamp())


def _verify_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(
        get_settings().SECRET_KEY, salt="verify-email", signer=ClockTimestampSigner
    )


def _cancel_serializer() -> URLSafeSerializer:
    return URLSafeSerializer(get_settings().SECRET_KEY, salt="cancel-request")


def make_verify_token(user_id: uuid.UUID) -> str:
    return _verify_serializer().dumps(str(user_id))


def read_verify_token(token: str) -> uuid.UUID | None:
    """ID пользователя или None при неверной подписи или истёкшем сроке."""
    max_age = get_settings().VERIFY_TOKEN_TTL_HOURS * 3600
    try:
        return uuid.UUID(_verify_serializer().loads(token, max_age=max_age))
    except (BadData, ValueError, TypeError):
        return None


def make_cancel_token(request_id: uuid.UUID) -> str:
    """Детерминированный токен: одна и та же ссылка для писем E2 и E3."""
    return _cancel_serializer().dumps(str(request_id))


def read_cancel_token(token: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(_cancel_serializer().loads(token))
    except (BadData, ValueError, TypeError):
        return None


def verify_url(user_id: uuid.UUID) -> str:
    return f"{get_settings().base_url}/verify-email/{make_verify_token(user_id)}"


def cancel_url(request_id: uuid.UUID) -> str:
    return f"{get_settings().base_url}/cancel/{make_cancel_token(request_id)}"
