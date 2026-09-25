"""Портал наследника: вход по ключу, подача запроса, состояния портала, выданные данные
(FR-08, FR-09, FR-15, сценарии В и Ж 2.2.4, 2.2.8, 3.3.3)."""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import audit, clock, crypto, security, storage
from app.config import get_settings
from app.db import get_db, new_session
from app.models import ACTIVE_STATUSES, Heir, HeirKey, InheritanceRequest, Record, heir_record_access
from app.routes import (
    DOCUMENT_EXTENSIONS,
    DOCUMENT_TYPES_LABEL,
    MB,
    AppError,
    BodyTooLarge,
    HeirContext,
    HeirLoginRequired,
    attachment_headers,
    clean_filename,
    detect_file_type,
    field_error,
    file_too_large,
    form_file,
    form_str,
    normalize_email,
    parse_id,
    read_form_limited,
    redirect,
    render,
    require_heir,
    unsupported_file,
    verify_csrf,
)
from app.rules import PHOTO_ADVICE, REASON_MESSAGES

router = APIRouter()
logger = logging.getLogger("app.heir")


# --- Вход и выход ------------------------------------------------------------------------


@router.get("/heir")
def heir_login_form(request: Request):
    return render(request, "heir_login.html")


@router.post("/heir/login")
@security.limiter.limit(security.HEIR_LOGIN_RATE_LIMIT)
def heir_login(request: Request, db: Session = Depends(get_db), csrf_token: str = Form(""), key: str = Form("")):
    verify_csrf(request, csrf_token)
    normalized = crypto.normalize_heir_key(key)  # неверный формат — без обращения к БД
    heir_key = None
    if normalized is not None:
        heir_key = db.execute(
            select(HeirKey).where(
                HeirKey.key_hash == crypto.hash_heir_key(normalized), HeirKey.revoked_at.is_(None)
            )
        ).scalar_one_or_none()
    if heir_key is None:
        alert = {"code": "INVALID_KEY", "message": "Ключ не найден или недействителен"}
        return render(request, "heir_login.html", {"alert": alert}, status_code=401)
    security.login_heir(request, heir_key.id)
    return redirect("/heir/portal")


@router.post("/heir/logout")
def heir_logout(request: Request, heir: HeirContext = Depends(require_heir), csrf_token: str = Form("")):
    verify_csrf(request, csrf_token)
    security.clear_heir_session(request)
    return redirect("/heir")


# --- Состояние портала --------------------------------------------------------------------


def key_requests(db: Session, heir_key_id: uuid.UUID) -> list[InheritanceRequest]:
    """Запросы по текущему ключу, новые первыми."""
    return list(
        db.execute(
            select(InheritanceRequest)
            .where(InheritanceRequest.heir_key_id == heir_key_id)
            .order_by(InheritanceRequest.created_at.desc())
        ).scalars()
    )


def counts_toward_daily_limit(item: InheritanceRequest) -> bool:
    """Отклонённые с причиной OCR_UNAVAILABLE в суточном лимите не учитываются (3.1.4)."""
    reasons = (item.check_result or {}).get("reasons") or []
    return not (item.status == "REJECTED" and "OCR_UNAVAILABLE" in reasons)


def daily_request_count(db: Session, heir_key_id: uuid.UUID) -> int:
    since = clock.now() - timedelta(hours=24)
    recent = db.execute(
        select(InheritanceRequest).where(
            InheritanceRequest.heir_key_id == heir_key_id, InheritanceRequest.created_at > since
        )
    ).scalars()
    return sum(1 for item in recent if counts_toward_daily_limit(item))


def released_items(db: Session, heir_id: uuid.UUID) -> list[dict]:
    """Только записи, назначенные наследнику (FR-15)."""
    records = db.execute(
        select(Record)
        .join(heir_record_access, heir_record_access.c.record_id == Record.id)
        .where(heir_record_access.c.heir_id == heir_id)
        .order_by(Record.created_at)
    ).scalars()
    items = []
    for record in records:
        content = None
        if record.type == "text":
            crypto.check_key_version(record.key_version)
            content = crypto.decrypt(record.ciphertext, crypto.record_aad(record.id)).decode("utf-8")
        items.append({"record": record, "content": content})
    return items


def portal_context(db: Session, context: HeirContext) -> dict:
    requests_ = key_requests(db, context.key.id)
    released = next((item for item in requests_ if item.status == "RELEASED"), None)
    if released is not None:
        return {"state": "released", "released": released, "items": released_items(db, context.heir.id)}
    latest = requests_[0] if requests_ else None
    status = latest.status if latest else None
    if status == "WAITING_CANCELLATION":
        state = "waiting"
    elif status in ("PENDING_REVIEW", "DOCUMENT_ACCEPTED"):
        state = "pending"
    elif status == "REJECTED":
        state = "rejected"
    else:
        state = "none"
    reasons = []
    if state == "rejected":
        codes = (latest.check_result or {}).get("reasons") or []
        reasons = [{"code": code, "message": REASON_MESSAGES.get(code, code)} for code in codes]
    limit_reached = daily_request_count(db, context.key.id) >= get_settings().HEIR_REQUESTS_PER_DAY
    return {"state": state, "latest": latest, "reasons": reasons, "limit_reached": limit_reached}


def render_portal(request: Request, db: Session, context: HeirContext, errors=None, values=None, status_code=200):
    data = portal_context(db, context)
    if data["state"] == "released":
        # Каждое открытие страницы с выданными данными пишет событие HEIR_DATA_VIEWED.
        audit.log(db, context.heir.user_id, "HEIR_DATA_VIEWED", {"heir_id": context.heir.id}, request_id=data["released"].id)
        db.commit()
    data.update({"advice": PHOTO_ADVICE, "errors": errors or {}, "values": values or {}})
    return render(request, "heir_portal.html", data, status_code=status_code, no_store=True)


