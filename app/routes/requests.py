"""Запросы в кабинете Владельца и отмена (FR-13, FR-14, 3.2.5)."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock, security
from app.config import current_request_id
from app.db import get_db
from app.email.backends import send_best_effort
from app.models import ACTIVE_STATUSES, Heir, InheritanceRequest, User
from app.routes import AppError, parse_id, redirect, render, require_owner, verify_csrf
from app.transitions import CANCELLABLE, TransitionConflict, transition

router = APIRouter()
logger = logging.getLogger("app.requests")


def cancel_request(db: Session, request_id: uuid.UUID) -> InheritanceRequest:
    """Отмена Владельцем: условный переход «активный статус → CANCELLED» с отзывом ключа."""
    token = current_request_id.set(str(request_id))
    try:
        try:
            transition(db, request_id, CANCELLABLE, "CANCELLED", cancelled_at=clock.now())
        except TransitionConflict:
            db.rollback()
            raise AppError("INVALID_TRANSITION") from None
        db.commit()
        logger.info("Запрос отменён Владельцем")
        cancelled = db.get(InheritanceRequest, request_id)
        send_best_effort("E9", cancelled.heir_contact_email)  # P1: одна попытка, ошибка в лог
        return cancelled
    finally:
        current_request_id.reset(token)


@router.get("/requests")
def requests_list(request: Request, owner: User = Depends(require_owner), db: Session = Depends(get_db)):
    rows = db.execute(
        select(InheritanceRequest, Heir.name)
        .join(Heir, Heir.id == InheritanceRequest.heir_id)
        .where(Heir.user_id == owner.id)
        .order_by(InheritanceRequest.created_at.desc())
    ).all()
    items = [{"request": item, "heir_name": name, "active": item.status in ACTIVE_STATUSES} for item, name in rows]
    return render(request, "requests.html", {"items": items})


@router.post("/requests/{request_id}/cancel")
def cancel_from_cabinet(
    request_id: str,
    request: Request,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
    csrf_token: str = Form(""),
):
    verify_csrf(request, csrf_token)
    found = db.execute(
        select(InheritanceRequest.id)
        .join(Heir, Heir.id == InheritanceRequest.heir_id)
        .where(InheritanceRequest.id == parse_id(request_id), Heir.user_id == owner.id)
    ).scalar_one_or_none()
    if found is None:
        raise AppError("NOT_FOUND")
    cancel_request(db, found)
    security.flash(request, "Запрос отменён")
    return redirect("/requests")
