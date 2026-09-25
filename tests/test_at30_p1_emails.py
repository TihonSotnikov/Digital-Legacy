"""AT-30: письма P1 (E4–E9)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app import clock, rules, worker
from app.routes import format_datetime
from tests.helpers import (
    add_heir,
    cancel_path,
    certificate_text,
    csrf_from,
    emails,
    heir_login,
    only_request,
    page_csrf,
    signup,
    submit_document,
)

OWNER = "owner@example.com"
HEIR = "heir@example.com"


def start(make_client, mail, contact: str = HEIR):
    owner = signup(make_client(), mail)
    _, key = add_heir(owner, "Пётр")
    heir = make_client()
    heir_login(heir, key)
    assert submit_document(heir, contact=contact).status_code == 303
    return owner, heir


def release_flow(db, settings):
    clock.set_now(only_request(db).waiting_until - timedelta(seconds=settings.REMINDER_BEFORE_SECONDS))
    worker.run_tick()
    clock.set_now(only_request(db).waiting_until + timedelta(seconds=1))
    worker.run_tick()


def test_at30_rejection_sends_e4_to_owner_and_e6_to_heir(make_client, mail, db, ocr):
    start(make_client, mail)
    ocr.push(certificate_text(last="Петрова"))
    worker.run_tick()
    request = only_request(db)
    assert request.status == "REJECTED"

    (e4,) = emails(mail, "E4")
    assert e4.to == OWNER and e4.subject == "Наследственный запрос отклонён"
    assert "Пётр" in e4.body and format_datetime(request.created_at) in e4.body
    assert "перевыпустить ключ" in e4.body and "http://testserver/heirs" in e4.body

    (e6,) = emails(mail, "E6")
    assert e6.to == HEIR and e6.subject == "Документ не прошёл проверку"
    assert rules.REASON_MESSAGES["NAME_MISMATCH"] in e6.body
    assert "http://testserver/heir" in e6.body
    assert {m.email_id for m in mail.outbox} - {"E1"} == {"E4", "E6"}  # E1 — письмо регистрации


def test_at30_ocr_unavailable_rejection_also_notifies(make_client, mail, db, ocr, settings):
    start(make_client, mail)
    ocr.push(*[RuntimeError("сбой")] * settings.OCR_MAX_ATTEMPTS)
    for _ in range(settings.OCR_MAX_ATTEMPTS):
        worker.run_tick()
    assert only_request(db).status == "REJECTED"
    assert len(emails(mail, "E4", OWNER)) == 1
    (e6,) = emails(mail, "E6", HEIR)
    assert rules.REASON_MESSAGES["OCR_UNAVAILABLE"] in e6.body


def test_at30_waiting_sends_e7_and_release_sends_e5_and_e8(make_client, mail, db, ocr, settings):
    start(make_client, mail)
    ocr.push(certificate_text())
    worker.run_tick()
    request = only_request(db)
    assert request.status == "WAITING_CANCELLATION"
    (e7,) = emails(mail, "E7")
    assert e7.to == HEIR and e7.subject == "Документ принят, идёт период ожидания"
    assert format_datetime(request.waiting_until) in e7.body
    assert emails(mail, "E5") == [] and emails(mail, "E8") == []

    release_flow(db, settings)
    request = only_request(db)
    assert request.status == "RELEASED"
    (e5,) = emails(mail, "E5")
    assert e5.to == OWNER and e5.subject == "Доступ к вашим данным открыт наследнику"
    assert "Пётр" in e5.body and format_datetime(request.released_at) in e5.body
    (e8,) = emails(mail, "E8")
    assert e8.to == HEIR and e8.subject == "Доступ к данным открыт" and "http://testserver/heir" in e8.body

    for _ in range(3):
        worker.run_tick()
    assert [m.email_id for m in mail.outbox].count("E5") == 1 and [m.email_id for m in mail.outbox].count("E8") == 1


@pytest.mark.parametrize("via", ["cabinet", "link"])
def test_at30_cancellation_sends_e9_to_heir(make_client, mail, db, ocr, via):
    owner, _ = start(make_client, mail)
    ocr.push(certificate_text())
    worker.run_tick()
    request = only_request(db)
    if via == "cabinet":
        response = owner.post(f"/requests/{request.id}/cancel", data={"csrf_token": page_csrf(owner, "/requests")})
    else:
        path = cancel_path(emails(mail, "E2")[0])
        visitor = make_client()
        response = visitor.post(path, data={"csrf_token": csrf_from(visitor.get(path).text)})
    assert response.status_code == 303
    (e9,) = emails(mail, "E9")
    assert e9.to == HEIR and e9.subject == "Запрос отменён владельцем"


def test_at30_no_heir_emails_without_contact_email(make_client, mail, db, ocr, settings):
    owner, heir = start(make_client, mail, contact="")
    ocr.push(certificate_text())
    worker.run_tick()
    release_flow(db, settings)
    assert only_request(db).status == "RELEASED"
    assert all(m.to == OWNER for m in mail.outbox)
    assert {m.email_id for m in mail.outbox} >= {"E2", "E3", "E5"}
    assert not {"E6", "E7", "E8", "E9"} & {m.email_id for m in mail.outbox}


def test_at30_p1_email_failure_does_not_change_status(make_client, mail, db, ocr, settings):
    start(make_client, mail)
    mail.fail_recipients = {HEIR}
    ocr.push(certificate_text())
    worker.run_tick()
    assert only_request(db).status == "WAITING_CANCELLATION"  # E7 не отправлено
    assert emails(mail, "E7") == [] and len(emails(mail, "E2")) == 1

    clock.set_now(only_request(db).waiting_until - timedelta(seconds=settings.REMINDER_BEFORE_SECONDS))
    worker.run_tick()
    mail.fail_recipients = {HEIR, OWNER}  # E5 и E8 не отправятся
    clock.set_now(only_request(db).waiting_until + timedelta(seconds=1))
    worker.run_tick()
    assert only_request(db).status == "RELEASED"
    assert emails(mail, "E5") == [] and emails(mail, "E8") == []

    mail.fail_recipients = set()
    for _ in range(2):
        worker.run_tick()
    assert emails(mail, "E5") == [] and emails(mail, "E7") == []  # P1 — без повторов


def test_at30_p1_failures_on_rejection_and_cancel(make_client, mail, db, ocr):
    owner, heir = start(make_client, mail)
    mail.fail_recipients = {OWNER, HEIR}
    ocr.push(certificate_text(last="Петрова"))
    worker.run_tick()
    assert only_request(db).status == "REJECTED"
    assert emails(mail, "E4") == [] and emails(mail, "E6") == []

    mail.fail_recipients = set()
    clock.advance(60)
    submit_document(heir, contact=HEIR)
    request = only_request_pending(db)
    mail.fail_recipients = {HEIR}
    response = owner.post(f"/requests/{request.id}/cancel", data={"csrf_token": page_csrf(owner, "/requests")})
    assert response.status_code == 303
    db.expire_all()
    assert db.get(type(request), request.id).status == "CANCELLED"
    assert emails(mail, "E9") == []


def only_request_pending(db):
    from sqlalchemy import select

    from app.models import InheritanceRequest

    db.expire_all()
    return db.execute(select(InheritanceRequest).where(InheritanceRequest.status == "PENDING_REVIEW")).scalar_one()
