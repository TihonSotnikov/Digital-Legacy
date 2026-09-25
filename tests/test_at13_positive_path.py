"""AT-13: полный позитивный путь."""

from __future__ import annotations

import os
from datetime import timedelta

from app import clock, storage, worker
from app.routes import format_datetime
from tests.helpers import (
    add_file_record,
    add_heir,
    add_text_record,
    certificate_text,
    emails,
    events,
    heir_login,
    only_request,
    signup,
    submit_document,
)

R2_BYTES = b"\x89PNG\r\n\x1a\n" + os.urandom(3000)


def test_at13_full_positive_path(make_client, mail, db, ocr, settings, files_dir):
    owner = signup(make_client(), mail)
    r1 = add_text_record(owner, "R1 Банковская карта", "PIN-код карты 7391")
    r2 = add_file_record(owner, "R2 Скан договора", "договор.png", R2_BYTES)
    r3 = add_text_record(owner, "R3 Личный дневник", "Только для меня 5520")
    heir_id, key = add_heir(owner, "Наследник Н", [r1, r2])

    heir = make_client()
    assert heir_login(heir, key).status_code == 303
    assert submit_document(heir, contact="heir@example.com").status_code == 303
    request = only_request(db)
    assert storage.doc_path(request.id).exists()

    # Первый цикл: проверка, E2, начало ожидания, очистка документа.
    ocr.push(certificate_text())
    worker.run_tick()
    request = only_request(db)
    assert request.status == "WAITING_CANCELLATION"
    assert request.notified_at == clock.now()
    assert request.waiting_until == clock.now() + timedelta(seconds=settings.WAITING_PERIOD_SECONDS)
    assert request.check_result["accepted"] is True and request.check_result["reasons"] == []
    (e2,) = emails(mail, "E2")
    assert e2.to == "owner@example.com"
    assert e2.subject == "Поступил наследственный запрос — вы можете его отменить"
    assert "/cancel/" in e2.body and "/requests" in e2.body and "Наследник Н" in e2.body
    assert "Если вы не ожидали этот запрос, отмените его. Отмена отзовёт ключ наследника" in e2.body
    assert not storage.doc_path(request.id).exists()
    assert request.doc_stored is False
    portal = heir.get("/heir/portal").text
    assert format_datetime(request.waiting_until) in portal
    assert "Документ принят. Владелец данных уведомлён." in portal
    assert [e.event_type for e in events(db, "OWNER_NOTIFIED")] == ["OWNER_NOTIFIED"]

    # Сдвиг к waiting_until − REMINDER_BEFORE_SECONDS: напоминание E3.
    waiting_until = request.waiting_until
    clock.set_now(waiting_until - timedelta(seconds=settings.REMINDER_BEFORE_SECONDS))
    worker.run_tick()
    request = only_request(db)
    assert request.reminder_sent_at == clock.now()
    assert request.waiting_until == waiting_until
    (e3,) = emails(mail, "E3")
    assert e3.subject == f"Напоминание: доступ к вашим данным откроется {format_datetime(waiting_until)}"
    assert e3.to == "owner@example.com" and cancel_link(e2) == cancel_link(e3)
    assert request.status == "WAITING_CANCELLATION"

    # Сдвиг за waiting_until: выдача.
    clock.set_now(waiting_until + timedelta(seconds=1))
    worker.run_tick()
    request = only_request(db)
    assert request.status == "RELEASED"
    assert request.released_at == clock.now()

    heir = make_client()  # сессия наследника истекла за время ожидания
    assert heir_login(heir, key).status_code == 303
    portal = heir.get("/heir/portal")
    assert portal.status_code == 200 and portal.headers["Cache-Control"] == "no-store"
    assert "Доступ открыт" in portal.text
    assert "R1 Банковская карта" in portal.text and "PIN-код карты 7391" in portal.text
    assert f'href="/heir/files/{r2}"' in portal.text and "договор.png" in portal.text
    assert "R3 Личный дневник" not in portal.text and "Только для меня 5520" not in portal.text

    response = heir.get(f"/heir/files/{r3}")
    assert response.status_code == 404
    response = heir.get(f"/heir/files/{r2}")
    assert response.status_code == 200
    assert response.content == R2_BYTES
    assert response.headers["Content-Disposition"].startswith("attachment;")
    assert response.headers["Cache-Control"] == "no-store"

    viewed = events(db, "HEIR_DATA_VIEWED")
    downloaded = events(db, "HEIR_FILE_DOWNLOADED")
    assert viewed and viewed[0].meta == {"heir_id": str(heir_id)} and viewed[0].request_id == request.id
    assert [e.meta for e in downloaded] == [{"heir_id": str(heir_id), "record_id": str(r2)}]
    changes = [(e.meta["from"], e.meta["to"]) for e in events(db, "REQUEST_STATUS_CHANGED")]
    assert changes == [
        ("PENDING_REVIEW", "DOCUMENT_ACCEPTED"),
        ("DOCUMENT_ACCEPTED", "WAITING_CANCELLATION"),
        ("WAITING_CANCELLATION", "RELEASED"),
    ]


def cancel_link(message) -> str:
    return next(line for line in message.body.splitlines() if "/cancel/" in line)
