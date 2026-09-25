"""Каркас (этап 1): проверки при старте, часы, маскирование логов, базовые страницы."""

from __future__ import annotations

import base64
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app import clock, worker
from app.config import Settings, TokenMaskFilter, describe_validation_error
from app.routes import SECURITY_HEADERS
from tests.conftest import ROOT

VALID = {
    "APP_ENV": "dev",
    "SECRET_KEY": "s" * 32,
    "MASTER_KEY": base64.b64encode(os.urandom(32)).decode(),
    "DATABASE_URL": "postgresql+psycopg://u:p@db/app",
    "EMAIL_BACKEND": "smtp",
    "OCR_PROVIDER": "easyocr",
    "COOKIE_SECURE": False,
    "BASE_URL": "http://localhost:8000",
}


def make_settings(**overrides) -> Settings:
    return Settings(**{**VALID, **overrides})


def test_settings_defaults_follow_spec():
    s = make_settings()
    assert (s.MAX_RECORDS, s.MAX_HEIRS, s.MAX_TEXT_CHARS) == (50, 5, 10000)
    assert (s.MAX_FILE_MB, s.MAX_STORAGE_MB, s.MAX_DOC_MB, s.HEIR_REQUESTS_PER_DAY) == (10, 100, 10, 3)
    assert (s.WAITING_PERIOD_SECONDS, s.REMINDER_BEFORE_SECONDS) == (1209600, 259200)
    assert (s.WORKER_POLL_SECONDS, s.WORKER_STALE_SECONDS, s.OCR_MAX_ATTEMPTS) == (10, 60, 3)
    assert s.ocr_langs == ["ru", "en"] and s.DISPLAY_TZ == "Europe/Moscow"


@pytest.mark.parametrize(
    "overrides, fragment",
    [
        ({"MASTER_KEY": base64.b64encode(b"x" * 31).decode()}, "MASTER_KEY"),
        ({"MASTER_KEY": base64.b64encode(b"x" * 33).decode()}, "MASTER_KEY"),
        ({"MASTER_KEY": "не base64"}, "MASTER_KEY"),
        ({"SECRET_KEY": "s" * 31}, "SECRET_KEY"),
        ({"REMINDER_BEFORE_SECONDS": 0}, "REMINDER_BEFORE_SECONDS"),
        ({"REMINDER_BEFORE_SECONDS": 100, "WAITING_PERIOD_SECONDS": 100}, "REMINDER_BEFORE_SECONDS"),
        ({"WORKER_POLL_SECONDS": 31}, "WORKER_POLL_SECONDS"),
        ({"APP_ENV": "prod", "BASE_URL": "https://x.ru"}, "COOKIE_SECURE"),
        ({"APP_ENV": "prod", "COOKIE_SECURE": True, "BASE_URL": "https://x.ru", "EMAIL_BACKEND": "console"}, "EMAIL_BACKEND"),
        ({"APP_ENV": "prod", "COOKIE_SECURE": True, "BASE_URL": "https://x.ru", "OCR_PROVIDER": "fake"}, "OCR_PROVIDER"),
        ({"APP_ENV": "prod", "COOKIE_SECURE": True, "BASE_URL": "http://x.ru"}, "https://"),
    ],
)
def test_startup_checks_reject_invalid_config(overrides, fragment):
    with pytest.raises(ValidationError) as info:
        make_settings(**overrides)
    messages = describe_validation_error(info.value)
    assert len(messages) == 1
    assert fragment in messages[0]


def test_prod_config_accepted():
    s = make_settings(APP_ENV="prod", COOKIE_SECURE=True, BASE_URL="https://legacy.example.ru")
    assert s.APP_ENV == "prod"


@pytest.mark.parametrize("command", [["-c", "import app.main"], ["-m", "app.worker"]])
def test_processes_exit_with_clear_error_on_bad_master_key(command):
    bad_key = base64.b64encode(b"k" * 16).decode()
    env = {**os.environ, "MASTER_KEY": bad_key}
    result = subprocess.run(
        [sys.executable, *command], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 1
    assert "Ошибка конфигурации" in result.stderr
    assert "MASTER_KEY" in result.stderr
    assert bad_key not in result.stderr + result.stdout


def test_clock_set_and_advance():
    moment = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    clock.set_now(moment)
    assert clock.now() == moment
    clock.advance(90)
    assert clock.now() == moment + timedelta(seconds=90)
    with pytest.raises(ValueError):
        clock.set_now(datetime(2030, 1, 1))


def test_token_mask_filter_masks_paths():
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "GET", "/verify-email/abc.def?x=1", "1.1", 200), None,
    )
    TokenMaskFilter().filter(record)
    assert record.getMessage() == '127.0.0.1:5000 - "GET /verify-email/***?x=1 HTTP/1.1" 200'


def test_landing_page_and_security_headers(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Цифровое наследие" in response.text
    assert "У меня есть ключ наследника" in response.text
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
    assert "Проверка документов автоматическая и не является юридическим подтверждением" in response.text


@pytest.mark.parametrize("path", ["/terms", "/privacy"])
def test_legal_pages_are_marked_placeholders(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert "Заглушка" in response.text


def test_unknown_page_is_not_found(client):
    response = client.get("/no-such-page")
    assert response.status_code == 404
    assert 'data-error-code="NOT_FOUND"' in response.text
    assert response.headers["X-Frame-Options"] == "DENY"


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_api_docs_not_exposed(client, path):
    assert client.get(path).status_code == 404


@pytest.mark.parametrize("path", ["/static/pico.min.css", "/static/app.css", "/static/app.js"])
def test_static_assets_served(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["Content-Security-Policy"] == "default-src 'self'"


def test_at26_healthz(client, settings):
    response = client.get("/healthz")
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["worker_heartbeat_age_seconds"] is None

    worker.run_tick()
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": True, "worker_heartbeat_age_seconds": 0}

    clock.advance(settings.WORKER_STALE_SECONDS + 1)
    response = client.get("/healthz")
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
