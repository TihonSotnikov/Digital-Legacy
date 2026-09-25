"""AT-18: недоступность OCR."""

from __future__ import annotations

import logging

from sqlalchemy import select

from app import clock, worker
from app.config import RequestIdFilter
from app.models import Heir, HeirKey
from app.ocr.provider import OcrUnavailable
from tests.helpers import add_heir, heir_login, make_request, only_request, signup, submit_document


def test_at18_ocr_failures_reject_with_ocr_unavailable_not_counted(make_client, mail, db, ocr, settings, caplog):
    owner = signup(make_client(), mail)
    heir_id, key = add_heir(owner, "Пётр")
    heir = make_client()
    heir_login(heir, key)
    submit_document(heir)
    request_id = only_request(db).id

    failures = [OcrUnavailable("модель недоступна"), RuntimeError("сбой"), MemoryError()]
    ocr.push(*failures[: settings.OCR_MAX_ATTEMPTS])
    caplog.handler.addFilter(RequestIdFilter())  # request_id в записи лога, как в stdout
    with caplog.at_level(logging.INFO):
        for attempt in range(1, settings.OCR_MAX_ATTEMPTS):
            worker.run_tick()
            request = only_request(db)
            assert request.status == "PENDING_REVIEW"
            assert request.ocr_attempts == attempt
    errors = [r for r in caplog.records if r.levelno == logging.ERROR and r.name == "app.worker"]
    assert errors and all(getattr(r, "request_id_part", "") == f" request_id={request_id}" for r in errors)

    worker.run_tick()
    request = only_request(db)
    assert request.status == "REJECTED"
    assert request.check_result == {
        "accepted": False,
        "reasons": ["OCR_UNAVAILABLE"],
        "attempts": settings.OCR_MAX_ATTEMPTS,
    }
    assert request.rejected_at is not None
    assert ocr.calls == settings.OCR_MAX_ATTEMPTS

    # Отклонение с OCR_UNAVAILABLE не учитывается в суточном лимите.
    heir_key = db.execute(select(HeirKey).where(HeirKey.heir_id == heir_id)).scalar_one()
    for _ in range(settings.HEIR_REQUESTS_PER_DAY - 1):
        make_request(db, db.get(Heir, heir_id), heir_key, status="REJECTED")
    clock.advance(60)
    portal = heir.get("/heir/portal").text
    assert "Превышено число попыток за сутки" not in portal
    assert submit_document(heir).status_code == 303
