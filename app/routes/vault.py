"""Сейф Владельца: текстовые и файловые записи, лимиты (FR-03, FR-04, FR-05, сценарий А 2.2.2)."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import audit, clock, crypto, security, storage
from app.config import get_settings
from app.db import get_db, new_session
from app.models import Heir, Record, User, heir_record_access
from app.routes import (
    MB,
    RECORD_EXTENSIONS,
    RECORD_TYPES_LABEL,
    AppError,
    BodyTooLarge,
    attachment_headers,
    clean_filename,
    detect_file_type,
    field_error,
    file_too_large,
    form_file,
    form_str,
    format_mb,
    limit_error,
    parse_id,
    read_form_limited,
    redirect,
    render,
    require_owner,
    unsupported_file,
    verify_csrf,
)

router = APIRouter()
logger = logging.getLogger("app.vault")


def owner_record(db: Session, owner: User, record_id: str) -> Record:
    """Запись Владельца; чужая или несуществующая — 404."""
    record = db.execute(
        select(Record).where(Record.id == parse_id(record_id), Record.user_id == owner.id)
    ).scalar_one_or_none()
    if record is None:
        raise AppError("NOT_FOUND")
    return record


def decrypt_text(record: Record) -> str:
    crypto.check_key_version(record.key_version)
    return crypto.decrypt(record.ciphertext, crypto.record_aad(record.id)).decode("utf-8")


def usage(db: Session, owner_id: uuid.UUID) -> dict[str, int]:
    records = db.scalar(select(func.count()).select_from(Record).where(Record.user_id == owner_id))
    storage_bytes = db.scalar(
        select(func.coalesce(func.sum(Record.size_bytes), 0)).where(
            Record.user_id == owner_id, Record.type == "file"
        )
    )
    heirs = db.scalar(select(func.count()).select_from(Heir).where(Heir.user_id == owner_id))
    return {"records": records, "storage_bytes": int(storage_bytes), "heirs": heirs}


def heir_counts(db: Session, record_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not record_ids:
        return {}
    rows = db.execute(
        select(heir_record_access.c.record_id, func.count())
        .where(heir_record_access.c.record_id.in_(record_ids))
        .group_by(heir_record_access.c.record_id)
    )
    return dict(rows.tuples().all())


def validate_title(title: str) -> dict[str, str] | None:
    if not 1 <= len(title) <= 200:
        return field_error("VALIDATION_ERROR", "Название: от 1 до 200 символов")
    return None


def validate_content(content: str) -> dict[str, str] | None:
    limit = get_settings().MAX_TEXT_CHARS
    if len(content) > limit:
        return field_error("VALIDATION_ERROR", f"Текст должен быть не длиннее {limit:,} символов".replace(",", " "))
    return None


def lock_owner_and_check_record_limit(db: Session, owner_id: uuid.UUID) -> None:
    # Блокировка строки Владельца упорядочивает параллельные проверки лимитов.
    db.execute(select(User.id).where(User.id == owner_id).with_for_update())
    limit = get_settings().MAX_RECORDS
    count = db.scalar(select(func.count()).select_from(Record).where(Record.user_id == owner_id))
    if count >= limit:
        raise limit_error("записи", count, limit)


# --- Сейф -----------------------------------------------------------------------------------


@router.get("/vault")
def vault(request: Request, owner: User = Depends(require_owner), db: Session = Depends(get_db)):
    records = list(
        db.execute(
            select(Record)
            .where(Record.user_id == owner.id)
            .order_by(Record.updated_at.desc(), Record.created_at.desc())
        ).scalars()
    )
    return render(
        request,
        "vault.html",
        {
            "records": records,
            "heir_counts": heir_counts(db, [r.id for r in records]),
            "usage": usage(db, owner.id),
        },
    )


@router.get("/records/new")
def new_record_form(request: Request, type: str = "text", owner: User = Depends(require_owner)):
    if type not in ("text", "file"):
        raise AppError("NOT_FOUND")
    return render(
        request, "record_form.html", {"mode": "new", "record_type": type, "values": {}, "errors": {}}
    )


@router.post("/records")
async def create_record(request: Request, owner: User = Depends(require_owner)):
    settings = get_settings()
    try:
        form = await read_form_limited(request, settings.MAX_FILE_MB * MB)
    except BodyTooLarge:
        raise file_too_large(settings.MAX_FILE_MB) from None
    try:
        verify_csrf(request, form_str(form, "csrf_token"))
        record_type = form_str(form, "type")
        title = form_str(form, "title").strip()
        if record_type == "text":
            content = form_str(form, "content").replace("\r\n", "\n")
            return await run_in_threadpool(create_text_record, request, owner.id, title, content)
        if record_type == "file":
            upload = form_file(form, "file")
            data = await upload.read() if upload else None
            filename = clean_filename(upload.filename) if upload else ""
            return await run_in_threadpool(
                create_file_record, request, owner.id, title, filename, data
            )
        raise AppError("VALIDATION_ERROR")
    finally:
        await form.close()


def create_text_record(request: Request, owner_id: uuid.UUID, title: str, content: str):
    errors = {}
    if error := validate_title(title):
        errors["title"] = error
    if error := validate_content(content):
        errors["content"] = error
    if errors:
        context = {"mode": "new", "record_type": "text", "values": {"title": title, "content": content}, "errors": errors}
        return render(request, "record_form.html", context, status_code=400, no_store=True)

    with new_session() as db:
        lock_owner_and_check_record_limit(db, owner_id)
        now = clock.now()
        record_id = uuid.uuid4()
        plaintext = content.encode("utf-8")
        db.add(
            Record(
                id=record_id,
                user_id=owner_id,
                type="text",
                title=title,
                ciphertext=crypto.encrypt(plaintext, crypto.record_aad(record_id)),
                size_bytes=len(plaintext),
                key_version=crypto.KEY_VERSION,
                created_at=now,
                updated_at=now,
            )
        )
        audit.log(db, owner_id, "RECORD_CREATED", {"record_id": record_id, "type": "text"})
        db.commit()
    security.flash(request, "Запись сохранена")
    return redirect("/vault")


def create_file_record(
    request: Request, owner_id: uuid.UUID, title: str, filename: str, data: bytes | None
):
    settings = get_settings()
    errors = {}
    if error := validate_title(title):
        errors["title"] = error
    if data is None:
        errors["file"] = field_error("VALIDATION_ERROR", "Выберите файл")
    if errors:
        context = {"mode": "new", "record_type": "file", "values": {"title": title}, "errors": errors}
        return render(request, "record_form.html", context, status_code=400)

    mime_type = detect_file_type(filename, data, RECORD_EXTENSIONS)
    if mime_type is None:
        raise unsupported_file(RECORD_TYPES_LABEL)
    if len(data) > settings.MAX_FILE_MB * MB:
        raise file_too_large(settings.MAX_FILE_MB)

    with new_session() as db:
        lock_owner_and_check_record_limit(db, owner_id)
        used = usage(db, owner_id)["storage_bytes"]
        if used + len(data) > settings.MAX_STORAGE_MB * MB:
            raise limit_error("объём файлов", f"{format_mb(used)}", f"{settings.MAX_STORAGE_MB} МБ")

        now = clock.now()
        record_id = uuid.uuid4()
        storage.save_record_file(record_id, data)
        try:
            db.add(
                Record(
                    id=record_id,
                    user_id=owner_id,
                    type="file",
                    title=title,
                    file_name=filename,
                    mime_type=mime_type,
                    size_bytes=len(data),
                    key_version=crypto.KEY_VERSION,
                    created_at=now,
                    updated_at=now,
                )
            )
            audit.log(db, owner_id, "RECORD_CREATED", {"record_id": record_id, "type": "file"})
            db.commit()
        except BaseException:
            db.rollback()
            storage.delete(storage.record_path(record_id))  # при ошибке транзакции файл удаляется
            raise
    security.flash(request, "Запись сохранена")
    return redirect("/vault")


# --- Просмотр, редактирование, удаление, скачивание --------------------------------------------


@router.get("/records/{record_id}")
def view_record(
    record_id: str, request: Request, owner: User = Depends(require_owner), db: Session = Depends(get_db)
):
    record = owner_record(db, owner, record_id)
    content = decrypt_text(record) if record.type == "text" else None
    context = {"record": record, "content": content, "heir_count": heir_counts(db, [record.id]).get(record.id, 0)}
    return render(request, "record_view.html", context, no_store=True)


@router.get("/records/{record_id}/edit")
def edit_record_form(
    record_id: str, request: Request, owner: User = Depends(require_owner), db: Session = Depends(get_db)
):
    record = owner_record(db, owner, record_id)
    values = {"title": record.title}
    if record.type == "text":
        values["content"] = decrypt_text(record)
    context = {"mode": "edit", "record": record, "record_type": record.type, "values": values, "errors": {}}
    return render(request, "record_form.html", context, no_store=True)


@router.post("/records/{record_id}/edit")
def edit_record(
    record_id: str,
    request: Request,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
    csrf_token: str = Form(""),
    title: str = Form(""),
    content: str = Form(""),
):
    verify_csrf(request, csrf_token)
    record = owner_record(db, owner, record_id)
    title = title.strip()
    content = content.replace("\r\n", "\n")
    errors = {}
    if error := validate_title(title):
        errors["title"] = error
    if record.type == "text" and (error := validate_content(content)):
        errors["content"] = error
    if errors:
        values = {"title": title, "content": content}
        context = {"mode": "edit", "record": record, "record_type": record.type, "values": values, "errors": errors}
        return render(request, "record_form.html", context, status_code=400, no_store=True)

    record.title = title
    if record.type == "text":
        plaintext = content.encode("utf-8")
        record.ciphertext = crypto.encrypt(plaintext, crypto.record_aad(record.id))
        record.size_bytes = len(plaintext)
        record.key_version = crypto.KEY_VERSION
    record.updated_at = clock.now()
    audit.log(db, owner.id, "RECORD_UPDATED", {"record_id": record.id, "type": record.type})
    db.commit()
    security.flash(request, "Изменения сохранены")
    return redirect(f"/records/{record.id}")


@router.post("/records/{record_id}/delete")
def delete_record(
    record_id: str,
    request: Request,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
    csrf_token: str = Form(""),
):
    verify_csrf(request, csrf_token)
    record = owner_record(db, owner, record_id)
    deleted_id, deleted_type = record.id, record.type
    db.delete(record)
    audit.log(db, owner.id, "RECORD_DELETED", {"record_id": deleted_id, "type": deleted_type})
    db.commit()
    if deleted_type == "file":
        storage.delete(storage.record_path(deleted_id))  # после фиксации транзакции
    security.flash(request, "Запись удалена")
    return redirect("/vault")


@router.get("/records/{record_id}/download")
def download_record(
    record_id: str, request: Request, owner: User = Depends(require_owner), db: Session = Depends(get_db)
):
    record = owner_record(db, owner, record_id)
    if record.type != "file":
        raise AppError("NOT_FOUND")
    crypto.check_key_version(record.key_version)
    try:
        data = storage.load_record_file(record.id)
    except FileNotFoundError:
        logger.error("Файл записи отсутствует на диске: %s", record.id)
        raise AppError("INTERNAL_ERROR") from None
    return Response(content=data, media_type=record.mime_type, headers=attachment_headers(record.file_name))
