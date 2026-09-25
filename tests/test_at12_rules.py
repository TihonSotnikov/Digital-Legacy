"""AT-12: правила проверки (unit, параметризованный)."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from app import clock
from app.config import get_settings
from app.rules import OwnerNames, check

OWNER = OwnerNames("Смирнова", "Анна", "Сергеевна")
REGISTRY_LINE = "Место государственной регистрации: Отдел ЗАГС Центрального района"
SERIES_LINE = "I-ЕТ № 123456"


def today() -> date:
    return clock.now().astimezone(get_settings().display_tz).date()


def fmt(value: date) -> str:
    return f"{value:%d.%m.%Y}"


def document(
    title: str | None = "СВИДЕТЕЛЬСТВО О СМЕРТИ",
    names: tuple[str, ...] = ("Смирнова", "Анна Сергеевна"),
    death: str | None = "default",
    issue: str | None = "default",
    registry: str | None = REGISTRY_LINE,
    series: str | None = SERIES_LINE,
) -> str:
    """Текст документа; даты относительно app.clock: после последнего входа и не позже сегодня."""
    death = fmt(today() - timedelta(days=10)) if death == "default" else death
    issue = fmt(today() - timedelta(days=5)) if issue == "default" else issue
    lines = [
        "РОССИЙСКАЯ ФЕДЕРАЦИЯ",
        title,
        *names,
        f"умер(ла) {death}" if death else None,
        registry,
        f"Дата выдачи {issue}" if issue else None,
        series,
    ]
    return "\n".join(line for line in lines if line)


def last_login() -> date:
    return today() - timedelta(days=20)


ACCEPTED = {
    "полный корректный текст": (OWNER, lambda: document()),
    "ошибка OCR в одной букве фамилии": (OWNER, lambda: document(names=("Смирнава", "Анна Сергеевна"))),
    "латинские двойники в заголовке и ФИО": (
        OWNER,
        lambda: document(title="CBИДETEЛЬCTBO O CMEPTИ", names=("CMИPHOBA", "AHHA CEPГEEBHA")),
    ),
    "двойная фамилия через дефис": (
        OwnerNames("Римская-Корсакова", "Анна", "Сергеевна"),
        lambda: document(names=("Римская-Корсакова", "Анна Сергеевна")),
    ),
    "Владелец без отчества": (OwnerNames("Смирнова", "Анна"), lambda: document(names=("Смирнова", "Анна"))),
    "только серия и номер": (OWNER, lambda: document(registry=None)),
    "только ЗАГС": (OWNER, lambda: document(series=None)),
}


@pytest.mark.parametrize("case", list(ACCEPTED))
def test_at12_accepted(case):
    owner, make_text = ACCEPTED[case]
    result = check(make_text(), owner, today(), last_login())
    assert result.accepted, (case, result.reasons, result.flags)
    assert result.reasons == []


def test_at12_accepted_flags_for_registry_variants():
    only_series = check(document(registry=None), OWNER, today(), last_login())
    assert only_series.flags["series_number"] and not only_series.flags["registry"]
    only_registry = check(document(series=None), OWNER, today(), last_login())
    assert only_registry.flags["registry"] and not only_registry.flags["series_number"]


REJECTED = {
    "нет заголовка": (OWNER, lambda: document(title=None), None, ["TITLE_NOT_FOUND"]),
    "другая фамилия": (OWNER, lambda: document(names=("Петрова", "Анна Сергеевна")), None, ["NAME_MISMATCH"]),
    "отсутствует часть двойной фамилии": (
        OwnerNames("Римская-Корсакова", "Анна", "Сергеевна"),
        lambda: document(names=("Корсакова", "Анна Сергеевна")),
        None,
        ["NAME_MISMATCH"],
    ),
    "имя Ян при токене ЯНА": (
        OwnerNames("Смирнов", "Ян", "Сергеевич"),
        lambda: document(names=("Смирнов", "Яна Сергеевич")),
        None,
        ["NAME_MISMATCH"],
    ),
    "нет дат": (OWNER, lambda: document(death=None, issue=None), None, ["DATE_NOT_FOUND"]),
    "только будущая дата": (
        OWNER,
        lambda: document(death=fmt(today() + timedelta(days=1)), issue=None),
        None,
        ["DATE_NOT_FOUND"],
    ),
    "только некорректная дата 31.02.2020": (
        OWNER,
        lambda: document(death="31.02.2020", issue=None),
        None,
        ["DATE_NOT_FOUND"],
    ),
    "нет ни серии и номера, ни ЗАГС": (
        OWNER,
        lambda: document(registry="Место государственной регистрации: Отдел Центрального района", series=None),
        None,
        ["REGISTRY_DETAILS_NOT_FOUND"],
    ),
    "последний вход позже самой поздней даты": (
        OWNER,
        lambda: document(),
        lambda: today() - timedelta(days=4),
        ["OWNER_ACTIVE_AFTER_DOCUMENT"],
    ),
}


@pytest.mark.parametrize("case", list(REJECTED))
def test_at12_rejected(case):
    owner, make_text, login_date, expected = REJECTED[case]
    result = check(make_text(), owner, today(), login_date() if login_date else last_login())
    assert not result.accepted
    assert result.reasons == expected, (case, result.flags)


def test_at12_several_violations_in_fixed_order():
    text = document(title=None, names=("Петрова", "Мария"), death=None, issue=None, registry=None, series=None)
    result = check(text, OWNER, today(), last_login())
    assert result.reasons == ["TITLE_NOT_FOUND", "NAME_MISMATCH", "DATE_NOT_FOUND", "REGISTRY_DETAILS_NOT_FOUND"]

    result = check(document(title=None), OWNER, today(), today())
    assert result.reasons == ["TITLE_NOT_FOUND", "OWNER_ACTIVE_AFTER_DOCUMENT"]


def test_at12_latest_valid_date_is_used_for_activity_check():
    # Самая поздняя корректная дата — дата выдачи; вход в день выдачи противоречием не считается.
    issue = today() - timedelta(days=5)
    assert check(document(), OWNER, today(), issue).accepted
    assert check(document(), OWNER, today(), issue + timedelta(days=1)).reasons == ["OWNER_ACTIVE_AFTER_DOCUMENT"]
    # Без даты последнего входа противоречия нет.
    assert check(document(), OWNER, today(), None).accepted


def test_at12_check_result_contains_no_text_or_names():
    text = document(names=("Петрова", "Анна Сергеевна"))
    result = check(text, OWNER, today(), last_login()).to_dict()
    assert set(result) == {"accepted", "reasons", "flags", "ocr_chars"}
    assert set(result["flags"]) == {"title", "name", "date", "series_number", "registry", "owner_active_after_document"}
    assert result["ocr_chars"] == len(text)
    dumped = json.dumps(result, ensure_ascii=False).upper()
    for fragment in ("СМИРНОВА", "ПЕТРОВА", "АННА", "СЕРГЕЕВНА", "СВИДЕТЕЛЬСТВО", "ЗАГС", "123456", str(today().year)):
        assert fragment not in dumped


def test_at12_threshold_from_configuration(monkeypatch):
    text = document(names=("Смирнава", "Анна Сергеевна"))  # ratio 87.5
    assert check(text, OWNER, today(), last_login()).accepted
    monkeypatch.setattr(get_settings(), "RULE_FUZZY_THRESHOLD", 90)
    assert check(text, OWNER, today(), last_login()).reasons == ["NAME_MISMATCH"]
