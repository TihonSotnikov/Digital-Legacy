"""Вспомогательные функции тестов (4.4)."""

from __future__ import annotations

import re
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock, crypto
from app.models import AuditEvent, Heir, HeirKey, InheritanceRequest, User, heir_record_access
from app.security import hash_password

DEFAULT_PASSWORD = "correct-horse-battery"


# --- Прямая подготовка состояния в БД ---------------------------------------------


def make_user(
    db: Session,
    email: str = "owner@example.com",
    last_name: str = "Смирнова",
    first_name: str = "Анна",
    middle_name: str | None = "Сергеевна",
    verified: bool = True,
    password: str = DEFAULT_PASSWORD,
) -> User:
    now = clock.now()
    user = User(
        id=uuid.uuid4(),
        email=email,
        password_hash=hash_password(password),
        last_name=last_name,
        first_name=first_name,
        middle_name=middle_name,
        email_verified_at=now if verified else None,
        last_login_at=now,
        created_at=now,
    )
    db.add(user)
    db.commit()
    return user


def make_heir(db: Session, user: User, name: str = "Иван", record_ids: list[uuid.UUID] = ()) -> Heir:
    heir = Heir(id=uuid.uuid4(), user_id=user.id, name=name, created_at=clock.now())
    db.add(heir)
    db.flush()
    for record_id in record_ids:
        db.execute(heir_record_access.insert().values(heir_id=heir.id, record_id=record_id))
    db.commit()
    return heir


def make_key(db: Session, heir: Heir) -> tuple[HeirKey, str]:
    key = crypto.generate_heir_key()
    heir_key = HeirKey(
        id=uuid.uuid4(), heir_id=heir.id, key_hash=crypto.hash_heir_key(key), created_at=clock.now()
    )
    db.add(heir_key)
    db.commit()
    return heir_key, key


def status_fields(status: str) -> dict[str, Any]:
    """Поля, обязательные для статуса по CHECK-ограничениям 2.3.1."""
    now = clock.now()
    if status == "WAITING_CANCELLATION":
        return {"notified_at": now, "waiting_until": now + timedelta(days=14)}
    if status == "RELEASED":
        return {"notified_at": now, "waiting_until": now, "reminder_sent_at": now, "released_at": now}
    if status == "CANCELLED":
        return {"cancelled_at": now}
    if status == "REJECTED":
        return {"rejected_at": now, "check_result": {"accepted": False, "reasons": ["NAME_MISMATCH"]}}
    if status == "DOCUMENT_ACCEPTED":
        return {"check_result": {"accepted": True, "reasons": []}}
    return {}


def make_request(
    db: Session, heir: Heir, heir_key: HeirKey, status: str = "PENDING_REVIEW", **fields: Any
) -> InheritanceRequest:
    now = clock.now()
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "heir_id": heir.id,
        "heir_key_id": heir_key.id,
        "status": status,
        "doc_mime_type": "image/png",
        "doc_stored": status == "PENDING_REVIEW",
        "ocr_attempts": 0,
        "created_at": now,
        "updated_at": now,
    }
    values.update(status_fields(status))
    values.update(fields)
    request = InheritanceRequest(**values)
    db.add(request)
    db.commit()
    return request


def events(db: Session, event_type: str | None = None) -> list[AuditEvent]:
    db.expire_all()
    stmt = select(AuditEvent).order_by(AuditEvent.id)
    if event_type:
        stmt = stmt.where(AuditEvent.event_type == event_type)
    return list(db.execute(stmt).scalars())


def reload(db: Session, obj):
    db.expire_all()
    return db.get(type(obj), obj.id)


# --- HTML --------------------------------------------------------------------------

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def csrf_from(html: str) -> str:
    match = CSRF_RE.search(html)
    assert match, "csrf_token не найден на странице"
    return match.group(1)


def error_codes(html: str) -> list[str]:
    return re.findall(r'data-error-code="([A-Z_]+)"', html)
