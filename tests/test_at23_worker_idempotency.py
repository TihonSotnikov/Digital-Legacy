"""AT-23: идемпотентность Worker."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app import clock, worker
from app.models import Heir, HeirKey
from tests.helpers import (
    add_heir,
    certificate_text,
    emails,
    events,
    heir_login,
    make_request,
    only_request,
    signup,
    submit_document,
)


def test_at23_five_ticks_after_release_change_nothing(make_client, mail, db, ocr, settings):
    owner = signup(make_client(), mail)
    _, key = add_heir(owner, "Пётр")
    heir = make_client()
    heir_login(heir, key)
    submit_document(heir, contact="heir@example.com")
    ocr.push(certificate_text())
    worker.run_tick()
    clock.set_now(only_request(db).waiting_until - timedelta(seconds=settings.REMINDER_BEFORE_SECONDS))
    worker.run_tick()
    clock.set_now(only_request(db).waiting_until + timedelta(seconds=1))
    worker.run_tick()
    assert only_request(db).status == "RELEASED"

    status_events = len(events(db, "REQUEST_STATUS_CHANGED"))
    sent = len(mail.outbox)
    for _ in range(5):
        clock.advance(60)
        worker.run_tick()
    assert len(events(db, "REQUEST_STATUS_CHANGED")) == status_events
    assert len(mail.outbox) == sent
    assert only_request(db).status == "RELEASED"


def test_at23_five_ticks_in_reminder_window_send_e3_once(make_client, mail, db, settings):
    owner = signup(make_client(), mail)
    heir_id, _ = add_heir(owner, "Пётр")
    heir_key = db.execute(select(HeirKey).where(HeirKey.heir_id == heir_id)).scalar_one()
    until = clock.now() + timedelta(seconds=settings.REMINDER_BEFORE_SECONDS - 3600)  # окно напоминания
    make_request(db, db.get(Heir, heir_id), heir_key, status="WAITING_CANCELLATION", waiting_until=until)
    for _ in range(5):
        worker.run_tick()
        clock.advance(10)
    assert len(emails(mail, "E3")) == 1
    assert len(events(db, "REMINDER_SENT")) == 1
    assert only_request(db).status == "WAITING_CANCELLATION"
