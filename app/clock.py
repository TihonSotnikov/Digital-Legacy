"""Единый источник текущего времени (2.3.2). В тестах подменяется через set_now/advance."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

_frozen: datetime | None = None


def now() -> datetime:
    frozen = _frozen
    return frozen if frozen is not None else datetime.now(timezone.utc)


def set_now(value: datetime) -> datetime:
    global _frozen
    if value.tzinfo is None:
        raise ValueError("ожидается datetime с часовым поясом")
    _frozen = value.astimezone(timezone.utc)
    return _frozen


def advance(seconds: float) -> datetime:
    return set_now(now() + timedelta(seconds=seconds))


def reset() -> None:
    global _frozen
    _frozen = None
