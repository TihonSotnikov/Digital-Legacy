"""AT-22: гонка отмены и выдачи."""

from __future__ import annotations

import threading
from datetime import timedelta

from sqlalchemy import select

from app import clock, worker
from app.db import new_session
from app.models import Heir, HeirKey
from app.transitions import CANCELLABLE, TransitionConflict, transition
from tests.helpers import add_heir, error_codes, make_key, make_request, page_csrf, reload, signup


def due_request(db, heir_id):
    """Запрос с наступившим waiting_until и отправленным напоминанием."""
    heir_key = db.execute(
        select(HeirKey).where(HeirKey.heir_id == heir_id, HeirKey.revoked_at.is_(None))
    ).scalar_one()
    now = clock.now()
    request = make_request(
        db,
        db.get(Heir, heir_id),
        heir_key,
        status="WAITING_CANCELLATION",
        notified_at=now - timedelta(days=14),
        reminder_sent_at=now - timedelta(days=3),
        waiting_until=now - timedelta(seconds=1),
    )
    return request, heir_key


def test_at22_cancel_before_tick_wins(make_client, mail, db):
    owner = signup(make_client(), mail)
    heir_id, _ = add_heir(owner, "Пётр")
    request, heir_key = due_request(db, heir_id)
    response = owner.post(f"/requests/{request.id}/cancel", data={"csrf_token": page_csrf(owner, "/requests")})
    assert response.status_code == 303
    worker.run_tick()
    assert reload(db, request).status == "CANCELLED"
    assert reload(db, heir_key).revoked_at is not None


def test_at22_tick_before_cancel_releases_and_cancel_conflicts(make_client, mail, db):
    owner = signup(make_client(), mail)
    heir_id, _ = add_heir(owner, "Пётр")
    request, heir_key = due_request(db, heir_id)
    worker.run_tick()
    assert reload(db, request).status == "RELEASED"
    response = owner.post(f"/requests/{request.id}/cancel", data={"csrf_token": page_csrf(owner, "/requests")})
    assert response.status_code == 409
    assert error_codes(response.text) == ["INVALID_TRANSITION"]
    assert reload(db, request).status == "RELEASED"
    assert reload(db, heir_key).revoked_at is None


def test_at22_parallel_release_and_cancel_exactly_one_succeeds(make_client, mail, db):
    owner = signup(make_client(), mail)
    heir_id, _ = add_heir(owner, "Пётр")
    outcomes = set()
    for _ in range(20):
        request, heir_key = due_request(db, heir_id)
        barrier = threading.Barrier(2)
        results: dict[str, str] = {}

        def run(name: str, sources, target: str, fields: dict) -> None:
            session = new_session()
            try:
                barrier.wait()
                transition(session, request.id, sources, target, **fields)
                session.commit()
                results[name] = "ok"
            except TransitionConflict:
                session.rollback()
                results[name] = "conflict"
            finally:
                session.close()

        now = clock.now()
        threads = [
            threading.Thread(target=run, args=("release", {"WAITING_CANCELLATION"}, "RELEASED", {"released_at": now})),
            threading.Thread(target=run, args=("cancel", CANCELLABLE, "CANCELLED", {"cancelled_at": now})),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sorted(results.values()) == ["conflict", "ok"]
        status = reload(db, request).status
        revoked = reload(db, heir_key).revoked_at is not None
        if results["cancel"] == "ok":
            assert status == "CANCELLED" and revoked
            make_key(db, db.get(Heir, heir_id))  # новый ключ для следующего раунда
        else:
            assert status == "RELEASED" and not revoked
        outcomes.add(status)
    assert outcomes <= {"CANCELLED", "RELEASED"}
