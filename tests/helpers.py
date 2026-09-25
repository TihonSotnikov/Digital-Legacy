"""Вспомогательные функции тестов (4.4)."""

from __future__ import annotations

import re
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock, crypto
from app.models import AuditEvent, Heir, HeirKey, InheritanceRequest, User, heir_record_access
from app.security import hash_password

DEFAULT_PASSWORD = "correct-horse-battery"


# --- Прямая подготовка состояния в БД ---------------------------------------------


def make_user(
    db: Session,
    email: str = "owner@example.com",
    last_name: str = "Смирнова",
    first_name: str = "Анна",
    middle_name: str | None = "Сергеевна",
    verified: bool = True,
    password: str = DEFAULT_PASSWORD,
) -> User:
    now = clock.now()
    user = User(
        id=uuid.uuid4(),
        email=email,
        password_hash=hash_password(password),
        last_name=last_name,
        first_name=first_name,
        middle_name=middle_name,
        email_verified_at=now if verified else None,
        last_login_at=now,
        created_at=now,
    )
    db.add(user)
    db.commit()
    return user


def make_heir(db: Session, user: User, name: str = "Иван", record_ids: list[uuid.UUID] = ()) -> Heir:
    heir = Heir(id=uuid.uuid4(), user_id=user.id, name=name, created_at=clock.now())
    db.add(heir)
    db.flush()
    for record_id in record_ids:
        db.execute(heir_record_access.insert().values(heir_id=heir.id, record_id=record_id))
    db.commit()
    return heir


def make_key(db: Session, heir: Heir) -> tuple[HeirKey, str]:
    key = crypto.generate_heir_key()
    heir_key = HeirKey(
        id=uuid.uuid4(), heir_id=heir.id, key_hash=crypto.hash_heir_key(key), created_at=clock.now()
    )
    db.add(heir_key)
    db.commit()
    return heir_key, key


def status_fields(status: str) -> dict[str, Any]:
    """Поля, обязательные для статуса по CHECK-ограничениям 2.3.1."""
    now = clock.now()
    if status == "WAITING_CANCELLATION":
        return {"notified_at": now, "waiting_until": now + timedelta(days=14)}
    if status == "RELEASED":
        return {"notified_at": now, "waiting_until": now, "reminder_sent_at": now, "released_at": now}
    if status == "CANCELLED":
        return {"cancelled_at": now}
    if status == "REJECTED":
        return {"rejected_at": now, "check_result": {"accepted": False, "reasons": ["NAME_MISMATCH"]}}
    if status == "DOCUMENT_ACCEPTED":
        return {"check_result": {"accepted": True, "reasons": []}}
    return {}


def make_request(
    db: Session, heir: Heir, heir_key: HeirKey, status: str = "PENDING_REVIEW", **fields: Any
) -> InheritanceRequest:
    now = clock.now()
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "heir_id": heir.id,
        "heir_key_id": heir_key.id,
        "status": status,
        "doc_mime_type": "image/png",
        "doc_stored": status == "PENDING_REVIEW",
        "ocr_attempts": 0,
        "created_at": now,
        "updated_at": now,
    }
    values.update(status_fields(status))
    values.update(fields)
    request = InheritanceRequest(**values)
    db.add(request)
    db.commit()
    return request


def events(db: Session, event_type: str | None = None) -> list[AuditEvent]:
    db.expire_all()
    stmt = select(AuditEvent).order_by(AuditEvent.id)
    if event_type:
        stmt = stmt.where(AuditEvent.event_type == event_type)
    return list(db.execute(stmt).scalars())


def reload(db: Session, obj):
    db.expire_all()
    return db.get(type(obj), obj.id)


# --- HTML --------------------------------------------------------------------------

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def csrf_from(html: str) -> str:
    match = CSRF_RE.search(html)
    assert match, "csrf_token не найден на странице"
    return match.group(1)


def error_codes(html: str) -> list[str]:
    return re.findall(r'data-error-code="([A-Z_]+)"', html)


# --- HTTP-сценарии ------------------------------------------------------------------------

KEY_RE = re.compile(r'<code class="key-display" id="heir-key">([^<]+)</code>')


