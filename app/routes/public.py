"""Публичные страницы: лендинг, условия использования, политика ПДн."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.routes import render

router = APIRouter()


@router.get("/")
def landing(request: Request):
    return render(request, "index.html")


@router.get("/terms")
def terms(request: Request):
    return render(request, "terms.html")


@router.get("/privacy")
def privacy(request: Request):
    return render(request, "privacy.html")
