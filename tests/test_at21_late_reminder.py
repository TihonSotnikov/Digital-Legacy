"""AT-21: позднее напоминание."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app import clock, worker
from app.models import Heir, HeirKey
from app.routes import format_datetime
from tests.helpers import add_heir, emails, events, make_request, only_request, signup


def test_at21_late_reminder_extends_waiting_and_delays_release(make_client, mail, db, settings):
    owner = signup(make_client(), mail)
    heir_id, _ = add_heir(owner, "Пётр")
    heir_key = db.execute(select(HeirKey).where(HeirKey.heir_id == heir_id)).scalar_one()
    original_until = clock.now() + timedelta(seconds=settings.WAITING_PERIOD_SECONDS)
    make_request(db, db.get(Heir, heir_id), heir_key, status="WAITING_CANCELLATION", waiting_until=original_until)

    # Worker не работал: время ушло за waiting_until без циклов.
    clock.set_now(original_until + timedelta(days=2))
    worker.run_tick()
    request = only_request(db)
    assert request.status == "WAITING_CANCELLATION"  # доступ не выдан
    assert request.reminder_sent_at == clock.now()
    assert request.waiting_until == clock.now() + timedelta(seconds=settings.REMINDER_BEFORE_SECONDS)
    (e3,) = emails(mail, "E3")
    assert format_datetime(request.waiting_until) in e3.subject and format_datetime(request.waiting_until) in e3.body
    assert events(db, "REMINDER_SENT")[0].meta == {"waiting_until": request.waiting_until.isoformat()}

    clock.set_now(request.waiting_until - timedelta(seconds=1))
    worker.run_tick()
    assert only_request(db).status == "WAITING_CANCELLATION"

    clock.set_now(request.waiting_until + timedelta(seconds=1))
    worker.run_tick()
    assert only_request(db).status == "RELEASED"


def test_at21_release_requires_reminder_even_when_due(make_client, mail, db, settings):
    """Шаг D не выдаёт доступ без отправленного напоминания (SMTP недоступен)."""
    owner = signup(make_client(), mail)
    heir_id, _ = add_heir(owner, "Пётр")
    heir_key = db.execute(select(HeirKey).where(HeirKey.heir_id == heir_id)).scalar_one()
    make_request(db, db.get(Heir, heir_id), heir_key, status="WAITING_CANCELLATION", waiting_until=clock.now())
    clock.advance(30 * 86400)
    mail.fail = True
    for _ in range(3):
        worker.run_tick()
    assert only_request(db).status == "WAITING_CANCELLATION"
    assert only_request(db).reminder_sent_at is None
