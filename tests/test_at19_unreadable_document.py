"""AT-19: нечитаемый документ."""

from __future__ import annotations

import pytest

from app import clock, storage, worker
from tests.helpers import add_heir, heir_login, only_request, signup, submit_document
from tests.test_stage6_prepare import EMPTY_PDF

BROKEN_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00 corrupted image data " * 20


@pytest.mark.parametrize(
    "filename, data, mime",
    [("broken.png", BROKEN_PNG, "image/png"), ("empty.pdf", EMPTY_PDF, "application/pdf")],
    ids=["png-corrupted-content", "pdf-without-pages"],
)
def test_at19_unreadable_document_rejected_without_ocr(make_client, mail, db, ocr, filename, data, mime):
    owner = signup(make_client(), mail)
    _, key = add_heir(owner, "Пётр")
    heir = make_client()
    heir_login(heir, key)
    assert submit_document(heir, data, filename, mime).status_code == 303  # сигнатура корректна

    ocr.push("не должно понадобиться")
    worker.run_tick()
    request = only_request(db)
    assert request.status == "REJECTED"
    assert request.check_result == {"accepted": False, "reasons": ["DOCUMENT_UNREADABLE"]}
    assert request.rejected_at == clock.now()
    assert ocr.calls == 0
    assert not storage.doc_path(request.id).exists()


def test_at19_missing_or_tampered_document_file_is_unreadable(make_client, mail, db, ocr):
    owner = signup(make_client(), mail)
    _, key = add_heir(owner, "Пётр")
    heir = make_client()
    heir_login(heir, key)
    submit_document(heir)
    request = only_request(db)
    path = storage.doc_path(request.id)
    blob = bytearray(path.read_bytes())
    blob[-1] ^= 0x01  # DecryptionError
    path.write_bytes(bytes(blob))
    worker.run_tick()
    assert only_request(db).check_result["reasons"] == ["DOCUMENT_UNREADABLE"]
    assert ocr.calls == 0
