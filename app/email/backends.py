"""Отправка писем (3.2.9): интерфейс EmailBackend и реализации smtp, memory, console."""

from __future__ import annotations

import logging
import smtplib
import ssl
import sys
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Any, Protocol

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from app.config import get_settings

logger = logging.getLogger("app.email")


class EmailSendError(Exception):
    """Письмо не принято (SMTP недоступен, отказ сервера, тайм-аут)."""


@dataclass(frozen=True)
class Email:
    email_id: str  # E1–E9
    to: str
    subject: str
    body: str


class EmailBackend(Protocol):
    def send(self, email: Email) -> None: ...


class SmtpBackend:
    """Эксплуатация и разработка (Mailpit). Успешный возврат — письмо принято SMTP-сервером."""

    def send(self, email: Email) -> None:
        settings = get_settings()
        message = EmailMessage()
        message["From"] = settings.SMTP_FROM
        message["To"] = email.to
        message["Subject"] = email.subject
        message["Date"] = formatdate(localtime=False)
        message["Message-ID"] = make_msgid(domain=settings.SMTP_FROM.rpartition("@")[2] or None)
        message.set_content(email.body, charset="utf-8")
        try:
            with smtplib.SMTP(
                settings.SMTP_HOST, settings.SMTP_PORT, timeout=settings.SMTP_TIMEOUT_SECONDS
            ) as smtp:
                if settings.SMTP_STARTTLS:
                    smtp.starttls(context=ssl.create_default_context())
                if settings.SMTP_USER:
                    smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
                smtp.send_message(message)
        except (OSError, smtplib.SMTPException) as exc:
            raise EmailSendError(type(exc).__name__) from None


class MemoryBackend:
    """Тесты: письма складываются в outbox; режим ошибки имитирует недоступный SMTP."""

    def __init__(self) -> None:
        self.outbox: list[Email] = []
        self.fail = False
        self.fail_recipients: set[str] = set()

    def send(self, email: Email) -> None:
        if self.fail or email.to in self.fail_recipients:
            raise EmailSendError("memory backend: режим ошибки")
        self.outbox.append(email)

    def reset(self) -> None:
        self.outbox.clear()
        self.fail = False
        self.fail_recipients.clear()


class ConsoleBackend:
    """Отладка: письмо печатается в stdout (не в журнал). Не использовать в эксплуатации."""

    def send(self, email: Email) -> None:
        print(
            f"----- {email.email_id} → {email.to}\nSubject: {email.subject}\n\n{email.body}\n-----",
            file=sys.stdout,
            flush=True,
        )


_backend: EmailBackend | None = None


def get_backend() -> EmailBackend:
    global _backend
    if _backend is None:
        kind = get_settings().EMAIL_BACKEND
        _backend = {"smtp": SmtpBackend, "memory": MemoryBackend, "console": ConsoleBackend}[kind]()
    return _backend


def reset_backend() -> None:
    global _backend
    _backend = None


# --- Письма E1–E9 -------------------------------------------------------------------------

_env = Environment(
    loader=FileSystemLoader(Path(__file__).resolve().parent / "templates"),
    autoescape=False,
    keep_trailing_newline=True,
    trim_blocks=True,
    lstrip_blocks=True,
    undefined=StrictUndefined,
)

SUBJECTS = {
    "E1": "Подтвердите email",
    "E2": "Поступил наследственный запрос — вы можете его отменить",
    "E3": "Напоминание: доступ к вашим данным откроется {date}",
    "E4": "Наследственный запрос отклонён",
    "E5": "Доступ к вашим данным открыт наследнику",
    "E6": "Документ не прошёл проверку",
    "E7": "Документ принят, идёт период ожидания",
    "E8": "Доступ к данным открыт",
    "E9": "Запрос отменён владельцем",
}

TEMPLATES = {
    "E1": "e1_verify_email.txt",
    "E2": "e2_request_received.txt",
    "E3": "e3_reminder.txt",
    "E4": "e4_request_rejected.txt",
    "E5": "e5_access_released.txt",
    "E6": "e6_document_rejected.txt",
    "E7": "e7_document_accepted.txt",
    "E8": "e8_access_opened.txt",
    "E9": "e9_request_cancelled.txt",
}


def build_email(email_id: str, to: str, **context: Any) -> Email:
    subject = SUBJECTS[email_id].format(**context)
    body = _env.get_template(TEMPLATES[email_id]).render(**context)
    return Email(email_id=email_id, to=to, subject=subject, body=body)


def send_email(email_id: str, to: str, **context: Any) -> None:
    """Отправка; при неудаче — EmailSendError (вызывающий код решает, повторять ли)."""
    get_backend().send(build_email(email_id, to, **context))


def send_best_effort(email_id: str, to: str | None, **context: Any) -> bool:
    """Письма P1 и E9: одна попытка, ошибка только логируется (3.2.9)."""
    if not to:
        return False
    try:
        send_email(email_id, to, **context)
    except EmailSendError as exc:
        logger.error("Письмо %s не отправлено: %s", email_id, exc)
        return False
    return True
