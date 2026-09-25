"""Машина состояний наследственного запроса (2.3.4, 3.2.5)."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import audit, clock
from app.models import ACTIVE_STATUSES, Heir, HeirKey, InheritanceRequest

ALLOWED = {
    ("PENDING_REVIEW", "DOCUMENT_ACCEPTED"),
    ("PENDING_REVIEW", "REJECTED"),
    ("PENDING_REVIEW", "CANCELLED"),
    ("DOCUMENT_ACCEPTED", "WAITING_CANCELLATION"),
    ("DOCUMENT_ACCEPTED", "CANCELLED"),
    ("WAITING_CANCELLATION", "RELEASED"),
    ("WAITING_CANCELLATION", "CANCELLED"),
}

# Отмена Владельцем (из кабинета и по ссылке): одинаковые исходные статусы.
CANCELLABLE = frozenset(ACTIVE_STATUSES)

_PROTECTED_FIELDS = {"id", "status", "updated_at", "heir_id", "heir_key_id", "created_at"}
_COLUMNS = set(InheritanceRequest.__table__.columns.keys())


class TransitionConflict(Exception):
    """Статус запроса уже изменился: условное обновление не затронуло строку."""


def transition(
    db: Session,
    request_id: uuid.UUID,
    from_statuses: Iterable[str],
    to: str,
    **fields: Any,
) -> str:
    """Атомарно меняет статус и возвращает предыдущий. Иначе — TransitionConflict.

    Изменения пишутся в транзакцию вызывающего кода; фиксирует её вызывающий код.
    """
    sources = set(from_statuses)
    if not sources:
        raise ValueError("не заданы исходные статусы")
    for source in sources:
        if (source, to) not in ALLOWED:
            raise ValueError(f"переход {source} → {to} запрещён")
    invalid = set(fields) - (_COLUMNS - _PROTECTED_FIELDS)
    if invalid:
        raise ValueError(f"недопустимые поля перехода: {sorted(invalid)}")

    now = clock.now()
    table = InheritanceRequest.__table__
    # Предыдущий статус берётся в том же запросе: подзапрос блокирует строку (FOR UPDATE),
    # а условие по статусу перепроверяется после ожидания конкурирующей транзакции.
    previous = (
        select(table.c.id, table.c.status)
        .where(table.c.id == request_id, table.c.status.in_(sorted(sources)))
        .with_for_update()
        .subquery("previous")
    )
    stmt = (
        update(table)
        .where(table.c.id == previous.c.id)
        .values(status=to, updated_at=now, **fields)
        .returning(previous.c.status, table.c.heir_id, table.c.heir_key_id, table.c.check_result)
    )
    db.flush()
    row = db.execute(stmt).one_or_none()
    if row is None:
        raise TransitionConflict(str(request_id))
    previous_status, heir_id, heir_key_id, check_result = row

    owner_id = db.execute(select(Heir.user_id).where(Heir.id == heir_id)).scalar_one()
    meta: dict[str, Any] = {"from": previous_status, "to": to}
    if check_result and check_result.get("reasons"):
        meta["reasons"] = list(check_result["reasons"])
    audit.log(db, owner_id, "REQUEST_STATUS_CHANGED", meta, request_id=request_id)

    if to == "CANCELLED":
        revoked = db.execute(
            update(HeirKey)
            .where(HeirKey.id == heir_key_id, HeirKey.revoked_at.is_(None))
            .values(revoked_at=now)
            .returning(HeirKey.id)
        ).scalar_one_or_none()
        if revoked is not None:
            audit.log(
                db,
                owner_id,
                "HEIR_KEY_REVOKED",
                {"heir_id": heir_id, "heir_key_id": heir_key_id, "reason": "cancel"},
                request_id=request_id,
            )

    # Загруженные в сессию объекты должны отражать новое состояние при следующем обращении.
    _expire(db, InheritanceRequest, request_id)
    _expire(db, HeirKey, heir_key_id)
    return previous_status


def _expire(db: Session, model: type, pk: uuid.UUID) -> None:
    cached = db.identity_map.get(db.identity_key(model, pk))
    if cached is not None:
        db.expire(cached)