def page_csrf(client, path: str = "/") -> str:
    return csrf_from(client.get(path).text)


def register(
    client,
    email: str = "owner@example.com",
    password: str = DEFAULT_PASSWORD,
    last_name: str = "Смирнова",
    first_name: str = "Анна",
    middle_name: str | None = "Сергеевна",
    consent: bool = True,
    password_confirm: str | None = None,
):
    data = {
        "csrf_token": page_csrf(client, "/register"),
        "email": email,
        "password": password,
        "password_confirm": password if password_confirm is None else password_confirm,
        "last_name": last_name,
        "first_name": first_name,
        "middle_name": middle_name or "",
    }
    if consent:
        data["consent"] = "on"
    return client.post("/register", data=data)


def verification_path(mail, email: str) -> str:
    for message in reversed(mail.outbox):
        if message.email_id == "E1" and message.to == email:
            match = re.search(r"https?://[^/\s]+(/verify-email/\S+)", message.body)
            assert match, "в письме E1 нет ссылки подтверждения"
            return match.group(1)
    raise AssertionError(f"письмо E1 для {email} не найдено")


def login(client, email: str, password: str = DEFAULT_PASSWORD, next_url: str = ""):
    data = {"csrf_token": page_csrf(client, "/login"), "email": email, "password": password, "next": next_url}
    return client.post("/login", data=data)


def signup(client, mail, email: str = "owner@example.com", verify: bool = True, **names):
    """Регистрация (вход выполняется автоматически) и, по умолчанию, подтверждение email."""
    response = register(client, email=email, **names)
    assert response.status_code == 303, response.text
    if verify:
        assert client.get(verification_path(mail, email.lower())).status_code == 303
    return client


def record_id_by_title(title: str, email: str | None = None) -> uuid.UUID:
    from app.db import new_session
    from app.models import Record

    with new_session() as session:
        stmt = select(Record.id).where(Record.title == title).order_by(Record.created_at.desc())
        if email:
            stmt = stmt.join(User, User.id == Record.user_id).where(User.email == email)
        return session.execute(stmt).scalars().first()


def add_text_record(client, title: str, content: str, email: str | None = None) -> uuid.UUID:
    data = {"csrf_token": page_csrf(client, "/vault"), "type": "text", "title": title, "content": content}
    response = client.post("/records", data=data)
    assert response.status_code == 303, response.text
    return record_id_by_title(title, email)


def post_file(client, title: str, filename: str, data: bytes, mime: str = "application/octet-stream"):
    form = {"csrf_token": page_csrf(client, "/vault"), "type": "file", "title": title}
    return client.post("/records", data=form, files={"file": (filename, data, mime)})


def add_file_record(client, title: str, filename: str, data: bytes, email: str | None = None) -> uuid.UUID:
    response = post_file(client, title, filename, data)
    assert response.status_code == 303, response.text
    return record_id_by_title(title, email)


def key_from_page(html: str) -> str | None:
    match = KEY_RE.search(html)
    return match.group(1) if match else None


def add_heir(client, name: str = "Иван", record_ids=()) -> tuple[uuid.UUID, str]:
    """Создание наследника; ключ берётся со страницы однократного показа /heirs/{id}/key."""
    data = {"csrf_token": page_csrf(client, "/heirs"), "name": name, "record_ids": [str(r) for r in record_ids]}
    response = client.post("/heirs", data=data)
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    heir_id = uuid.UUID(location.split("/")[2])
    key = key_from_page(client.get(location).text)
    assert key is not None
    return heir_id, key.replace(" ", "")


def heir_login(client, key: str):
    data = {"csrf_token": page_csrf(client, "/heir"), "key": key}
    return client.post("/heir/login", data=data)


def session_data(client) -> dict:
    """Содержимое подписанной cookie сессии Starlette (для проверок в тестах)."""
    import base64
    import json

    from itsdangerous import TimestampSigner

    from app.config import get_settings

    raw = client.cookies.get("session")
    if not raw:
        return {}
    payload = TimestampSigner(get_settings().SECRET_KEY).unsign(raw.encode())
    return json.loads(base64.b64decode(payload))
