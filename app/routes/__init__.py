"""Общие элементы веб-слоя: шаблоны, коды ошибок (3.1.3), обработчики ошибок,
зависимости аутентификации, CSRF, разбор загрузок, форматирование и метки статусов."""

from __future__ import annotations

import logging
import re
import urllib.parse
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, select_autoescape
from slowapi.errors import RateLimitExceeded
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData, UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import clock, security
from app.config import get_settings
from app.crypto import DecryptionError
from app.db import get_db, new_session
from app.models import ACTIVE_STATUSES, Heir, HeirKey, InheritanceRequest, User

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


class OwnerLoginRequired(Exception):
    pass


class HeirLoginRequired(Exception):
    def __init__(self, expired: bool):
        self.expired = expired
        super().__init__("heir login required")


def error_message(code: str) -> str:
    return ERRORS[code][1]


def limit_error(what: str, current: int | str, maximum: int | str) -> AppError:
    return AppError("LIMIT_EXCEEDED", f"Достигнут лимит: {what} ({current} из {maximum})")


def file_too_large(max_mb: int) -> AppError:
    return AppError("FILE_TOO_LARGE", f"Файл слишком большой. Максимальный размер — {max_mb} МБ")


def unsupported_file(allowed: str) -> AppError:
    return AppError("UNSUPPORTED_FILE_TYPE", f"Недопустимый формат файла. Допустимы: {allowed}")


# --- Рендеринг -----------------------------------------------------------------------


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
        "settings": get_settings(),
    }


def render(
    request: Request,
    template: str,
    context: dict[str, Any] | None = None,
    status_code: int = 200,
    no_store: bool = False,
) -> HTMLResponse:
    ctx = base_context(request)
    banners_factory = getattr(request.state, "banners_factory", None)
    if banners_factory is not None:
        ctx["banners"] = banners_factory()
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


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError):
        return render_error(request, exc.code, exc.message)

    @app.exception_handler(OwnerLoginRequired)
    async def _owner_login(request: Request, exc: OwnerLoginRequired):
        security.flash(request, "Сессия истекла, войдите снова", level="info")
        target = "/login"
        if request.method == "GET":
            path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            target += "?" + urllib.parse.urlencode({"next": path})
        return redirect(target)

    @app.exception_handler(HeirLoginRequired)
    async def _heir_login(request: Request, exc: HeirLoginRequired):
        if exc.expired:
            security.flash(request, "Сессия истекла, введите ключ снова", level="info")
        return redirect("/heir")

    @app.exception_handler(RateLimitExceeded)
    async def _rate_limited(request: Request, exc: RateLimitExceeded):
        return render_error(request, "RATE_LIMITED")

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        return render_error(request, "VALIDATION_ERROR")

    @app.exception_handler(DecryptionError)
    async def _decryption(request: Request, exc: DecryptionError):
        logger.error("Ошибка расшифрования данных (%s %s)", request.method, request.url.path)
        return render_error(request, "INTERNAL_ERROR")

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404:
            return render_error(request, "NOT_FOUND")
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(Exception)
    async def _internal(request: Request, exc: Exception):
        logger.error("Необработанная ошибка: %s", type(exc).__name__)
        return render_error(request, "INTERNAL_ERROR")


# --- Аутентификация и CSRF ---------------------------------------------------------


def verify_csrf(request: Request, submitted: str | None) -> None:
    if not security.csrf_valid(request, submitted):
        raise AppError("CSRF_FAILED")


def owner_banners(user_id: uuid.UUID, verified: bool) -> list[dict[str, Any]]:
    """Баннеры на всех страницах Владельца (3.3.1)."""
    banners: list[dict[str, Any]] = [] if verified else [{"kind": "unverified"}]
    with new_session() as db:
        names = db.execute(
            select(Heir.name)
            .join(InheritanceRequest, InheritanceRequest.heir_id == Heir.id)
            .where(Heir.user_id == user_id, InheritanceRequest.status.in_(ACTIVE_STATUSES))
            .order_by(InheritanceRequest.created_at)
        ).scalars()
        banners += [{"kind": "active_request", "heir_name": name} for name in names]
    return banners


