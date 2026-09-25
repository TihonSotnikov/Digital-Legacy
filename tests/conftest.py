"""Инфраструктура тестов (4.4).

Профиль test: база app_test, письма в памяти, fake OCR. Переменные окружения
контейнера, влияющие на поведение, сбрасываются к значениям по умолчанию из 3.4.1,
чтобы настройки .env (например, укороченный период ожидания) не влияли на тесты.
"""

from __future__ import annotations

import base64
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
if not TEST_DATABASE_URL:
    raise RuntimeError("Для тестов нужна переменная TEST_DATABASE_URL (база app_test)")

from app.config import Settings  # noqa: E402  (импорт не читает окружение)

for _name in Settings.model_fields:
    os.environ.pop(_name, None)
os.environ.update(
    {
        "APP_ENV": "test",
        "DATABASE_URL": TEST_DATABASE_URL,
        "TEST_DATABASE_URL": TEST_DATABASE_URL,
        "SECRET_KEY": "test-secret-key-0123456789abcdef0123456789",
        "MASTER_KEY": base64.b64encode(bytes(range(32))).decode(),
        "EMAIL_BACKEND": "memory",
        "OCR_PROVIDER": "fake",
        "BASE_URL": "http://testserver",
        "COOKIE_SECURE": "false",
    }
)

from alembic import command  # noqa: E402
from alembic.config import Config as AlembicConfig  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from app import clock  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import get_engine, new_session  # noqa: E402
from app.email.backends import MemoryBackend, get_backend  # noqa: E402
from app.security import limiter  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _migrated_database():
    """Миграции применяются один раз за прогон, на чистую схему."""
    engine = create_engine(TEST_DATABASE_URL, hide_parameters=True)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()
    cfg = AlembicConfig(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(cfg, "head")
    yield


def _truncate_all_tables() -> None:
    with get_engine().begin() as conn:
        tables = conn.execute(
            text(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
            )
        ).scalars().all()
        if tables:
            names = ", ".join(f'"{name}"' for name in tables)
            conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Перед каждым тестом: пустые таблицы, свой FILES_DIR, замороженные часы."""
    settings = get_settings()
    files_dir = tmp_path / "files"
    files_dir.mkdir()
    monkeypatch.setattr(settings, "FILES_DIR", str(files_dir))
    _truncate_all_tables()
    clock.set_now(datetime.now(timezone.utc).replace(microsecond=0))
    get_backend().reset()
    limiter.reset()
    yield
    clock.reset()


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
def mail() -> MemoryBackend:
    backend = get_backend()
    assert isinstance(backend, MemoryBackend)
    return backend


@pytest.fixture
def files_dir(settings) -> Path:
    return Path(settings.FILES_DIR)


@pytest.fixture
def db():
    session = new_session()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def app():
    from app.main import create_app

    return create_app()


@pytest.fixture
def make_client(app):
    clients: list[TestClient] = []

    def factory() -> TestClient:
        client = TestClient(app, base_url="http://testserver", follow_redirects=False)
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.close()


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()
