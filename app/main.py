"""Приложение FastAPI: middleware, заголовки безопасности, роутеры."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from starlette.datastructures import MutableHeaders
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import configure_logging, load_settings_or_exit
from app.routes import SECURITY_HEADERS, health, public, register_error_handlers
from app.security import limiter

STATIC_DIR = Path(__file__).resolve().parent / "static"


class SecurityHeadersMiddleware:
    """Заголовки безопасности (1.1.3) на всех ответах."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


def create_app() -> FastAPI:
    settings = load_settings_or_exit()
    configure_logging()

    app = FastAPI(title="Цифровое наследие", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.limiter = limiter
    register_error_handlers(app)

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.SECRET_KEY,
        session_cookie="session",
        max_age=settings.SESSION_TTL_HOURS * 3600,
        same_site="lax",
        https_only=settings.COOKIE_SECURE,
    )
    app.add_middleware(SecurityHeadersMiddleware)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    for module in (public, health):
        app.include_router(module.router)
    return app


app = create_app()
