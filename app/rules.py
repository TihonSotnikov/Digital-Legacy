"""Правила структурной проверки свидетельства о смерти (3.2.8).

Чистая функция check() без обращений к БД. Порог и регулярные выражения — константы
модуля (порог переопределяется переменной RULE_FUZZY_THRESHOLD) и подлежат калибровке (4.5).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from rapidfuzz import fuzz

DEFAULT_THRESHOLD = 85

TITLE = "СВИДЕТЕЛЬСТВО О СМЕРТИ"
REGISTRY_WORD = "ЗАГС"
REGISTRY_PHRASE = "ЗАПИСИ АКТОВ ГРАЖДАНСКОГО СОСТОЯНИЯ"

# R3: дата ДД.ММ.ГГГГ с разделителями . - / или пробел. Обёртка (?=(...)) находит и
# перекрывающиеся совпадения.
DATE_RE = re.compile(r"(?=((?<!\d)(\d{2})[.\-/ ](\d{2})[.\-/ ](\d{4})(?!\d)))")
# R4: серия (римские цифры) и номер бланка, например «I-ЕТ № 123456».
SERIES_NUMBER_RE = re.compile(r"(?<![A-ZА-Я0-9])[IVXLC1]{1,6}\s?-\s?[А-ЯA-Z]{2}\s?(?:№|N|NO)?\s?\d{6}(?!\d)")
MIN_DATE = date(1900, 1, 1)

LATIN_TWINS = str.maketrans("ABCEHKMOPTXY", "АВСЕНКМОРТХУ")
_WHITESPACE_RE = re.compile(r"\s+")
_NOT_ALLOWED_RE = re.compile(r"[^А-Я0-9 .\-№]")
_TOKEN_SPLIT_RE = re.compile(r"[ .\-]+")
_NAME_SPLIT_RE = re.compile(r"[ \-]+")

# Коды причин в фиксированном порядке и сообщения Наследнику.
REASON_MESSAGES = {
    "TITLE_NOT_FOUND": "Не удалось распознать заголовок „Свидетельство о смерти“",
    "NAME_MISMATCH": "ФИО в документе не совпадает с данными владельца",
    "DATE_NOT_FOUND": "Не удалось распознать даты в документе",
    "REGISTRY_DETAILS_NOT_FOUND": "Не удалось распознать серию и номер бланка или сведения об органе ЗАГС",
    "OWNER_ACTIVE_AFTER_DOCUMENT": "Сведения документа противоречат данным системы",
    "DOCUMENT_UNREADABLE": "Файл повреждён или не открывается",
    "OCR_UNAVAILABLE": (
        "Проверка временно недоступна. Загрузите документ повторно позже — "
        "эта попытка не учитывается в лимите"
    ),
}

PHOTO_ADVICE = "Сфотографируйте свидетельство целиком, при хорошем освещении, без бликов и обрезанных краёв"


@dataclass(frozen=True)
class OwnerNames:
    last_name: str
    first_name: str
    middle_name: str | None = None


@dataclass
class CheckResult:
    accepted: bool
    reasons: list[str]
    flags: dict[str, bool]
    ocr_chars: int
    latest_date: date | None = field(default=None, repr=False)  # только в памяти, в БД не пишется

    def to_dict(self) -> dict[str, Any]:
        """check_result: флаги и коды причин; текст, ФИО и даты не сохраняются."""
        return {
            "accepted": self.accepted,
            "reasons": list(self.reasons),
            "flags": dict(self.flags),
            "ocr_chars": self.ocr_chars,
        }


def normalize_raw(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text.upper()).strip()


def normalize_cyr(text_raw: str) -> str:
    value = text_raw.upper().replace("Ё", "Е").translate(LATIN_TWINS)
    value = _NOT_ALLOWED_RE.sub(" ", value)
    return _WHITESPACE_RE.sub(" ", value).strip()


def tokenize(text_cyr: str) -> list[str]:
    return [token for token in _TOKEN_SPLIT_RE.split(text_cyr) if token]


def name_subparts(names: OwnerNames) -> list[str]:
    parts = [names.last_name, names.first_name]
    if names.middle_name:
        parts.append(names.middle_name)
    subparts: list[str] = []
    for part in parts:
        normalized = normalize_cyr(normalize_raw(part))
        subparts += [piece for piece in _NAME_SPLIT_RE.split(normalized) if piece]
    return subparts


def valid_dates(text_raw: str, today: date) -> list[date]:
    found = []
    for match in DATE_RE.finditer(text_raw):
        day, month, year = int(match.group(2)), int(match.group(3)), int(match.group(4))
        try:
            value = date(year, month, day)
        except ValueError:
            continue
        if MIN_DATE <= value <= today:
            found.append(value)
    return found


def check(
    text: str,
    owner_names: OwnerNames,
    today: date,
    last_login_date: date | None,
    threshold: int | None = None,
) -> CheckResult:
    if threshold is None:
        from app.config import get_settings

        threshold = get_settings().RULE_FUZZY_THRESHOLD

    text_raw = normalize_raw(text)
    text_cyr = normalize_cyr(text_raw)
    tokens = tokenize(text_cyr)
    token_set = set(tokens)

    r1 = fuzz.partial_ratio(TITLE, text_cyr) >= threshold

    subparts = name_subparts(owner_names)
    r2 = bool(subparts) and all(
        (part in token_set)
        if len(part) <= 3
        else max((fuzz.ratio(part, token) for token in tokens), default=0) >= threshold
        for part in subparts
    )

    dates = valid_dates(text_raw, today)
    r3 = bool(dates)
    latest = max(dates) if dates else None

    r4 = SERIES_NUMBER_RE.search(text_raw) is not None
    r5 = any(fuzz.ratio(token, REGISTRY_WORD) >= threshold for token in tokens) or (
        fuzz.partial_ratio(REGISTRY_PHRASE, text_cyr) >= threshold
    )
    r6 = r3 and last_login_date is not None and last_login_date > latest

    reasons = []
    if not r1:
        reasons.append("TITLE_NOT_FOUND")
    if not r2:
        reasons.append("NAME_MISMATCH")
    if not r3:
        reasons.append("DATE_NOT_FOUND")
    if not r4 and not r5:
        reasons.append("REGISTRY_DETAILS_NOT_FOUND")
    if r6:
        reasons.append("OWNER_ACTIVE_AFTER_DOCUMENT")

    flags = {
        "title": r1,
        "name": r2,
        "date": r3,
        "series_number": r4,
        "registry": r5,
        "owner_active_after_document": r6,
    }
    accepted = r1 and r2 and r3 and (r4 or r5) and not r6
    return CheckResult(accepted=accepted, reasons=reasons, flags=flags, ocr_chars=len(text), latest_date=latest)
