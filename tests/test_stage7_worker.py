"""Worker и выдача (этап 7): изоляция ошибок, жизненный цикл процесса, экраны запросов."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from datetime import timedelta

from sqlalchemy import select

from app import clock, tokens, worker
from app.config import RequestIdFilter
from app.email import backends
from app.models import Heir, HeirKey, InheritanceRequest, SystemState
from app.routes import format_datetime
from tests.conftest import ROOT
from tests.helpers import (
    add_heir,
    certificate_text,
    emails,
    make_request,
    page_csrf,
    reload,
    signup,
)


def accepted_request(db, owner_client, name):
    heir_id, _ = add_heir(owner_client, name)
    heir_key = db.execute(select(HeirKey).where(HeirKey.heir_id == heir_id)).scalar_one()
    return make_request(db, db.get(Heir, heir_id), heir_key, status="DOCUMENT_ACCEPTED")


def test_error_in_one_request_does_not_stop_others(make_client, mail, db, monkeypatch, caplog):
    first_owner = signup(make_client(), mail, email="first@example.com")
    second_owner = signup(make_client(), mail, email="second@example.com")
    failing = accepted_request(db, first_owner, "Первый")
    clock.advance(1)
    working = accepted_request(db, second_owner, "Второй")

    real_send = backends.send_email

    def flaky_send(email_id, to, **context):
        if to == "first@example.com":
            raise RuntimeError("неожиданный сбой")
        return real_send(email_id, to, **context)

    monkeypatch.setattr(worker, "send_email", flaky_send)
    caplog.handler.addFilter(RequestIdFilter())
    with caplog.at_level(logging.ERROR):
        worker.run_tick()
    assert reload(db, failing).status == "DOCUMENT_ACCEPTED"
    assert reload(db, working).status == "WAITING_CANCELLATION"
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any(r.request_id_part == f" request_id={failing.id}" for r in errors)
    assert db.get(SystemState, "worker_heartbeat") is not None


def test_e2_contents_and_dates_in_display_timezone(make_client, mail, db, settings):
    owner = signup(make_client(), mail)
    request = accepted_request(db, owner, "Мария")
    worker.run_tick()
    (e2,) = emails(mail, "E2")
    request = reload(db, request)
    assert "Мария" in e2.body
    assert format_datetime(request.created_at) in e2.body
    assert format_datetime(request.waiting_until) in e2.body and "MSK" in e2.body
    assert f"http://testserver/cancel/{tokens.make_cancel_token(request.id)}" in e2.body
    assert "http://testserver/requests" in e2.body


def test_requests_page_labels_and_banner(make_client, mail, db, settings):
    owner = signup(make_client(), mail)
    assert "Запросов нет" in owner.get("/requests").text
    request = accepted_request(db, owner, "Мария")
    page = owner.get("/vault").text
    assert "Поступил наследственный запрос от наследника Мария. Если вы его не ожидали — отмените." in page
    assert "Перейти к запросам" in page
    assert "Документ принят, отправляется уведомление" in owner.get("/requests").text

    worker.run_tick()
    request = reload(db, request)
    page = owner.get("/requests").text
    assert f"Ожидание отмены до {format_datetime(request.waiting_until)}" in page
    assert "Отменить" in page

    clock.set_now(request.waiting_until - timedelta(seconds=settings.REMINDER_BEFORE_SECONDS))
    worker.run_tick()
    clock.set_now(request.waiting_until + timedelta(seconds=1))
    worker.run_tick()
    request = reload(db, request)
    page = owner.get("/requests").text
    assert f"Доступ выдан {format_datetime(request.released_at)}" in page
    assert "/cancel" not in page and "Поступил наследственный запрос" not in owner.get("/vault").text


def test_cancel_page_for_finished_request_shows_status_only(make_client, mail, db):
    owner = signup(make_client(), mail)
    heir_id, _ = add_heir(owner, "Мария")
    heir_key = db.execute(select(HeirKey).where(HeirKey.heir_id == heir_id)).scalar_one()
    request = make_request(db, db.get(Heir, heir_id), heir_key, status="REJECTED")
    page = make_client().get(f"/cancel/{tokens.make_cancel_token(request.id)}").text
    assert "Статус: Отклонён" in page and "Отменить запрос" not in page


def test_cancel_link_for_deleted_request_is_not_found(make_client, mail, db):
    import uuid

    response = make_client().get(f"/cancel/{tokens.make_cancel_token(uuid.uuid4())}")
    assert response.status_code == 404


def test_worker_process_stops_after_current_cycle_on_sigterm(db):
    env = {**os.environ, "WORKER_POLL_SECONDS": "1", "OCR_PROVIDER": "fake"}
    process = subprocess.Popen(
        [sys.executable, "-m", "app.worker"], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            db.expire_all()
            if db.get(SystemState, "worker_heartbeat") is not None:
                break
            time.sleep(0.2)
        else:
            raise AssertionError("heartbeat не появился")
        process.send_signal(signal.SIGTERM)
        output, _ = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
    assert process.returncode == 0
    assert "Worker запущен" in output and "Worker остановлен" in output