@router.get("/heir/portal")
def portal(request: Request, heir: HeirContext = Depends(require_heir), db: Session = Depends(get_db)):
    return render_portal(request, db, heir)


# --- Подача запроса -----------------------------------------------------------------------


@router.post("/heir/request")
async def submit_request(request: Request, heir: HeirContext = Depends(require_heir)):
    settings = get_settings()
    try:
        form = await read_form_limited(request, settings.MAX_DOC_MB * MB)
    except BodyTooLarge:
        raise file_too_large(settings.MAX_DOC_MB) from None
    try:
        verify_csrf(request, form_str(form, "csrf_token"))
        upload = form_file(form, "document")
        data = await upload.read() if upload else None
        filename = clean_filename(upload.filename) if upload else ""
        contact = form_str(form, "contact_email")
        return await run_in_threadpool(create_request, request, heir, data, filename, contact)
    finally:
        await form.close()


def create_request(request: Request, context: HeirContext, data: bytes | None, filename: str, contact_raw: str):
    settings = get_settings()
    with new_session() as db:
        # Блокировка строки наследника упорядочивает подачу с отменой, перевыпуском и удалением.
        heir = db.execute(select(Heir).where(Heir.id == context.heir.id).with_for_update()).scalar_one_or_none()
        key = db.get(HeirKey, context.key.id)
        if heir is None or key is None or key.revoked_at is not None:
            security.clear_heir_session(request)
            raise HeirLoginRequired(expired=True)
        active = db.execute(
            select(InheritanceRequest.id).where(
                InheritanceRequest.heir_id == heir.id, InheritanceRequest.status.in_(ACTIVE_STATUSES)
            )
        ).first()
        if active is not None:
            raise AppError("ACTIVE_REQUEST_EXISTS")
        released = db.execute(
            select(InheritanceRequest.id).where(
                InheritanceRequest.heir_key_id == key.id, InheritanceRequest.status == "RELEASED"
            )
        ).first()
        if released is not None:
            raise AppError("ALREADY_RELEASED")
        if daily_request_count(db, key.id) >= settings.HEIR_REQUESTS_PER_DAY:
            raise AppError("REQUEST_LIMIT_EXCEEDED")

        errors = {}
        if data is None:
            errors["document"] = field_error("VALIDATION_ERROR", "Выберите файл документа")
        contact_email = None
        if contact_raw.strip():
            contact_email = normalize_email(contact_raw)
            if contact_email is None:
                errors["contact_email"] = field_error(
                    "VALIDATION_ERROR", "Введите корректный email или оставьте поле пустым"
                )
        if errors:
            return render_portal(request, db, context, errors, {"contact_email": contact_raw.strip()}, 400)

        mime_type = detect_file_type(filename, data, DOCUMENT_EXTENSIONS)
        if mime_type is None:
            raise unsupported_file(DOCUMENT_TYPES_LABEL)
        if len(data) > settings.MAX_DOC_MB * MB:
            raise file_too_large(settings.MAX_DOC_MB)

        now = clock.now()
        request_id = uuid.uuid4()
        storage.save_document(request_id, data)  # AAD doc:{request_id}
        try:
            db.add(
                InheritanceRequest(
                    id=request_id,
                    heir_id=heir.id,
                    heir_key_id=key.id,
                    status="PENDING_REVIEW",
                    heir_contact_email=contact_email,
                    doc_mime_type=mime_type,
                    doc_stored=True,
                    ocr_attempts=0,
                    created_at=now,
                    updated_at=now,
                )
            )
            db.flush()
            audit.log(db, heir.user_id, "REQUEST_CREATED", {"heir_id": heir.id}, request_id=request_id)
            db.commit()
        except IntegrityError:
            db.rollback()
            storage.delete(storage.doc_path(request_id))
            raise AppError("ACTIVE_REQUEST_EXISTS") from None
        except BaseException:
            db.rollback()
            storage.delete(storage.doc_path(request_id))
            raise
        logger.info("Создан наследственный запрос", extra={"request_id": str(request_id)})
    return redirect("/heir/portal")


# --- Скачивание выданных файлов -----------------------------------------------------------


@router.get("/heir/files/{record_id}")
def heir_file(record_id: str, request: Request, heir: HeirContext = Depends(require_heir), db: Session = Depends(get_db)):
    """Файл выдаётся, только если по текущему ключу есть RELEASED и запись назначена наследнику."""
    released = db.execute(
        select(InheritanceRequest.id).where(
            InheritanceRequest.heir_key_id == heir.key.id, InheritanceRequest.status == "RELEASED"
        )
    ).scalars().first()
    record = db.execute(
        select(Record)
        .join(heir_record_access, heir_record_access.c.record_id == Record.id)
        .where(Record.id == parse_id(record_id), heir_record_access.c.heir_id == heir.heir.id)
    ).scalar_one_or_none()
    if released is None or record is None or record.type != "file":
        raise AppError("NOT_FOUND")
    crypto.check_key_version(record.key_version)
    try:
        data = storage.load_record_file(record.id)
    except FileNotFoundError:
        logger.error("Файл записи отсутствует на диске: %s", record.id)
        raise AppError("INTERNAL_ERROR") from None
    audit.log(
        db, heir.heir.user_id, "HEIR_FILE_DOWNLOADED", {"heir_id": heir.heir.id, "record_id": record.id}, request_id=released
    )
    db.commit()
    return Response(content=data, media_type=record.mime_type, headers=attachment_headers(record.file_name))
