"""Worker (3.2.6): проверка документов, письма E2 и E3, таймеры, очистка, heartbeat.

Принцип безопасного отказа (1.1.3): любой сбой может только задержать выдачу доступа.
Каждый запрос обрабатывается в отдельной транзакции; блокировки на время OCR не держатся.
"""

from __future__ import annotations

import logging
import signal
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from app import audit, clock, storage, tokens
from app.config import configure_logging, current_request_id, get_settings, load_settings_or_exit
from app.crypto import DecryptionError
from app.db import new_session
from app.email.backends import EmailSendError, send_email
from app.models import Heir, InheritanceRequest, SystemState, User
from app.ocr.prepare import DocumentUnreadable, prepare_image
from app.ocr.provider import get_provider
from app.routes import format_datetime
from app.rules import OwnerNames, check
from app.transitions import TransitionConflict, transition

logger = logging.getLogger("app.worker")

HEARTBEAT_KEY = "worker_heartbeat"
STEP_B_LIMIT = 50
STEP_C_LIMIT = 50
STEP_D_LIMIT = 100


@contextmanager
def request_context(request_id: uuid.UUID) -> Iterator[None]:
    """Ошибка обработки запроса пишется в лог ERROR с request_id и не прерывает цикл."""
    token = current_request_id.set(str(request_id))
    try:
        yield
    except Exception:
        logger.exception("Ошибка обработки запроса")
    finally:
        current_request_id.reset(token)


def _load(request_id: uuid.UUID) -> tuple[InheritanceRequest, Heir, User] | None:
    with new_session() as db:
        row = db.execute(
            select(InheritanceRequest, Heir, User)
            .join(Heir, Heir.id == InheritanceRequest.heir_id)
            .join(User, User.id == Heir.user_id)
            .where(InheritanceRequest.id == request_id)
        ).first()
    return tuple(row) if row else None


def _owner_email_context(item: InheritanceRequest, heir: Heir, waiting_until) -> dict[str, Any]:
    settings = get_settings()
    return {
        "heir_name": heir.name,
        "created_at": format_datetime(item.created_at),
        "waiting_until": format_datetime(waiting_until),
        "cancel_url": tokens.cancel_url(item.id),
        "requests_url": f"{settings.base_url}/requests",
    }


# --- Heartbeat ---------------------------------------------------------------------------


def write_heartbeat() -> None:
    now = clock.now()
    with new_session() as db:
        stmt = insert(SystemState).values(key=HEARTBEAT_KEY, value=now.isoformat(), updated_at=now)
        stmt = stmt.on_conflict_do_update(
            index_elements=[SystemState.key],
            set_={"value": stmt.excluded.value, "updated_at": stmt.excluded.updated_at},
        )
        db.execute(stmt)
        db.commit()


# --- Шаг A: проверка документа -------------------------------------------------------------


def step_a_check_document() -> None:
    """Не более одного документа за цикл: самый ранний запрос PENDING_REVIEW."""
    with new_session() as db:
        request_id = db.execute(
            select(InheritanceRequest.id)
            .where(InheritanceRequest.status == "PENDING_REVIEW")
            .order_by(InheritanceRequest.created_at, InheritanceRequest.id)
            .limit(1)
        ).scalar_one_or_none()
    if request_id is not None:
        with request_context(request_id):
            check_document(request_id)


def check_document(request_id: uuid.UUID) -> None:
    settings = get_settings()
    loaded = _load(request_id)
    if loaded is None or loaded[0].status != "PENDING_REVIEW":
        return
    item, _, owner = loaded
    owner_names = OwnerNames(owner.last_name, owner.first_name, owner.middle_name)
    last_login_date = owner.last_login_at.astimezone(settings.display_tz).date() if owner.last_login_at else None

    try:
        data = storage.load_document(request_id)
        image = prepare_image(data, item.doc_mime_type)
    except (FileNotFoundError, DecryptionError, DocumentUnreadable) as exc:
        logger.warning("Документ нечитаем: %s", type(exc).__name__)
        reject(request_id, {"accepted": False, "reasons": ["DOCUMENT_UNREADABLE"]})
        return
    finally:
        data = None  # расшифрованный документ не держится в памяти дольше необходимого

    try:
        text = get_provider().recognize(image)
    except Exception as exc:
        register_ocr_failure(request_id, exc)
        return

    today = clock.now().astimezone(settings.display_tz).date()
    result = check(text, owner_names, today, last_login_date)
    text = None  # распознанный текст существует только в памяти на время проверки
    if result.accepted:
        with new_session() as db:
            try:
                transition(db, request_id, {"PENDING_REVIEW"}, "DOCUMENT_ACCEPTED", check_result=result.to_dict())
            except TransitionConflict:
                db.rollback()
                logger.info("Статус запроса изменился во время проверки, пропуск")
                return
            db.commit()
        logger.info("Документ принят")
    else:
        reject(request_id, result.to_dict())


