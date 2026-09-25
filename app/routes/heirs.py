"""Наследники и ключи (FR-06, FR-07, сценарий Б 2.2.3, 3.2.2)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app import audit, clock, crypto, security, storage
from app.config import get_settings
from app.db import get_db
from app.models import ACTIVE_STATUSES, Heir, HeirKey, InheritanceRequest, Record, User, heir_record_access
from app.routes import (
    AppError,
    field_error,
    limit_error,
    parse_id,
    redirect,
    render,
    require_owner,
    verify_csrf,
)

router = APIRouter()


def owner_heir(db: Session, owner: User, heir_id: str, lock: bool = False) -> Heir:
    """Наследник Владельца; чужой или несуществующий — 404."""
    stmt = select(Heir).where(Heir.id == parse_id(heir_id), Heir.user_id == owner.id)
    if lock:
        stmt = stmt.with_for_update()
    heir = db.execute(stmt).scalar_one_or_none()
    if heir is None:
        raise AppError("NOT_FOUND")
    return heir


def has_active_request(db: Session, heir_id: uuid.UUID) -> bool:
    return (
        db.execute(
            select(InheritanceRequest.id).where(
                InheritanceRequest.heir_id == heir_id, InheritanceRequest.status.in_(ACTIVE_STATUSES)
            )
        ).first()
        is not None
    )


def owned_record_ids(db: Session, owner: User, raw_ids: list[str]) -> list[uuid.UUID]:
    """Все record_ids должны принадлежать Владельцу, иначе 404 (AT-07)."""
    ids = list(dict.fromkeys(parse_id(value) for value in raw_ids if value))
    if not ids:
        return []
    found = set(
        db.execute(select(Record.id).where(Record.id.in_(ids), Record.user_id == owner.id)).scalars()
    )
    if found != set(ids):
        raise AppError("NOT_FOUND")
    return ids


def owner_records(db: Session, owner: User) -> list[Record]:
    return list(
        db.execute(select(Record).where(Record.user_id == owner.id).order_by(Record.created_at)).scalars()
    )


def issue_key(db: Session, owner_id: uuid.UUID, heir_id: uuid.UUID) -> str:
    """Новый ключ: в БД только sha256; исходный ключ возвращается для однократного показа."""
    key = crypto.generate_heir_key()
    heir_key = HeirKey(
        id=uuid.uuid4(), heir_id=heir_id, key_hash=crypto.hash_heir_key(key), created_at=clock.now()
    )
    db.add(heir_key)
    audit.log(db, owner_id, "HEIR_KEY_ISSUED", {"heir_id": heir_id, "heir_key_id": heir_key.id})
    return key


def validate_name(name: str) -> dict[str, str] | None:
    if not 1 <= len(name) <= 100:
        return field_error("VALIDATION_ERROR", "Имя наследника: от 1 до 100 символов")
    return None


def form_context(db: Session, owner: User, mode: str, values: dict, errors: dict, heir: Heir | None = None) -> dict:
    return {"mode": mode, "heir": heir, "records": owner_records(db, owner), "values": values, "errors": errors}


def require_verified(owner: User) -> None:
    if owner.email_verified_at is None:
        raise AppError("EMAIL_NOT_VERIFIED")


# --- Список и создание -----------------------------------------------------------------------


@router.get("/heirs")
def heirs_list(request: Request, owner: User = Depends(require_owner), db: Session = Depends(get_db)):
    heirs = list(db.execute(select(Heir).where(Heir.user_id == owner.id).order_by(Heir.created_at)).scalars())
    ids = [heir.id for heir in heirs]
    record_counts = dict(
        db.execute(
            select(heir_record_access.c.heir_id, func.count())
            .where(heir_record_access.c.heir_id.in_(ids))
            .group_by(heir_record_access.c.heir_id)
        ).tuples().all()
    ) if ids else {}
    active_keys = {
        key.heir_id: key
        for key in db.execute(
            select(HeirKey).where(HeirKey.heir_id.in_(ids), HeirKey.revoked_at.is_(None))
        ).scalars()
    } if ids else {}
    last_requests: dict[uuid.UUID, InheritanceRequest] = {}
    if ids:
        for item in db.execute(
            select(InheritanceRequest)
            .where(InheritanceRequest.heir_id.in_(ids))
            .order_by(InheritanceRequest.created_at.desc())
        ).scalars():
            last_requests.setdefault(item.heir_id, item)
    context = {
        "heirs": heirs,
        "record_counts": record_counts,
        "active_keys": active_keys,
        "last_requests": last_requests,
        "max_heirs": get_settings().MAX_HEIRS,
    }
    return render(request, "heirs.html", context)


@router.get("/heirs/new")
def new_heir_form(request: Request, owner: User = Depends(require_owner), db: Session = Depends(get_db)):
    require_verified(owner)
    return render(request, "heir_form.html", form_context(db, owner, "new", {"record_ids": []}, {}))


@router.post("/heirs")
def create_heir(
    request: Request,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
    csrf_token: str = Form(""),
    name: str = Form(""),
    record_ids: list[str] = Form([]),
):
    verify_csrf(request, csrf_token)
    require_verified(owner)
    db.execute(select(User.id).where(User.id == owner.id).with_for_update())
    limit = get_settings().MAX_HEIRS
    count = db.scalar(select(func.count()).select_from(Heir).where(Heir.user_id == owner.id))
    if count >= limit:
        raise limit_error("наследники", count, limit)
    name = name.strip()
    if error := validate_name(name):
        values = {"name": name, "record_ids": record_ids}
        context = form_context(db, owner, "new", values, {"name": error})
        return render(request, "heir_form.html", context, status_code=400)
    ids = owned_record_ids(db, owner, record_ids)

    heir = Heir(id=uuid.uuid4(), user_id=owner.id, name=name, created_at=clock.now())
    db.add(heir)
    db.flush()
    for record_id in ids:
        db.execute(heir_record_access.insert().values(heir_id=heir.id, record_id=record_id))
    audit.log(db, owner.id, "HEIR_CREATED", {"heir_id": heir.id, "record_count": len(ids)})
    key = issue_key(db, owner.id, heir.id)
    db.commit()

    security.set_flash_key(request, heir.id, key)
    security.flash(request, "Наследник создан")
    return redirect(f"/heirs/{heir.id}/key")


@router.get("/heirs/{heir_id}/key")
def show_key(heir_id: str, request: Request, owner: User = Depends(require_owner), db: Session = Depends(get_db)):
    heir = owner_heir(db, owner, heir_id)
    key = security.pop_flash_key(request, heir.id)  # ключ удаляется из сессии при первом показе
    context = {"heir": heir, "key": crypto.format_heir_key(key) if key else None}
    return render(request, "heir_key.html", context, no_store=True)


# --- Изменение, перевыпуск ключа, удаление ------------------------------------------------------


@router.get("/heirs/{heir_id}/edit")
def edit_heir_form(heir_id: str, request: Request, owner: User = Depends(require_owner), db: Session = Depends(get_db)):
    heir = owner_heir(db, owner, heir_id)
    assigned = [
        str(value)
        for value in db.execute(
            select(heir_record_access.c.record_id).where(heir_record_access.c.heir_id == heir.id)
        ).scalars()
    ]
    values = {"name": heir.name, "record_ids": assigned}
    return render(request, "heir_form.html", form_context(db, owner, "edit", values, {}, heir))


@router.post("/heirs/{heir_id}/edit")
def edit_heir(
    heir_id: str,
    request: Request,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
    csrf_token: str = Form(""),
    name: str = Form(""),
    record_ids: list[str] = Form([]),
):
    verify_csrf(request, csrf_token)
    heir = owner_heir(db, owner, heir_id)
    name = name.strip()
    if error := validate_name(name):
        values = {"name": name, "record_ids": record_ids}
        context = form_context(db, owner, "edit", values, {"name": error}, heir)
        return render(request, "heir_form.html", context, status_code=400)
    ids = owned_record_ids(db, owner, record_ids)

    heir.name = name
    db.execute(delete(heir_record_access).where(heir_record_access.c.heir_id == heir.id))
    for record_id in ids:
        db.execute(heir_record_access.insert().values(heir_id=heir.id, record_id=record_id))
    audit.log(db, owner.id, "HEIR_UPDATED", {"heir_id": heir.id, "record_count": len(ids)})
    db.commit()
    security.flash(request, "Изменения сохранены")
    return redirect("/heirs")


@router.post("/heirs/{heir_id}/regenerate-key")
def regenerate_key(
    heir_id: str,
    request: Request,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
    csrf_token: str = Form(""),
):
    verify_csrf(request, csrf_token)
    heir = owner_heir(db, owner, heir_id, lock=True)  # против гонки с подачей запроса
    if has_active_request(db, heir.id):
        raise AppError("HEIR_HAS_ACTIVE_REQUEST")
    now = clock.now()
    revoked = db.execute(
        update(HeirKey)
        .where(HeirKey.heir_id == heir.id, HeirKey.revoked_at.is_(None))
        .values(revoked_at=now)
        .returning(HeirKey.id)
    ).scalars().all()
    for key_id in revoked:
        audit.log(
            db, owner.id, "HEIR_KEY_REVOKED", {"heir_id": heir.id, "heir_key_id": key_id, "reason": "regenerate"}
        )
    key = issue_key(db, owner.id, heir.id)
    db.commit()

    security.set_flash_key(request, heir.id, key)
    security.flash(request, "Ключ перевыпущен")
    return redirect(f"/heirs/{heir.id}/key")


@router.post("/heirs/{heir_id}/delete")
def delete_heir(
    heir_id: str,
    request: Request,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
    csrf_token: str = Form(""),
):
    verify_csrf(request, csrf_token)
    heir = owner_heir(db, owner, heir_id, lock=True)
    if has_active_request(db, heir.id):
        raise AppError("HEIR_HAS_ACTIVE_REQUEST")
    record_count = db.scalar(
        select(func.count()).select_from(heir_record_access).where(heir_record_access.c.heir_id == heir.id)
    )
    # Документы завершённых запросов, ещё не удалённые шагом E, удаляются вместе с наследником.
    stored_docs = list(
        db.execute(
            select(InheritanceRequest.id).where(
                InheritanceRequest.heir_id == heir.id, InheritanceRequest.doc_stored.is_(True)
            )
        ).scalars()
    )
    deleted_id = heir.id
    db.delete(heir)
    audit.log(db, owner.id, "HEIR_DELETED", {"heir_id": deleted_id, "record_count": record_count})
    db.commit()
    for request_id in stored_docs:
        storage.delete(storage.doc_path(request_id))
    security.flash(request, "Наследник удалён")
    return redirect("/heirs")
