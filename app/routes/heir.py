"""Портал наследника: вход по ключу (FR-08, 3.2.2, 3.3.3)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import crypto, security
from app.db import get_db
from app.models import HeirKey
from app.routes import HeirContext, redirect, render, require_heir, verify_csrf

router = APIRouter()


@router.get("/heir")
def heir_login_form(request: Request):
    return render(request, "heir_login.html")


@router.post("/heir/login")
@security.limiter.limit(security.HEIR_LOGIN_RATE_LIMIT)
def heir_login(request: Request, db: Session = Depends(get_db), csrf_token: str = Form(""), key: str = Form("")):
    verify_csrf(request, csrf_token)
    normalized = crypto.normalize_heir_key(key)  # неверный формат — без обращения к БД
    heir_key = None
    if normalized is not None:
        heir_key = db.execute(
            select(HeirKey).where(
                HeirKey.key_hash == crypto.hash_heir_key(normalized), HeirKey.revoked_at.is_(None)
            )
        ).scalar_one_or_none()
    if heir_key is None:
        alert = {"code": "INVALID_KEY", "message": "Ключ не найден или недействителен"}
        return render(request, "heir_login.html", {"alert": alert}, status_code=401)
    security.login_heir(request, heir_key.id)
    return redirect("/heir/portal")


@router.post("/heir/logout")
def heir_logout(request: Request, heir: HeirContext = Depends(require_heir), csrf_token: str = Form("")):
    verify_csrf(request, csrf_token)
    security.clear_heir_session(request)
    return redirect("/heir")
