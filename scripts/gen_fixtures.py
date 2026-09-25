"""Синтетические свидетельства о смерти с вымышленными данными (4.4).

Используются тестами с маркером ocr и ручной проверкой (4.6). Файлы создаются во временном
каталоге; бинарные фикстуры в репозиторий не добавляются.

Пример:
    python scripts/gen_fixtures.py --out /tmp/fixtures --last Смирнова --first Анна --middle Сергеевна
"""

from __future__ import annotations

import argparse
import os
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
)
PAGE_SIZE = (1600, 2200)
PDF_DPI = 200
REGISTRY_PLACE = "Отдел ЗАГС Центрального района"


def _font(size: int) -> ImageFont.FreeTypeFont:
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size)
    raise FileNotFoundError("Не найден шрифт DejaVu Sans (пакет fonts-dejavu-core)")


def certificate_lines(
    last_name: str,
    first_name: str,
    middle_name: str | None,
    death_date: date,
    issue_date: date,
    series: str,
    number: str,
) -> list[tuple[str, int]]:
    """Строки документа и размер шрифта для каждой."""
    full_name = " ".join(part for part in (first_name, middle_name) if part)
    return [
        ("РОССИЙСКАЯ ФЕДЕРАЦИЯ", 60),
        ("СВИДЕТЕЛЬСТВО О СМЕРТИ", 72),
        (last_name, 64),
        (full_name, 64),
        (f"умер(ла) {death_date:%d.%m.%Y}", 56),
        ("Место государственной регистрации:", 50),
        (REGISTRY_PLACE, 50),
        (f"Дата выдачи {issue_date:%d.%m.%Y}", 56),
        (f"{series} № {number}", 64),
    ]


def make_certificate(
    path: str | Path,
    last_name: str,
    first_name: str,
    middle_name: str | None,
    death_date: date,
    issue_date: date,
    series: str = "I-ЕТ",
    number: str = "123456",
    fmt: str = "png",
) -> Path:
    """Рисует свидетельство на белом фоне шрифтом DejaVu Sans; fmt — "png" или "pdf"."""
    if fmt not in ("png", "pdf"):
        raise ValueError("fmt должен быть png или pdf")
    image = Image.new("RGB", PAGE_SIZE, "white")
    draw = ImageDraw.Draw(image)
    y = 180
    for text, size in certificate_lines(last_name, first_name, middle_name, death_date, issue_date, series, number):
        font = _font(size)
        width = draw.textlength(text, font=font)
        draw.text(((PAGE_SIZE[0] - width) / 2, y), text, fill="black", font=font)
        y += int(size * 2.1)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "png":
        image.save(path, "PNG")
    else:
        image.save(path, "PDF", resolution=PDF_DPI)
    return path


def display_today() -> date:
    """Сегодняшняя дата в часовом поясе правил проверки (DISPLAY_TZ, 3.2.8)."""
    return datetime.now(ZoneInfo(os.environ.get("DISPLAY_TZ", "Europe/Moscow"))).date()


def main() -> None:
    parser = argparse.ArgumentParser(description="Синтетическое свидетельство о смерти (PNG и PDF)")
    parser.add_argument("--out", required=True, help="каталог для файлов")
    parser.add_argument("--last", required=True, help="фамилия")
    parser.add_argument("--first", required=True, help="имя")
    parser.add_argument("--middle", default="", help="отчество (если есть)")
    parser.add_argument("--death-date", type=date.fromisoformat, default=display_today(), help="ГГГГ-ММ-ДД")
    parser.add_argument("--issue-date", type=date.fromisoformat, default=display_today(), help="ГГГГ-ММ-ДД")
    parser.add_argument("--series", default="I-ЕТ")
    parser.add_argument("--number", default="123456")
    args = parser.parse_args()

    out = Path(args.out)
    for fmt in ("png", "pdf"):
        path = make_certificate(
            out / f"certificate.{fmt}",
            args.last,
            args.first,
            args.middle or None,
            args.death_date,
            args.issue_date,
            args.series,
            args.number,
            fmt,
        )
        print(path)


if __name__ == "__main__":
    main()
