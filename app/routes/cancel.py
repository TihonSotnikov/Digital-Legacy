"""Отмена запроса по ссылке из письма без входа (FR-13, сценарий Е 2.2.7)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import security, tokens
from app.db import get_db
from app.models import ACTIVE_STATUSES, Heir, InheritanceRequest
from app.routes import AppError, redirect, render, verify_csrf
from app.routes.requests import cancel_request

router = APIRouter()


def request_by_token(db: Session, token: str) -> tuple[InheritanceRequest, Heir]:
    request_id = tokens.read_cancel_token(token)
    if request_id is None:
        raise AppError("INVALID_TOKEN")
    row = db.execute(
        select(InheritanceRequest, Heir)
        .join(Heir, Heir.id == InheritanceRequest.heir_id)
        .where(InheritanceRequest.id == request_id)
    ).first()
    if row is None:
        raise AppError("NOT_FOUND")
    return row[0], row[1]


@router.get("/cancel/{token}")
def cancel_page(token: str, request: Request, db: Session = Depends(get_db)):
    item, heir = request_by_token(db, token)
    context = {
        "item": item,
        "heir": heir,
        "token": token,
        "active": item.status in ACTIVE_STATUSES,
    }
    return render(request, "cancel.html", context, no_store=True)


@router.post("/cancel/{token}")
@security.limiter.limit(security.CANCEL_RATE_LIMIT)
def cancel_by_link(token: str, request: Request, db: Session = Depends(get_db), csrf_token: str = Form("")):
    verify_csrf(request, csrf_token)
    item, _ = request_by_token(db, token)
    cancel_request(db, item.id)
    security.flash(
        request,
        "Запрос отменён. Ключ наследника отозван. Новый ключ можно выпустить в личном кабинете",
    )
    return redirect(f"/cancel/{token}")