def require_owner(request: Request, db: Session = Depends(get_db)) -> User:
    """Проверка сессии Владельца (3.2.4): пользователь с uid существует."""
    user_id = security.session_uuid(request, "uid")
    user = db.get(User, user_id) if user_id else None
    if user is None:
        if "uid" in request.session:
            request.session.pop("uid")
        raise OwnerLoginRequired()
    request.state.owner = user
    verified = user.email_verified_at is not None
    request.state.banners_factory = lambda: owner_banners(user.id, verified)
    return user


@dataclass
class HeirContext:
    heir: Heir
    key: HeirKey

    @property
    def heir_name(self) -> str:
        return self.heir.name


def require_heir(request: Request, db: Session = Depends(get_db)) -> HeirContext:
    """Проверка сессии Наследника (3.2.4): срок, ключ существует и не отозван."""
    key_id = security.session_uuid(request, "hkid")
    if key_id is None:
        expired = "hkid" in request.session
        security.clear_heir_session(request)
        raise HeirLoginRequired(expired=expired)
    auth_at = security.heir_auth_time(request)
    ttl = timedelta(minutes=get_settings().HEIR_SESSION_TTL_MINUTES)
    key = db.get(HeirKey, key_id)
    if auth_at is None or not auth_at + ttl > clock.now() or key is None or key.revoked_at is not None:
        security.clear_heir_session(request)
        raise HeirLoginRequired(expired=True)
    context = HeirContext(heir=db.get(Heir, key.heir_id), key=key)
    request.state.heir = context
    return context


def parse_id(value: str) -> uuid.UUID:
    """Идентификатор из пути; неверный формат неотличим от отсутствующего объекта."""
    try:
        return uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise AppError("NOT_FOUND") from None


# --- Загрузка файлов -----------------------------------------------------------------

MB = 1024 * 1024
MULTIPART_OVERHEAD = 64 * 1024


class BodyTooLarge(Exception):
    pass


async def read_form_limited(request: Request, max_file_bytes: int) -> FormData:
    """Форма с файлом читается потоково; при превышении лимита чтение прерывается."""
    limit = max_file_bytes + MULTIPART_OVERHEAD
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > limit:
        raise BodyTooLarge()
    received = 0
    upstream = request.receive

    async def receive():
        nonlocal received
        message = await upstream()
        if message["type"] == "http.request":
            received += len(message.get("body", b""))
            if received > limit:
                raise BodyTooLarge()
        return message

    limited = Request(request.scope, receive)
    return await limited.form(max_files=2, max_fields=50)


def form_str(form: FormData, name: str) -> str:
    value = form.get(name)
    return value if isinstance(value, str) else ""


def form_file(form: FormData, name: str) -> UploadFile | None:
    value = form.get(name)
    if isinstance(value, UploadFile) and (value.filename or "").strip():
        return value
    return None


@dataclass(frozen=True)
class FileKind:
    label: str
    mime: str
    signature: bytes | None  # None — проверка корректного UTF-8


