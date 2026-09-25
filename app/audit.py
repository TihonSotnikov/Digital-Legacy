"""Журнал аудита критических событий (1.1.3, 3.2.10)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import insert
from sqlalchemy.orm import Session

from app import clock
from app.models import AuditEvent

# Тип события → допустимые ключи meta. В meta запрещены секреты, содержимое, ФИО и IP.
EVENT_META: dict[str, frozenset[str]] = {
    "USER_REGISTERED": frozenset(),
    "EMAIL_VERIFIED": frozenset(),
    "LOGIN_SUCCEEDED": frozenset(),
    "LOGIN_FAILED": frozenset(),
    "PROFILE_UPDATED": frozenset(),
    "RECORD_CREATED": frozenset({"record_id", "type"}),
    "RECORD_UPDATED": frozenset({"record_id", "type"}),
    "RECORD_DELETED": frozenset({"record_id", "type"}),
    "HEIR_CREATED": frozenset({"heir_id", "record_count"}),
    "HEIR_UPDATED": frozenset({"heir_id", "record_count"}),
    "HEIR_DELETED": frozenset({"heir_id", "record_count"}),
    "HEIR_KEY_ISSUED": frozenset({"heir_id", "heir_key_id"}),
    "HEIR_KEY_REVOKED": frozenset({"heir_id", "heir_key_id", "reason"}),
    "REQUEST_CREATED": frozenset({"heir_id"}),
    "REQUEST_STATUS_CHANGED": frozenset({"from", "to", "reasons"}),
    "OWNER_NOTIFIED": frozenset(),
    "REMINDER_SENT": frozenset({"waiting_until"}),
    "HEIR_DATA_VIEWED": frozenset({"heir_id"}),
    "HEIR_FILE_DOWNLOADED": frozenset({"heir_id", "record_id"}),
}


def _plain(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def log(
    db: Session,
    user_id: uuid.UUID,
    event_type: str,
    meta: dict[str, Any] | None = None,
    request_id: uuid.UUID | None = None,
) -> None:
    """Пишет событие в текущей транзакции вызывающего кода."""
    allowed = EVENT_META.get(event_type)
    if allowed is None:
        raise ValueError(f"неизвестный тип события аудита: {event_type}")
    meta = {key: _plain(value) for key, value in (meta or {}).items()}
    extra = set(meta) - allowed
    if extra:
        raise ValueError(f"недопустимые поля meta для {event_type}: {sorted(extra)}")
    db.flush()  # родительские строки должны существовать до вставки события
    db.execute(
        insert(AuditEvent).values(
            user_id=user_id,
            request_id=request_id,
            event_type=event_type,
            meta=meta,
            created_at=clock.now(),
        )
    )
