"""Worker (3.2.6): цикл run_tick() → пауза WORKER_POLL_SECONDS."""

from __future__ import annotations

import logging
import signal
import threading

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app import clock
from app.config import configure_logging, load_settings_or_exit
from app.db import new_session
from app.models import SystemState

logger = logging.getLogger("app.worker")

HEARTBEAT_KEY = "worker_heartbeat"


def write_heartbeat(db: Session) -> None:
    now = clock.now()
    stmt = insert(SystemState).values(key=HEARTBEAT_KEY, value=now.isoformat(), updated_at=now)
    stmt = stmt.on_conflict_do_update(
        index_elements=[SystemState.key],
        set_={"value": stmt.excluded.value, "updated_at": stmt.excluded.updated_at},
    )
    db.execute(stmt)


def run_tick() -> None:
    with new_session() as db:
        write_heartbeat(db)
        db.commit()


def main() -> None:
    settings = load_settings_or_exit()
    configure_logging()

    stop = threading.Event()

    def request_stop(signum, frame) -> None:
        logger.info("Получен сигнал %s, завершение после текущего цикла", signum)
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    logger.info("Worker запущен")
    while not stop.is_set():
        try:
            run_tick()
        except Exception:
            logger.exception("Ошибка цикла Worker")
        stop.wait(settings.WORKER_POLL_SECONDS)
    logger.info("Worker остановлен")


if __name__ == "__main__":
    main()
