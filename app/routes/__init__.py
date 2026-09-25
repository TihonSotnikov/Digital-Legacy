"""Общие элементы веб-слоя: шаблоны, коды ошибок (3.1.3), обработчики ошибок."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, select_autoescape
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import security

logger = logging.getLogger("app.web")

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
jinja_env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)
templates = Jinja2Templates(env=jinja_env)

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

# Код → (HTTP-статус, сообщение пользователю), 3.1.3.
ERRORS: dict[str, tuple[int, str]] = {
    "VALIDATION_ERROR": (400, "Проверьте введённые данные"),
    "EMAIL_TAKEN": (400, "Этот email уже зарегистрирован"),
    "INVALID_CREDENTIALS": (401, "Неверный email или пароль"),
    "INVALID_KEY": (401, "Ключ не найден или недействителен"),
    "INVALID_TOKEN": (400, "Ссылка недействительна или устарела"),
    "CSRF_FAILED": (403, "Сессия устарела. Обновите страницу и повторите действие"),
    "EMAIL_NOT_VERIFIED": (403, "Подтвердите email, чтобы добавить наследника"),
    "NOT_FOUND": (404, "Страница не найдена"),
    "LIMIT_EXCEEDED": (409, "Достигнут лимит"),
    "ACTIVE_REQUEST_EXISTS": (409, "Запрос уже подан и обрабатывается"),
    "ALREADY_RELEASED": (409, "Доступ уже открыт"),
    "HEIR_HAS_ACTIVE_REQUEST": (409, "У наследника есть активный запрос. Сначала отмените его"),
    "PROFILE_LOCKED": (409, "ФИО нельзя изменить, пока есть активный запрос"),
    "INVALID_TRANSITION": (409, "Статус запроса уже изменился. Обновите страницу"),
    "FILE_TOO_LARGE": (413, "Файл слишком большой"),
    "UNSUPPORTED_FILE_TYPE": (415, "Недопустимый формат файла"),
    "RATE_LIMITED": (429, "Слишком много попыток. Повторите позже"),
    "REQUEST_LIMIT_EXCEEDED": (429, "Превышено число попыток за сутки. Повторите позже"),
    "EMAIL_SEND_FAILED": (200, "Не удалось отправить письмо. Попробуйте позже"),
    "INTERNAL_ERROR": (500, "Что-то пошло не так. Попробуйте позже"),
}

ERROR_TITLES = {
    400: "Некорректный запрос",
    401: "Доступ запрещён",
    403: "Доступ запрещён",
    404: "Страница не найдена",
    409: "Действие невозможно",
    413: "Файл слишком большой",
    415: "Недопустимый формат файла",
    429: "Слишком много попыток",
    500: "Ошибка сервера",
}


class AppError(Exception):
    """Ошибка бизнес-правила: отдельная страница ошибки с data-error-code."""

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        self.status_code, default_message = ERRORS[code]
        self.message = message or default_message
        super().__init__(code)


def error_message(code: str) -> str:
    return ERRORS[code][1]


def area_home(request: Request) -> str:
    if getattr(request.state, "owner", None) is not None:
        return "/vault"
    if getattr(request.state, "heir", None) is not None:
        return "/heir/portal"
    return "/"


def base_context(request: Request) -> dict[str, Any]:
    has_session = "session" in request.scope
    return {
        "csrf_token": security.ensure_csrf(request) if has_session else "",
        "flashes": security.pop_flashes(request) if has_session else [],
        "owner": getattr(request.state, "owner", None),
        "heir_ctx": getattr(request.state, "heir", None),
        "banners": [],
    }


def render(
    request: Request,
    template: str,
    context: dict[str, Any] | None = None,
    status_code: int = 200,
    no_store: bool = False,
) -> HTMLResponse:
    ctx = base_context(request)
    extra_banners = getattr(request.state, "banners_factory", None)
    if extra_banners is not None:
        ctx["banners"] = extra_banners()
    if context:
        ctx.update(context)
    response = templates.TemplateResponse(request, template, ctx, status_code=status_code)
    if no_store:
        response.headers["Cache-Control"] = "no-store"
    return response


def render_error(request: Request, code: str, message: str | None = None) -> HTMLResponse:
    status_code, default_message = ERRORS[code]
    ctx = base_context(request)
    ctx.update(
        {
            "code": code,
            "status": status_code,
            "title": ERROR_TITLES.get(status_code, "Ошибка"),
            "message": message or default_message,
            "home_url": area_home(request),
        }
    )
    response = templates.TemplateResponse(request, "error.html", ctx, status_code=status_code)
    for name, value in SECURITY_HEADERS.items():
        response.headers[name] = value
    return response


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError):
        return render_error(request, exc.code, exc.message)

    @app.exception_handler(RateLimitExceeded)
    async def _rate_limited(request: Request, exc: RateLimitExceeded):
        return render_error(request, "RATE_LIMITED")

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        return render_error(request, "VALIDATION_ERROR")

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404:
            return render_error(request, "NOT_FOUND")
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(Exception)
    async def _internal(request: Request, exc: Exception):
        logger.error("Необработанная ошибка: %s", type(exc).__name__)
        return render_error(request, "INTERNAL_ERROR")
