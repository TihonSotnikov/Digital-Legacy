"""Аккаунт Владельца: просмотр и удаление (FR-17, 1.2.5, 2.3.3)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import security, storage
from app.db import get_db
from app.models import Heir, InheritanceRequest, Record, User
from app.routes import redirect, render, require_owner, verify_csrf

router = APIRouter()
logger = logging.getLogger("app.account")


def account_context(owner: User, alert: dict | None = None) -> dict:
    return {"account": owner, "alert": alert}


@router.get("/account")
def account(request: Request, owner: User = Depends(require_owner)):
    return render(request, "account.html", account_context(owner))


@router.post("/account/delete")
def delete_account(
    request: Request,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
    csrf_token: str = Form(""),
    password: str = Form(""),
):
    verify_csrf(request, csrf_token)
    if not security.verify_password(owner.password_hash, password):
        alert = {"code": "INVALID_CREDENTIALS", "message": "Неверный пароль"}
        return render(request, "account.html", account_context(owner, alert), status_code=401)

    record_files = list(
        db.execute(select(Record.id).where(Record.user_id == owner.id, Record.type == "file")).scalars()
    )
    documents = list(
        db.execute(
            select(InheritanceRequest.id)
            .join(Heir, Heir.id == InheritanceRequest.heir_id)
            .where(Heir.user_id == owner.id)
        ).scalars()
    )
    # Одна транзакция: каскадно удаляются записи, наследники, ключи, назначения, запросы и аудит.
    db.execute(delete(User).where(User.id == owner.id))
    db.commit()

    # После фиксации транзакции — файлы записей и документы (отсутствие файла не ошибка).
    for record_id in record_files:
        storage.delete(storage.record_path(record_id))
    for request_id in documents:
        storage.delete(storage.doc_path(request_id))
    logger.info("Аккаунт удалён")

    request.session.clear()
    request.state.owner = None
    request.state.banners_factory = None
    security.flash(request, "Аккаунт удалён")
    return redirect("/")
