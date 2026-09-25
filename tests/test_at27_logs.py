"""AT-27: секреты не попадают в логи.

Сценарий AT-13 выполняется через настоящий HTTP-сервер uvicorn (в потоке), чтобы в
перехват попал и access-лог. Перехватываются все записи уровня DEBUG и выше; исключаются
только логгеры HTTP-клиента самого теста (httpx, httpcore) — это не логи приложения.
"""

from __future__ import annotations

import logging
import os
import re
import socket
import threading
import time
from datetime import timedelta

import httpx
import pytest
import uvicorn

from app import clock, worker
from app.config import TokenMaskFilter
from tests.helpers import (
    DEFAULT_PASSWORD,
    cancel_path,
    certificate_text,
    csrf_from,
    emails,
    key_from_page,
    only_request,
    png_document,
    verification_path,
)

R1_TEXT = "Секретный PIN 7391 от карты"
R3_TEXT = "Личный дневник 5520"
R2_BYTES = b"\x89PNG\r\n\x1a\n" + os.urandom(512)


class Collector(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def live_server(app):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, access_log=True, lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(10)


@pytest.fixture
def captured_logs():
    collector = Collector()
    root = logging.getLogger()
    previous = root.level
    root.addHandler(collector)
    root.setLevel(logging.DEBUG)
    yield collector
    root.removeHandler(collector)
    root.setLevel(previous)


def form(client: httpx.Client, path: str) -> str:
    return csrf_from(client.get(path).text)


def test_at27_secrets_absent_from_logs_during_at13_scenario(live_server, captured_logs, mail, ocr, settings):
    owner = httpx.Client(base_url=live_server, follow_redirects=False)
    heir = httpx.Client(base_url=live_server, follow_redirects=False)
    visitor = httpx.Client(base_url=live_server, follow_redirects=False)
    try:
        response = owner.post(
            "/register",
            data={
                "csrf_token": form(owner, "/register"),
                "email": "owner@example.com",
                "password": DEFAULT_PASSWORD,
                "password_confirm": DEFAULT_PASSWORD,
                "last_name": "Смирнова",
                "first_name": "Анна",
                "middle_name": "Сергеевна",
                "consent": "on",
            },
        )
        assert response.status_code == 303
        verify = verification_path(mail, "owner@example.com")
        assert owner.get(verify).status_code == 303

        def text_record(title: str, content: str) -> None:
            data = {"csrf_token": form(owner, "/vault"), "type": "text", "title": title, "content": content}
            assert owner.post("/records", data=data).status_code == 303

        text_record("R1", R1_TEXT)
        text_record("R3", R3_TEXT)
        response = owner.post(
            "/records",
            data={"csrf_token": form(owner, "/vault"), "type": "file", "title": "R2"},
            files={"file": ("r2.png", R2_BYTES, "image/png")},
        )
        assert response.status_code == 303
        vault = owner.get("/vault").text
        r1, r2 = (re.search(rf'<strong>{t}</strong>.*?/records/([0-9a-f-]{{36}})', vault, re.S).group(1) for t in ("R1", "R2"))

        response = owner.post("/heirs", data={"csrf_token": form(owner, "/heirs"), "name": "Н", "record_ids": [r1, r2]})
        shown_key = key_from_page(owner.get(response.headers["location"]).text)
        key = shown_key.replace(" ", "")

        assert heir.post("/heir/login", data={"csrf_token": form(heir, "/heir"), "key": key}).status_code == 303
        response = heir.post(
            "/heir/request",
            data={"csrf_token": form(heir, "/heir/portal"), "contact_email": ""},
            files={"document": ("certificate.png", png_document(), "image/png")},
        )
        assert response.status_code == 303

        ocr.push(certificate_text())
        worker.run_tick()
        cancel = cancel_path(emails(mail, "E2")[0])
        assert visitor.get(cancel).status_code == 200  # ссылка отмены открыта, но не использована

        request = only_request_from_db()
        clock.set_now(request.waiting_until - timedelta(seconds=settings.REMINDER_BEFORE_SECONDS))
        worker.run_tick()
        clock.set_now(request.waiting_until + timedelta(seconds=1))
        worker.run_tick()

        heir.cookies.clear()
        assert heir.post("/heir/login", data={"csrf_token": form(heir, "/heir"), "key": key}).status_code == 303
        portal = heir.get("/heir/portal").text
        assert R1_TEXT in portal
        assert heir.get(f"/heir/files/{r2}").content == R2_BYTES
    finally:
        owner.close()
        heir.close()
        visitor.close()

    formatter = logging.Formatter("%(name)s %(levelname)s %(message)s")
    app_records = [r for r in captured_logs.records if not r.name.startswith(("httpx", "httpcore"))]
    text = "\n".join(formatter.format(r) for r in app_records)

    verify_token = verify.rsplit("/", 1)[1]
    cancel_token = cancel.rsplit("/", 1)[1]
    secrets = {
        "ключ наследника": key,
        "ключ наследника (группами)": shown_key,
        "ключ наследника (верхний регистр)": key.upper(),
        "токен отмены": cancel_token,
        "токен подтверждения": verify_token,
        "пароль": DEFAULT_PASSWORD,
        "текст записи R1": R1_TEXT,
        "текст записи R3": R3_TEXT,
    }
    for label, secret in secrets.items():
        assert secret not in text, label

    access = [formatter.format(r) for r in app_records if r.name == "uvicorn.access"]
    assert any("/verify-email/*** " in line for line in access)
    assert any("/cancel/*** " in line for line in access)
    assert any("/heir/portal" in line for line in access)


def only_request_from_db():
    from app.db import new_session

    with new_session() as db:
        return only_request(db)


def test_at27_access_log_filter_masks_cancel_and_verify_paths():
    for path, masked in (("/cancel/abc", "/cancel/***"), ("/verify-email/abc", "/verify-email/***")):
        record = logging.LogRecord(
            "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
            ("10.0.0.1:1234", "GET", path, "1.1", 200), None,
        )
        TokenMaskFilter().filter(record)
        assert record.getMessage() == f'10.0.0.1:1234 - "GET {masked} HTTP/1.1" 200'
    record = logging.LogRecord("app", logging.INFO, __file__, 1, "Открыт /cancel/abc?x=1", None, None)
    TokenMaskFilter().filter(record)
    assert record.getMessage() == "Открыт /cancel/***?x=1"
