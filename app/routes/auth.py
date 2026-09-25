"""Регистрация, вход, выход, подтверждение email (FR-01, FR-02)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import audit, clock, security, tokens
from app.config import get_settings
from app.db import get_db
from app.email.backends import EmailSendError, send_email
from app.models import User
from app.routes import (
    AppError,
    field_error,
    normalize_email,
    normalize_text_field,
    redirect,
    render,
    require_owner,
    valid_name_part,
    verify_csrf,
)

router = APIRouter()


def send_verification(user: User) -> bool:
    """Письмо E1. False — SMTP не принял письмо."""
    try:
        send_email(
            "E1",
            user.email,
            verify_url=tokens.verify_url(user.id),
            ttl_hours=get_settings().VERIFY_TOKEN_TTL_HOURS,
        )
    except EmailSendError:
        return False
    return True


def flash_email_result(request: Request, sent: bool) -> None:
    if sent:
        security.flash(request, "Письмо отправлено")
    else:
        security.flash(request, "Не удалось отправить письмо. Попробуйте позже", "error", "EMAIL_SEND_FAILED")


# --- Регистрация -------------------------------------------------------------------------


@router.get("/register")
def register_form(request: Request):
    return render(request, "register.html", {"values": {}, "errors": {}})


@router.post("/register")
@security.limiter.limit(security.REGISTER_RATE_LIMIT)
def register(
    request: Request,
    db: Session = Depends(get_db),
    csrf_token: str = Form(""),
    email: str = Form(""),
    password: str = Form(""),
    password_confirm: str = Form(""),
    last_name: str = Form(""),
    first_name: str = Form(""),
    middle_name: str = Form(""),
    consent: str = Form(""),
):
    verify_csrf(request, csrf_token)
    values = {
        "email": email.strip(),
        "last_name": normalize_text_field(last_name),
        "first_name": normalize_text_field(first_name),
        "middle_name": normalize_text_field(middle_name),
        "consent": bool(consent),
    }
    errors: dict[str, dict[str, str]] = {}
    normalized_email = normalize_email(email)
    if normalized_email is None:
        errors["email"] = field_error("VALIDATION_ERROR", "Введите корректный email")
    if len(password) < security.MIN_PASSWORD_LENGTH:
        errors["password"] = field_error(
            "VALIDATION_ERROR", "Пароль должен быть не короче 10 символов"
        )
    elif password != password_confirm:
        errors["password_confirm"] = field_error("VALIDATION_ERROR", "Пароли не совпадают")
    if not valid_name_part(values["last_name"]):
        errors["last_name"] = field_error(
            "VALIDATION_ERROR", "Укажите фамилию: от 1 до 100 символов — буквы, пробел, дефис"
        )
    if not valid_name_part(values["first_name"]):
        errors["first_name"] = field_error(
            "VALIDATION_ERROR", "Укажите имя: от 1 до 100 символов — буквы, пробел, дефис"
        )
    if values["middle_name"] and not valid_name_part(values["middle_name"]):
        errors["middle_name"] = field_error(
            "VALIDATION_ERROR", "Отчество: до 100 символов — буквы, пробел, дефис"
        )
    if not consent:
        errors["consent"] = field_error(
            "VALIDATION_ERROR",
            "Необходимо принять условия использования и дать согласие на обработку персональных данных",
        )
    if normalized_email and "email" not in errors:
        exists = db.execute(select(User.id).where(User.email == normalized_email)).first()
        if exists:
            errors["email"] = field_error("EMAIL_TAKEN", "Этот email уже зарегистрирован")
    if errors:
        return render(request, "register.html", {"values": values, "errors": errors}, status_code=400)

    now = clock.now()
    user = User(
        id=uuid.uuid4(),
        email=normalized_email,
        password_hash=security.hash_password(password),
        last_name=values["last_name"],
        first_name=values["first_name"],
        middle_name=values["middle_name"] or None,
        created_at=now,
        last_login_at=now,
    )
    db.add(user)
    try:
        db.flush()  # параллельная регистрация того же email — нарушение уникальности
    except IntegrityError:
        db.rollback()
        errors["email"] = field_error("EMAIL_TAKEN", "Этот email уже зарегистрирован")
        return render(request, "register.html", {"values": values, "errors": errors}, status_code=400)
    audit.log(db, user.id, "USER_REGISTERED")
    db.commit()

    security.login_owner(request, user.id)
    # Регистрация завершается и при недоставленном E1: flash с кнопкой повторной отправки.
    flash_email_result(request, send_verification(user))
    return redirect("/vault")


# --- Вход и выход ------------------------------------------------------------------------


@router.get("/login")
def login_form(request: Request, next: str = ""):
    return render(request, "login.html", {"values": {"next": security.safe_next(next) or ""}})


@router.post("/login")
@security.limiter.limit(security.LOGIN_RATE_LIMIT)
def login(
    request: Request,
    db: Session = Depends(get_db),
    csrf_token: str = Form(""),
    email: str = Form(""),
    password: str = Form(""),
    next: str = Form(""),
):
    verify_csrf(request, csrf_token)
    lookup = normalize_email(email) or email.strip().lower()
    user = db.execute(select(User).where(User.email == lookup)).scalar_one_or_none()
    if not security.verify_password(user.password_hash if user else None, password):
        if user is not None:
            audit.log(db, user.id, "LOGIN_FAILED")
            db.commit()
        context = {
            "values": {"email": email.strip(), "next": security.safe_next(next) or ""},
            "alert": {"code": "INVALID_CREDENTIALS", "message": "Неверный email или пароль"},
        }
        return render(request, "login.html", context, status_code=401)

    security.login_owner(request, user.id)
    user.last_login_at = clock.now()
    audit.log(db, user.id, "LOGIN_SUCCEEDED")
    db.commit()
    return redirect(security.safe_next(next) or "/vault")


@router.post("/logout")
def logout(request: Request, owner: User = Depends(require_owner), csrf_token: str = Form("")):
    verify_csrf(request, csrf_token)
    request.session.clear()
    return redirect("/")


# --- Подтверждение email -------------------------------------------------------------------


@router.get("/verify-email/{token}")
def verify_email(token: str, request: Request, db: Session = Depends(get_db)):
    user_id = tokens.read_verify_token(token)
    user = db.get(User, user_id) if user_id else None
    if user is None:
        raise AppError("INVALID_TOKEN")
    if user.email_verified_at is None:
        user.email_verified_at = clock.now()
        audit.log(db, user.id, "EMAIL_VERIFIED")
        db.commit()
    security.flash(request, "Email подтверждён")
    logged_in = security.session_uuid(request, "uid") is not None
    return redirect("/vault" if logged_in else "/login")


@router.post("/verify-email/resend")
@security.limiter.limit(security.VERIFY_RESEND_RATE_LIMIT, key_func=security.owner_rate_key)
def resend_verification(
    request: Request,
    owner: User = Depends(require_owner),
    csrf_token: str = Form(""),
    next: str = Form(""),
):
    verify_csrf(request, csrf_token)
    if owner.email_verified_at is None:
        flash_email_result(request, send_verification(owner))
    return redirect(security.safe_next(next) or "/vault")
