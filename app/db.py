"""Engine и сессии SQLAlchemy (синхронный режим)."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

_engine: Engine | None = None
_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        # hide_parameters: значения параметров не попадают в тексты исключений и логи.
        _engine = create_engine(
            get_settings().DATABASE_URL, pool_pre_ping=True, hide_parameters=True
        )
    return _engine


def new_session() -> Session:
    global _factory
    if _factory is None:
        _factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _factory()


def get_db() -> Iterator[Session]:
    db = new_session()
    try:
        yield db
    finally:
        db.close()


def dispose_engine() -> None:
    global _engine, _factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _factory = None