FILE_KINDS = {
    ".jpg": FileKind("JPG", "image/jpeg", b"\xff\xd8\xff"),
    ".jpeg": FileKind("JPG", "image/jpeg", b"\xff\xd8\xff"),
    ".png": FileKind("PNG", "image/png", b"\x89PNG"),
    ".pdf": FileKind("PDF", "application/pdf", b"%PDF"),
    ".docx": FileKind("DOCX", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", b"PK\x03\x04"),
    ".xlsx": FileKind("XLSX", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", b"PK\x03\x04"),
    ".zip": FileKind("ZIP", "application/zip", b"PK\x03\x04"),
    ".txt": FileKind("TXT", "text/plain", None),
}
RECORD_EXTENSIONS = (".jpg", ".jpeg", ".png", ".pdf", ".docx", ".xlsx", ".zip", ".txt")
RECORD_TYPES_LABEL = "JPG, PNG, PDF, DOCX, XLSX, ZIP, TXT"
DOCUMENT_EXTENSIONS = (".jpg", ".jpeg", ".png", ".pdf")
DOCUMENT_TYPES_LABEL = "JPG, PNG, PDF"


def clean_filename(raw: str | None) -> str:
    name = re.split(r"[\\/]", raw or "")[-1]
    return "".join(ch for ch in name if ch.isprintable()).strip()


def detect_file_type(filename: str, data: bytes, allowed: tuple[str, ...]) -> str | None:
    """MIME-тип по расширению и сигнатуре (1.2.3) или None, если формат недопустим."""
    extension = Path(filename).suffix.lower()
    if extension not in allowed:
        return None
    kind = FILE_KINDS[extension]
    if kind.signature is None:
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            return None
        return kind.mime
    return kind.mime if data.startswith(kind.signature) else None


def attachment_headers(filename: str) -> dict[str, str]:
    fallback = re.sub(r"[^A-Za-z0-9._-]", "_", filename) or "file"
    quoted = urllib.parse.quote(filename, safe="")
    return {
        "Content-Disposition": f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quoted}",
        "Cache-Control": "no-store",
    }


# --- Проверка полей --------------------------------------------------------------------


def field_error(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def normalize_text_field(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def valid_name_part(value: str) -> bool:
    """ФИО: 1–100 символов — буквы, пробел, дефис (1.2.3)."""
    return (
        1 <= len(value) <= 100
        and all(ch.isalpha() or ch in " -" for ch in value)
        and any(ch.isalpha() for ch in value)
    )


def normalize_email(raw: str | None) -> str | None:
    from email_validator import EmailNotValidError, validate_email

    value = (raw or "").strip()
    if not value or len(value) > 254:
        return None
    try:
        result = validate_email(value, check_deliverability=False)
    except EmailNotValidError:
        return None
    return result.normalized.lower()


# --- Форматирование ----------------------------------------------------------------------


def to_display_tz(value: datetime) -> datetime:
    return value.astimezone(get_settings().display_tz)


def format_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    local = to_display_tz(value)
    return f"{local:%d.%m.%Y %H:%M} {local.tzname()}"


def format_date(value: datetime | None) -> str:
    if value is None:
        return ""
    return f"{to_display_tz(value):%d.%m.%Y}"


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size} Б"
    if size < MB:
        return f"{size / 1024:.1f} КБ".replace(".", ",")
    return f"{size / MB:.1f} МБ".replace(".", ",")


def format_mb(size: int) -> str:
    return f"{size / MB:.1f}".replace(".", ",")


def format_number(value: int) -> str:
    return f"{value:,}".replace(",", " ")


jinja_env.filters.update(
    datetime=format_datetime, date=format_date, size=format_size, mb=format_mb, number=format_number
)


# --- Метки статусов (3.3.6) --------------------------------------------------------------


def owner_status_label(request: InheritanceRequest) -> str:
    status = request.status
    if status == "PENDING_REVIEW":
        return "Документ на проверке"
    if status == "DOCUMENT_ACCEPTED":
        return "Документ принят, отправляется уведомление"
    if status == "WAITING_CANCELLATION":
        return f"Ожидание отмены до {format_datetime(request.waiting_until)}"
    if status == "RELEASED":
        return f"Доступ выдан {format_datetime(request.released_at)}"
    if status == "REJECTED":
        return "Отклонён"
    return "Отменён"


def heir_status_label(request: InheritanceRequest) -> str:
    status = request.status
    if status in ("PENDING_REVIEW", "DOCUMENT_ACCEPTED"):
        return "Документ на проверке"
    if status == "WAITING_CANCELLATION":
        return f"Документ принят, ожидание до {format_datetime(request.waiting_until)}"
    if status == "RELEASED":
        return "Доступ открыт"
    if status == "REJECTED":
        return "Документ не прошёл проверку"
    return ""


jinja_env.globals.update(owner_status_label=owner_status_label, heir_status_label=heir_status_label)
