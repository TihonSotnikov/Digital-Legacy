"""AT-15: отмена по ссылке."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app import clock, worker
from app.models import HeirKey
from tests.helpers import (
    add_heir,
    cancel_path,
    certificate_text,
    csrf_from,
    emails,
    error_codes,
    events,
    heir_login,
    only_request,
    reload,
    signup,
    submit_document,
)


def test_at15_cancel_by_link_from_e2(make_client, mail, db, ocr, settings):
    owner = signup(make_client(), mail)
    heir_id, key = add_heir(owner, "Пётр")
    heir = make_client()
    heir_login(heir, key)
    submit_document(heir)
    ocr.push(certificate_text())
    worker.run_tick()
    assert only_request(db).status == "WAITING_CANCELLATION"
    path = cancel_path(emails(mail, "E2")[0])

    visitor = make_client()  # без входа в кабинет
    page = visitor.get(path)
    assert page.status_code == 200
    assert page.headers["Cache-Control"] == "no-store"
    assert "Наследственный запрос от наследника Пётр" in page.text
    assert "Статус: Ожидание отмены до" in page.text
    assert "Отменить запрос" in page.text and f'action="{path}"' in page.text

    token = csrf_from(page.text)
    response = visitor.post(path, data={"csrf_token": token})
    assert response.status_code == 303
    assert response.headers["location"] == path
    request = only_request(db)
    assert request.status == "CANCELLED"
    assert request.cancelled_at == clock.now()
    heir_key = db.execute(select(HeirKey).where(HeirKey.heir_id == heir_id)).scalar_one()
    assert heir_key.revoked_at == clock.now()
    after = visitor.get(path).text
    assert "Запрос отменён. Ключ наследника отозван. Новый ключ можно выпустить в личном кабинете" in after
    assert "Статус: Отменён" in after and 'action="' + path not in after

    response = heir_login(make_client(), key)
    assert response.status_code == 401
    assert error_codes(response.text) == ["INVALID_KEY"]

    response = visitor.post(path, data={"csrf_token": token})
    assert response.status_code == 409
    assert error_codes(response.text) == ["INVALID_TRANSITION"]

    prefix, token_value = path.rsplit("/", 1)
    tampered = f"{prefix}/{'J' if token_value[0] != 'J' else 'K'}{token_value[1:]}"  # изменён payload
    for method in ("get", "post"):
        kwargs = {"data": {"csrf_token": token}} if method == "post" else {}
        response = getattr(visitor, method)(tampered, **kwargs)
        assert response.status_code == 400, method
        assert error_codes(response.text) == ["INVALID_TOKEN"], method

    # Последующие циклы (в том числе после waiting_until) статус не меняют.
    clock.advance(settings.WAITING_PERIOD_SECONDS + 60)
    for _ in range(3):
        worker.run_tick()
    assert only_request(db).status == "CANCELLED"
    changes = [(e.meta["from"], e.meta["to"]) for e in events(db, "REQUEST_STATUS_CHANGED")]
    assert changes[-1] == ("WAITING_CANCELLATION", "CANCELLED")
    assert [e.meta["reason"] for e in events(db, "HEIR_KEY_REVOKED")] == ["cancel"]
    assert emails(mail, "E3") == []
    assert reload(db, heir_key).revoked_at is not None


def test_at15_same_cancel_link_in_e2_and_e3(make_client, mail, db, ocr, settings):
    owner = signup(make_client(), mail)
    _, key = add_heir(owner, "Пётр")
    heir = make_client()
    heir_login(heir, key)
    submit_document(heir)
    ocr.push(certificate_text())
    worker.run_tick()
    clock.set_now(only_request(db).waiting_until - timedelta(seconds=settings.REMINDER_BEFORE_SECONDS))
    worker.run_tick()
    assert cancel_path(emails(mail, "E2")[0]) == cancel_path(emails(mail, "E3")[0])
