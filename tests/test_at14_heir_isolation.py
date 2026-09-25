"""AT-14: изоляция наследников."""

from __future__ import annotations

from sqlalchemy import select

from app.models import Heir, HeirKey
from tests.helpers import (
    add_file_record,
    add_heir,
    add_text_record,
    heir_login,
    make_request,
    signup,
)

R2_BYTES = b"%PDF-1.4 heir two only"


def test_at14_heirs_see_only_their_records(make_client, mail, db):
    owner = signup(make_client(), mail)
    r1 = add_text_record(owner, "Запись для H1", "Секрет H1 8841")
    r2 = add_file_record(owner, "Файл для H2", "h2.pdf", R2_BYTES)
    h1, key1 = add_heir(owner, "H1", [r1])
    h2, key2 = add_heir(owner, "H2", [r2])
    for heir_id in (h1, h2):
        heir_key = db.execute(select(HeirKey).where(HeirKey.heir_id == heir_id)).scalar_one()
        make_request(db, db.get(Heir, heir_id), heir_key, status="RELEASED")

    first = make_client()
    heir_login(first, key1)
    page = first.get("/heir/portal").text
    assert "Запись для H1" in page and "Секрет H1 8841" in page
    assert "Файл для H2" not in page and "h2.pdf" not in page and str(r2) not in page
    response = first.get(f"/heir/files/{r2}")
    assert response.status_code == 404
    assert R2_BYTES not in response.content

    second = make_client()
    heir_login(second, key2)
    page = second.get("/heir/portal").text
    assert "Файл для H2" in page and "Секрет H1 8841" not in page and "Запись для H1" not in page
    assert second.get(f"/heir/files/{r2}").content == R2_BYTES
    assert second.get(f"/heir/files/{r1}").status_code == 404  # текстовая запись и не назначена


def test_at14_no_files_before_release(make_client, mail, db):
    owner = signup(make_client(), mail)
    r1 = add_file_record(owner, "Файл", "f.pdf", b"%PDF-1.4 x")
    heir_id, key = add_heir(owner, "H1", [r1])
    heir_key = db.execute(select(HeirKey).where(HeirKey.heir_id == heir_id)).scalar_one()
    make_request(db, db.get(Heir, heir_id), heir_key, status="WAITING_CANCELLATION")
    heir = make_client()
    heir_login(heir, key)
    assert heir.get(f"/heir/files/{r1}").status_code == 404
    assert "f.pdf" not in heir.get("/heir/portal").text
