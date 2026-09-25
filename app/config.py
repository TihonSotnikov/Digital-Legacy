"""Конфигурация (3.4.1), проверки при старте и настройка логирования (3.2.10)."""

from __future__ import annotations

import base64
import binascii
import contextvars
import logging
import re
import sys
from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=True, extra="ignore")

    APP_ENV: Literal["dev", "test", "prod"] = "dev"
    BASE_URL: str = "http://localhost:8000"
    SECRET_KEY: str
    MASTER_KEY: str
    DATABASE_URL: str
    TEST_DATABASE_URL: str | None = None
    FILES_DIR: str = "/data/files"
    COOKIE_SECURE: bool = False
    SESSION_TTL_HOURS: int = Field(12, gt=0)
    HEIR_SESSION_TTL_MINUTES: int = Field(30, gt=0)
    VERIFY_TOKEN_TTL_HOURS: int = Field(48, gt=0)

    EMAIL_BACKEND: Literal["smtp", "memory", "console"] = "smtp"
    SMTP_HOST: str = "mailpit"
    SMTP_PORT: int = 1025
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = "no-reply@localhost"
    SMTP_STARTTLS: bool = False
    SMTP_TIMEOUT_SECONDS: int = Field(10, gt=0)

    OCR_PROVIDER: Literal["easyocr", "fake"] = "easyocr"
    OCR_LANGS: str = "ru,en"
    OCR_MAX_ATTEMPTS: int = Field(3, gt=0)
    OCR_MAX_IMAGE_SIDE: int = Field(2000, gt=0)
    PDF_RENDER_DPI: int = Field(200, gt=0)
    RULE_FUZZY_THRESHOLD: int = Field(85, ge=0, le=100)

    WAITING_PERIOD_SECONDS: int = 1209600
    REMINDER_BEFORE_SECONDS: int = 259200
    WORKER_POLL_SECONDS: int = Field(10, gt=0)
    WORKER_STALE_SECONDS: int = Field(60, gt=0)

    MAX_RECORDS: int = Field(50, gt=0)
    MAX_HEIRS: int = Field(5, gt=0)
    MAX_TEXT_CHARS: int = Field(10000, gt=0)
    MAX_FILE_MB: int = Field(10, gt=0)
    MAX_STORAGE_MB: int = Field(100, gt=0)
    MAX_DOC_MB: int = Field(10, gt=0)
    HEIR_REQUESTS_PER_DAY: int = Field(3, gt=0)
    DISPLAY_TZ: str = "Europe/Moscow"

    @field_validator("MASTER_KEY")
    @classmethod
    def _check_master_key(cls, value: str) -> str:
        try:
            raw = base64.b64decode(value.strip(), validate=True)
        except (binascii.Error, ValueError):
            raw = b""
        if len(raw) != 32:
            raise ValueError("MASTER_KEY должен быть base64 от ровно 32 байт")
        return value.strip()

    @field_validator("SECRET_KEY")
    @classmethod
    def _check_secret_key(cls, value: str) -> str:
        if len(value) < 32:
            raise ValueError("SECRET_KEY должен быть не короче 32 символов")
        return value

    @field_validator("DISPLAY_TZ")
    @classmethod
    def _check_tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"неизвестный часовой пояс DISPLAY_TZ: {value}") from None
        return value

    @model_validator(mode="after")
    def _startup_checks(self) -> Settings:
        if not 0 < self.REMINDER_BEFORE_SECONDS < self.WAITING_PERIOD_SECONDS:
            raise ValueError(
                "должно выполняться 0 < REMINDER_BEFORE_SECONDS < WAITING_PERIOD_SECONDS"
            )
        if self.WORKER_POLL_SECONDS > 30:
            raise ValueError("WORKER_POLL_SECONDS должен быть не больше 30")
        if self.APP_ENV == "prod":
            if not self.COOKIE_SECURE:
                raise ValueError("при APP_ENV=prod требуется COOKIE_SECURE=true")
            if self.EMAIL_BACKEND != "smtp":
                raise ValueError("при APP_ENV=prod требуется EMAIL_BACKEND=smtp")
            if self.OCR_PROVIDER != "easyocr":
                raise ValueError("при APP_ENV=prod требуется OCR_PROVIDER=easyocr")
            if not self.BASE_URL.startswith("https://"):
                raise ValueError("при APP_ENV=prod BASE_URL должен начинаться с https://")
        return self

    @property
    def master_key(self) -> bytes:
        return base64.b64decode(self.MASTER_KEY)

    @property
    def display_tz(self) -> ZoneInfo:
        return ZoneInfo(self.DISPLAY_TZ)

    @property
    def ocr_langs(self) -> list[str]:
        return [lang.strip() for lang in self.OCR_LANGS.split(",") if lang.strip()]

    @property
    def base_url(self) -> str:
        return self.BASE_URL.rstrip("/")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings() -> None:
    get_settings.cache_clear()


def describe_validation_error(error: ValidationError) -> list[str]:
    """Понятные строки ошибок без значений переменных (секреты не выводятся)."""
    lines = []
    for item in error.errors(include_input=False, include_url=False):
        name = ".".join(str(part) for part in item.get("loc", ()))
        if item.get("type") == "missing":
            message = "обязательная переменная не задана"
        else:
            message = str(item.get("msg", "")).removeprefix("Value error, ")
        lines.append(f"{name}: {message}" if name else message)
    return lines


def load_settings_or_exit() -> Settings:
    """Проверки при старте (3.4.1): при ошибке процесс завершается с понятным сообщением."""
    try:
        return get_settings()
    except ValidationError as error:
        details = "\n  ".join(describe_validation_error(error))
        print(f"Ошибка конфигурации:\n  {details}", file=sys.stderr, flush=True)
        raise SystemExit(1) from None


# --- Логирование --------------------------------------------------------------

current_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_request_id", default=None
)

TOKEN_PATH_RE = re.compile(r"/(cancel|verify-email)/[^/?\s]+")


def mask_token_paths(text: str) -> str:
    return TOKEN_PATH_RE.sub(r"/\1/***", text)


class TokenMaskFilter(logging.Filter):
    """Фильтр access-лога: /cancel/<токен> и /verify-email/<токен> → /…/***."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = mask_token_paths(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                mask_token_paths(arg) if isinstance(arg, str) else arg for arg in record.args
            )
        elif isinstance(record.args, dict):
            record.args = {
                key: mask_token_paths(arg) if isinstance(arg, str) else arg
                for key, arg in record.args.items()
            }
        return True


class RequestIdFilter(logging.Filter):
    """Добавляет request_id наследственного запроса, если он известен."""

    def filter(self, record: logging.LogRecord) -> bool:
        request_id = getattr(record, "request_id", None) or current_request_id.get()
        record.request_id_part = f" request_id={request_id}" if request_id else ""
        return True


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s%(request_id_part)s: %(message)s"
_QUIET_LOGGERS = ("sqlalchemy", "PIL", "multipart", "python_multipart", "asyncio", "easyocr")


def configure_logging(level: int = logging.INFO) -> None:
    """Логи в stdout с уровнем, именем логгера и request_id; токены в путях маскируются."""
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not getattr(h, "_digital_legacy", False)]
    handler = logging.StreamHandler(sys.stdout)
    handler._digital_legacy = True  # type: ignore[attr-defined]
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(RequestIdFilter())
    handler.addFilter(TokenMaskFilter())
    root.addHandler(handler)
    root.setLevel(level)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, TokenMaskFilter) for f in access.filters):
        access.addFilter(TokenMaskFilter())

    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
