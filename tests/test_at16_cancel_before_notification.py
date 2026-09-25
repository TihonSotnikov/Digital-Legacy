"""AT-16: отмена из кабинета до уведомления."""

from __future__ import annotations

from app import storage, worker
from tests.helpers import (
    add_heir,
    certificate_text,
    emails,
    heir_login,
    only_request,
    page_csrf,
    signup,
    submit_document,
)


def start(make_client, mail):
    owner = signup(make_client(), mail)
    _, key = add_heir(owner, "Пётр")
    heir = make_client()
    heir_login(heir, key)
    assert submit_document(heir).status_code == 303
    return owner


def cancel_from_cabinet(owner, request_id):
    page = owner.get("/requests")
    assert "Отменить" in page.text
    assert "Отменить запрос? Ключ наследника будет отозван" in page.text
    response = owner.post(f"/requests/{request_id}/cancel", data={"csrf_token": page_csrf(owner, "/requests")})
    assert response.status_code == 303
    assert response.headers["location"] == "/requests"
    return response


def test_at16_cancel_in_pending_review_skips_ocr_and_removes_document(make_client, mail, db, ocr):
    owner = start(make_client, mail)
    request = only_request(db)
    assert request.status == "PENDING_REVIEW"
    assert storage.doc_path(request.id).exists()

    cancel_from_cabinet(owner, request.id)
    assert only_request(db).status == "CANCELLED"
    assert "Запрос отменён" in owner.get("/requests").text

    ocr.push(certificate_text())
    worker.run_tick()
    assert ocr.calls == 0
    request = only_request(db)
    assert request.status == "CANCELLED"
    assert request.doc_stored is False
    assert not storage.doc_path(request.id).exists()


def test_at16_cancel_in_document_accepted_while_smtp_down_sends_no_e2(make_client, mail, db, ocr):
    owner = start(make_client, mail)
    mail.fail = True
    ocr.push(certificate_text())
    worker.run_tick()
    request = only_request(db)
    assert request.status == "DOCUMENT_ACCEPTED"

    cancel_from_cabinet(owner, request.id)
    assert only_request(db).status == "CANCELLED"

    mail.fail = False
    worker.run_tick()
    worker.run_tick()
    assert emails(mail, "E2") == []
    assert only_request(db).status == "CANCELLED"
