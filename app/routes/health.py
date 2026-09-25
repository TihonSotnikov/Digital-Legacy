"""GET /healthz (3.1.5)."""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app import clock
from app.config import get_settings
from app.db import new_session
from app.models import SystemState

router = APIRouter()
logger = logging.getLogger("app.health")

HEARTBEAT_KEY = "worker_heartbeat"


@router.get("/healthz")
def healthz() -> JSONResponse:
    db_ok = False
    age: float | None = None
    try:
        with new_session() as db:
            state = db.get(SystemState, HEARTBEAT_KEY)
            db_ok = True
            if state is not None:
                age = max(0.0, (clock.now() - state.updated_at).total_seconds())
    except Exception as exc:  # БД недоступна
        logger.warning("healthz: БД недоступна (%s)", type(exc).__name__)

    healthy = db_ok and age is not None and age <= get_settings().WORKER_STALE_SECONDS
    body = {
        "status": "ok" if healthy else "degraded",
        "db": db_ok,
        "worker_heartbeat_age_seconds": int(age) if age is not None else None,
    }
    return JSONResponse(body, status_code=200 if healthy else 503)
