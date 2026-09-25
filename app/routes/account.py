"""Аккаунт Владельца: просмотр и удаление (FR-17, 1.2.5, 2.3.3)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import audit, security, storage
from app.db import get_db
from app.models import ACTIVE_STATUSES, Heir, InheritanceRequest, Record, User
from app.routes import (
    AppError,
    field_error,
    normalize_text_field,
    redirect,
    render,
    require_owner,
    valid_name_part,
    verify_csrf,
)

router = APIRouter()
logger = logging.getLogger("app.account")


def has_active_request(db: Session, owner_id) -> bool:
    return (
        db.execute(
            select(InheritanceRequest.id)
            .join(Heir, Heir.id == InheritanceRequest.heir_id)
            .where(Heir.user_id == owner_id, InheritanceRequest.status.in_(ACTIVE_STATUSES))
        ).first()
        is not None
    )


def account_context(db: Session, owner: User, alert=None, profile=None, errors=None) -> dict:
    values = profile or {
        "last_name": owner.last_name,
        "first_name": owner.first_name,
        "middle_name": owner.middle_name or "",
    }
    return {
        "account": owner,
        "alert": alert,
        "profile": values,
        "errors": errors or {},
        "profile_locked": has_active_request(db, owner.id),
    }


@router.get("/account")
def account(request: Request, owner: User = Depends(require_owner), db: Session = Depends(get_db)):
    return render(request, "account.html", account_context(db, owner))


@router.post("/account/profile")
def update_profile(
    request: Request,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
    csrf_token: str = Form(""),
    last_name: str = Form(""),
    first_name: str = Form(""),
    middle_name: str = Form(""),
):
    """Изменение ФИО (FR-19) только при отсутствии активных запросов."""
    verify_csrf(request, csrf_token)
    # Подача запроса блокирует строку наследника: блокировка наследников Владельца исключает
    # появление активного запроса между проверкой и изменением ФИО.
    db.execute(select(Heir.id).where(Heir.user_id == owner.id).with_for_update()).all()
    if has_active_request(db, owner.id):
        raise AppError("PROFILE_LOCKED")
    values = {
        "last_name": normalize_text_field(last_name),
        "first_name": normalize_text_field(first_name),
        "middle_name": normalize_text_field(middle_name),
    }
    errors = {}
    if not valid_name_part(values["last_name"]):
        errors["last_name"] = field_error("VALIDATION_ERROR", "Укажите фамилию: от 1 до 100 символов — буквы, пробел, дефис")
    if not valid_name_part(values["first_name"]):
        errors["first_name"] = field_error("VALIDATION_ERROR", "Укажите имя: от 1 до 100 символов — буквы, пробел, дефис")
    if values["middle_name"] and not valid_name_part(values["middle_name"]):
        errors["middle_name"] = field_error("VALIDATION_ERROR", "Отчество: до 100 символов — буквы, пробел, дефис")
    if errors:
        context = account_context(db, owner, profile=values, errors=errors)
        return render(request, "account.html", context, status_code=400)

    owner.last_name = values["last_name"]
    owner.first_name = values["first_name"]
    owner.middle_name = values["middle_name"] or None
    audit.log(db, owner.id, "PROFILE_UPDATED")
    db.commit()
    security.flash(request, "Изменения сохранены")
    return redirect("/account")


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
        return render(request, "account.html", account_context(db, owner, alert), status_code=401)

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
