"""AT-17: отклонение и повторная подача."""

from __future__ import annotations

from sqlalchemy import select

from app import clock, rules, storage, worker
from app.models import InheritanceRequest
from tests.helpers import (
    add_heir,
    certificate_text,
    error_codes,
    heir_login,
    only_request,
    signup,
    submit_document,
)


def test_at17_rejected_with_name_mismatch_then_resubmitted(make_client, mail, db, ocr):
    owner = signup(make_client(), mail)
    _, key = add_heir(owner, "Пётр")
    heir = make_client()
    heir_login(heir, key)
    submit_document(heir)
    first_id = only_request(db).id

    ocr.push(certificate_text(last="Петрова"))
    worker.run_tick()
    request = only_request(db)
    assert request.status == "REJECTED"
    assert request.rejected_at == clock.now()
    assert request.check_result["accepted"] is False
    assert request.check_result["reasons"] == ["NAME_MISMATCH"]
    assert request.check_result["flags"]["name"] is False
    assert request.doc_stored is False
    assert not storage.doc_path(request.id).exists()

    portal = heir.get("/heir/portal").text
    assert "Документ не прошёл проверку:" in portal
    assert rules.REASON_MESSAGES["NAME_MISMATCH"] in portal
    assert error_codes(portal) == ["NAME_MISMATCH"]
    assert rules.PHOTO_ADVICE in portal
    assert "Загрузить другой документ" in portal and 'action="/heir/request"' in portal

    clock.advance(60)
    assert submit_document(heir).status_code == 303
    db.expire_all()
    requests = db.execute(select(InheritanceRequest).order_by(InheritanceRequest.created_at)).scalars().all()
    assert len(requests) == 2
    new = next(r for r in requests if r.id != first_id)
    assert new.status == "PENDING_REVIEW" and new.heir_key_id == request.heir_key_id
    assert "Документ на проверке" in heir.get("/heir/portal").text