def reject(request_id: uuid.UUID, check_result: dict[str, Any]) -> bool:
    with new_session() as db:
        try:
            transition(
                db, request_id, {"PENDING_REVIEW"}, "REJECTED", check_result=check_result, rejected_at=clock.now()
            )
        except TransitionConflict:
            db.rollback()
            logger.info("Статус запроса изменился во время проверки, пропуск")
            return False
        db.commit()
    logger.info("Документ отклонён: %s", ", ".join(check_result["reasons"]))
    return True


def register_ocr_failure(request_id: uuid.UUID, exc: Exception) -> None:
    """Попытка OCR фиксируется; после OCR_MAX_ATTEMPTS — REJECTED с OCR_UNAVAILABLE."""
    settings = get_settings()
    now = clock.now()
    with new_session() as db:
        attempts = db.execute(
            update(InheritanceRequest)
            .where(InheritanceRequest.id == request_id, InheritanceRequest.status == "PENDING_REVIEW")
            .values(ocr_attempts=InheritanceRequest.ocr_attempts + 1, updated_at=now)
            .returning(InheritanceRequest.ocr_attempts)
        ).scalar_one_or_none()
        if attempts is None:
            db.rollback()
            return
        logger.error(
            "Ошибка OCR, попытка %s из %s: %s", attempts, settings.OCR_MAX_ATTEMPTS, type(exc).__name__
        )
        rejected = attempts >= settings.OCR_MAX_ATTEMPTS
        if rejected:
            check_result = {"accepted": False, "reasons": ["OCR_UNAVAILABLE"], "attempts": attempts}
            transition(db, request_id, {"PENDING_REVIEW"}, "REJECTED", check_result=check_result, rejected_at=now)
        db.commit()


# --- Шаг B: уведомление Владельца ------------------------------------------------------------


def step_b_notify_owners() -> None:
    with new_session() as db:
        ids = db.execute(
            select(InheritanceRequest.id)
            .where(InheritanceRequest.status == "DOCUMENT_ACCEPTED")
            .order_by(InheritanceRequest.created_at)
            .limit(STEP_B_LIMIT)
        ).scalars().all()
    for request_id in ids:
        with request_context(request_id):
            notify_owner(request_id)


def notify_owner(request_id: uuid.UUID) -> None:
    """E2; только после приёма письма SMTP-сервером начинается отсчёт периода ожидания."""
    period = timedelta(seconds=get_settings().WAITING_PERIOD_SECONDS)
    loaded = _load(request_id)
    if loaded is None or loaded[0].status != "DOCUMENT_ACCEPTED":
        return
    item, heir, owner = loaded
    # В письме указан срок от момента до отправки; фактический срок отсчитывается после
    # приёма письма и потому не раньше указанного.
    context = _owner_email_context(item, heir, clock.now() + period)
    try:
        send_email("E2", owner.email, **context)
    except EmailSendError as exc:
        logger.error("Письмо E2 не отправлено (%s), повтор на следующем цикле", exc)
        return

    now = clock.now()
    with new_session() as db:
        try:
            transition(
                db, request_id, {"DOCUMENT_ACCEPTED"}, "WAITING_CANCELLATION", notified_at=now, waiting_until=now + period
            )
        except TransitionConflict:
            db.rollback()
            logger.info("Статус запроса изменился после отправки E2, пропуск")
            return
        audit.log(db, owner.id, "OWNER_NOTIFIED", {}, request_id=request_id)
        db.commit()
    logger.info("Владелец уведомлён, начат период ожидания")


# --- Шаг C: напоминание ------------------------------------------------------------------------


def step_c_send_reminders() -> None:
    now = clock.now()
    reminder = timedelta(seconds=get_settings().REMINDER_BEFORE_SECONDS)
    with new_session() as db:
        ids = db.execute(
            select(InheritanceRequest.id)
            .where(
                InheritanceRequest.status == "WAITING_CANCELLATION",
                InheritanceRequest.reminder_sent_at.is_(None),
                InheritanceRequest.waiting_until <= now + reminder,  # waiting_until − REMINDER ≤ now
            )
            .order_by(InheritanceRequest.waiting_until)
            .limit(STEP_C_LIMIT)
        ).scalars().all()
    for request_id in ids:
        with request_context(request_id):
            send_reminder(request_id)


