"""AT-29: машина состояний (unit)."""

from __future__ import annotations

import threading
import uuid

import pytest

from app import clock
from app.db import new_session
from app.models import ALL_STATUSES, TERMINAL_STATUSES
from app.transitions import ALLOWED, CANCELLABLE, TransitionConflict, transition
from tests.helpers import events, make_heir, make_key, make_request, make_user, reload

FORBIDDEN_PAIRS = [
    (source, target)
    for source in sorted(ALL_STATUSES)
    for target in sorted(ALL_STATUSES)
    if (source, target) not in ALLOWED
]


@pytest.fixture
def setup(db):
    user = make_user(db)
    heir = make_heir(db, user)
    heir_key, _ = make_key(db, heir)
    return user, heir, heir_key


@pytest.mark.parametrize("source, target", FORBIDDEN_PAIRS)
def test_at29_pair_outside_allowed_raises_value_error(db, setup, source, target):
    _, heir, heir_key = setup
    request = make_request(db, heir, heir_key, status=source)
    with pytest.raises(ValueError):
        transition(db, request.id, {source}, target)
    # Недопустимая пара запрещена и в наборе вместе с допустимыми исходными статусами.
    allowed_sources = {s for s, t in ALLOWED if t == target}
    with pytest.raises(ValueError):
        transition(db, request.id, allowed_sources | {source}, target)
    db.rollback()
    assert reload(db, request).status == source
    assert events(db, "REQUEST_STATUS_CHANGED") == []


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATUSES))
def test_at29_transition_from_terminal_status_conflicts(db, setup, terminal):
    _, heir, heir_key = setup
    request = make_request(db, heir, heir_key, status=terminal)
    for source, target in sorted(ALLOWED):
        with pytest.raises(TransitionConflict):
            transition(db, request.id, {source}, target)
        db.rollback()
    with pytest.raises(TransitionConflict):
        transition(db, request.id, CANCELLABLE, "CANCELLED", cancelled_at=clock.now())
    db.rollback()
    assert reload(db, request).status == terminal
    assert reload(db, heir_key).revoked_at is None
    assert events(db) == []


def test_at29_unknown_request_conflicts(db, setup):
    with pytest.raises(TransitionConflict):
        transition(db, uuid.uuid4(), {"PENDING_REVIEW"}, "DOCUMENT_ACCEPTED")


@pytest.mark.parametrize("source", sorted(CANCELLABLE))
def test_at29_cancel_revokes_key(db, setup, source):
    user, heir, heir_key = setup
    request = make_request(db, heir, heir_key, status=source)
    now = clock.now()
    previous = transition(db, request.id, CANCELLABLE, "CANCELLED", cancelled_at=now)
    db.commit()
    assert previous == source
    request = reload(db, request)
    assert (request.status, request.cancelled_at, request.updated_at) == ("CANCELLED", now, now)
    assert reload(db, heir_key).revoked_at == now

    revoked = events(db, "HEIR_KEY_REVOKED")
    assert len(revoked) == 1
    assert revoked[0].user_id == user.id and revoked[0].request_id == request.id
    assert revoked[0].meta == {"heir_id": str(heir.id), "heir_key_id": str(heir_key.id), "reason": "cancel"}


def test_at29_status_changed_event_contains_from_and_to(db, setup):
    user, heir, heir_key = setup
    request = make_request(db, heir, heir_key)
    accepted = {"accepted": True, "reasons": [], "flags": {}, "ocr_chars": 10}
    assert transition(db, request.id, {"PENDING_REVIEW"}, "DOCUMENT_ACCEPTED", check_result=accepted) == "PENDING_REVIEW"
    now = clock.now()
    transition(db, request.id, {"DOCUMENT_ACCEPTED"}, "WAITING_CANCELLATION", notified_at=now, waiting_until=now)
    transition(db, request.id, {"WAITING_CANCELLATION"}, "RELEASED", released_at=now)
    db.commit()

    changes = events(db, "REQUEST_STATUS_CHANGED")
    assert [e.meta for e in changes] == [
        {"from": "PENDING_REVIEW", "to": "DOCUMENT_ACCEPTED"},
        {"from": "DOCUMENT_ACCEPTED", "to": "WAITING_CANCELLATION"},
        {"from": "WAITING_CANCELLATION", "to": "RELEASED"},
    ]
    assert all(e.user_id == user.id and e.request_id == request.id for e in changes)
    assert reload(db, heir_key).revoked_at is None  # ключ отзывается только при отмене
    assert events(db, "HEIR_KEY_REVOKED") == []


def test_at29_rejection_event_contains_reasons(db, setup):
    _, heir, heir_key = setup
    request = make_request(db, heir, heir_key)
    result = {"accepted": False, "reasons": ["TITLE_NOT_FOUND", "NAME_MISMATCH"]}
    transition(db, request.id, {"PENDING_REVIEW"}, "REJECTED", check_result=result, rejected_at=clock.now())
    db.commit()
    (event,) = events(db, "REQUEST_STATUS_CHANGED")
    assert event.meta == {"from": "PENDING_REVIEW", "to": "REJECTED", "reasons": ["TITLE_NOT_FOUND", "NAME_MISMATCH"]}


def test_transition_rejects_protected_fields(db, setup):
    _, heir, heir_key = setup
    request = make_request(db, heir, heir_key)
    with pytest.raises(ValueError):
        transition(db, request.id, {"PENDING_REVIEW"}, "DOCUMENT_ACCEPTED", status="RELEASED")
    with pytest.raises(ValueError):
        transition(db, request.id, {"PENDING_REVIEW"}, "DOCUMENT_ACCEPTED", no_such_column=1)


def test_transition_concurrent_calls_single_winner(db, setup):
    """Ранняя проверка конкурентности transition() (подробно — AT-22)."""
    _, heir, heir_key = setup
    for _ in range(10):
        request = make_request(
            db, heir, heir_key, status="WAITING_CANCELLATION", reminder_sent_at=clock.now()
        )
        barrier = threading.Barrier(2)
        results: dict[str, object] = {}

        def run(name: str, sources: set[str], target: str, fields: dict) -> None:
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
            threading.Thread(target=run, args=("cancel", set(CANCELLABLE), "CANCELLED", {"cancelled_at": now})),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(results.values()) == ["conflict", "ok"]
        final = reload(db, request).status
        key_revoked = reload(db, heir_key).revoked_at is not None
        assert (final == "CANCELLED") == (results["cancel"] == "ok") == key_revoked
        if key_revoked:  # следующий раунд — с новым ключом
            heir_key, _ = make_key(db, heir)
