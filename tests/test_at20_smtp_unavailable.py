"""AT-20: недоступность SMTP."""

from __future__ import annotations

from datetime import timedelta

from app import clock, worker
from tests.helpers import add_heir, certificate_text, emails, events, heir_login, only_request, signup, submit_document


def test_at20_waiting_starts_only_after_successful_send(make_client, mail, db, ocr, settings):
    owner = signup(make_client(), mail)
    _, key = add_heir(owner, "Пётр")
    heir = make_client()
    heir_login(heir, key)
    submit_document(heir)

    mail.fail = True
    ocr.push(certificate_text())
    worker.run_tick()
    request = only_request(db)
    assert request.status == "DOCUMENT_ACCEPTED"
    assert request.waiting_until is None and request.notified_at is None
    assert emails(mail, "E2") == []
    assert events(db, "OWNER_NOTIFIED") == []

    clock.advance(2 * 3600)
    worker.run_tick()  # SMTP всё ещё недоступен
    assert only_request(db).status == "DOCUMENT_ACCEPTED"

    clock.advance(3600)
    mail.fail = False
    sent_at = clock.now()
    worker.run_tick()
    request = only_request(db)
    assert request.status == "WAITING_CANCELLATION"
    assert request.notified_at == sent_at
    assert request.waiting_until == sent_at + timedelta(seconds=settings.WAITING_PERIOD_SECONDS)
    assert len(emails(mail, "E2")) == 1