def send_reminder(request_id: uuid.UUID) -> None:
    reminder = timedelta(seconds=get_settings().REMINDER_BEFORE_SECONDS)
    loaded = _load(request_id)
    if loaded is None or loaded[0].status != "WAITING_CANCELLATION" or loaded[0].reminder_sent_at is not None:
        return
    item, heir, owner = loaded
    until = max(item.waiting_until, clock.now() + reminder)  # актуальная дата окончания ожидания
    context = _owner_email_context(item, heir, until)
    try:
        send_email("E3", owner.email, date=context["waiting_until"], **context)
    except EmailSendError as exc:
        logger.error("Напоминание E3 не отправлено (%s), повтор на следующем цикле", exc)
        return

    now = clock.now()
    with new_session() as db:
        # Продление гарантирует Владельцу не менее REMINDER_BEFORE_SECONDS после напоминания.
        waiting_until = db.execute(
            update(InheritanceRequest)
            .where(
                InheritanceRequest.id == request_id,
                InheritanceRequest.status == "WAITING_CANCELLATION",
                InheritanceRequest.reminder_sent_at.is_(None),
            )
            .values(
                reminder_sent_at=now,
                waiting_until=func.greatest(InheritanceRequest.waiting_until, now + reminder),
                updated_at=now,
            )
            .returning(InheritanceRequest.waiting_until)
        ).scalar_one_or_none()
        if waiting_until is None:
            db.rollback()
            return
        audit.log(db, owner.id, "REMINDER_SENT", {"waiting_until": waiting_until}, request_id=request_id)
        db.commit()
    logger.info("Напоминание отправлено")


# --- Шаг D: выдача -------------------------------------------------------------------------------


def step_d_release_due() -> None:
    now = clock.now()
    with new_session() as db:
        ids = db.execute(
            select(InheritanceRequest.id)
            .where(
                InheritanceRequest.status == "WAITING_CANCELLATION",
                InheritanceRequest.waiting_until <= now,
                InheritanceRequest.reminder_sent_at.is_not(None),
            )
            .order_by(InheritanceRequest.waiting_until)
            .limit(STEP_D_LIMIT)
        ).scalars().all()
    for request_id in ids:
        with request_context(request_id):
            release(request_id)


def release(request_id: uuid.UUID) -> None:
    now = clock.now()
    with new_session() as db:
        # Условия выдачи перепроверяются под блокировкой строки: продление срока или отмена,
        # зафиксированные после выборки, исключают запрос из выдачи.
        due = db.execute(
            select(InheritanceRequest.id)
            .where(
                InheritanceRequest.id == request_id,
                InheritanceRequest.status == "WAITING_CANCELLATION",
                InheritanceRequest.waiting_until <= now,
                InheritanceRequest.reminder_sent_at.is_not(None),
            )
            .with_for_update()
        ).scalar_one_or_none()
        if due is None:
            db.rollback()
            return
        try:
            transition(db, request_id, {"WAITING_CANCELLATION"}, "RELEASED", released_at=now)
        except TransitionConflict:
            db.rollback()
            return
        db.commit()
    logger.info("Доступ выдан")


# --- Шаг E: очистка документов --------------------------------------------------------------------


def step_e_cleanup_documents() -> None:
    with new_session() as db:
        ids = db.execute(
            select(InheritanceRequest.id).where(
                InheritanceRequest.doc_stored.is_(True), InheritanceRequest.status != "PENDING_REVIEW"
            )
        ).scalars().all()
    for request_id in ids:
        with request_context(request_id):
            storage.delete(storage.doc_path(request_id))  # отсутствие файла — не ошибка
            with new_session() as db:
                db.execute(
                    update(InheritanceRequest)
                    .where(InheritanceRequest.id == request_id, InheritanceRequest.doc_stored.is_(True))
                    .values(doc_stored=False, updated_at=clock.now())
                )
                db.commit()


# --- Цикл -----------------------------------------------------------------------------------------

STEPS = (
    write_heartbeat,
    step_a_check_document,
    step_b_notify_owners,
    step_c_send_reminders,
    step_d_release_due,
    step_e_cleanup_documents,
)


def run_tick() -> None:
    for step in STEPS:
        try:
            step()
        except Exception:
            logger.exception("Ошибка шага %s", step.__name__)


def main() -> None:
    settings = load_settings_or_exit()
    configure_logging()

    try:
        get_provider()  # модель OCR загружается один раз при старте
    except Exception:
        logger.exception("Не удалось инициализировать OCR (%s)", settings.OCR_PROVIDER)
        raise SystemExit(1) from None

    stop = threading.Event()

    def request_stop(signum, frame) -> None:
        logger.info("Получен сигнал %s, завершение после текущего цикла", signum)
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    logger.info("Worker запущен")
    while not stop.is_set():
        run_tick()
        stop.wait(settings.WORKER_POLL_SECONDS)
    logger.info("Worker остановлен")


if __name__ == "__main__":
    main()
