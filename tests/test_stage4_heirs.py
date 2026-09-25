"""Наследники (этап 4): создание, назначение записей, изменение, удаление, экраны."""

from __future__ import annotations

from sqlalchemy import func, select

from app import storage
from app.models import AuditEvent, Heir, HeirKey, InheritanceRequest, Record, heir_record_access
from tests.helpers import (
    add_heir,
    add_text_record,
    error_codes,
    events,
    make_request,
    page_csrf,
    reload,
    signup,
)


def assigned(db, heir_id) -> set:
    return set(
        db.execute(select(heir_record_access.c.record_id).where(heir_record_access.c.heir_id == heir_id)).scalars()
    )


def test_create_heir_with_records_and_events(client, mail, db):
    signup(client, mail)
    r1 = add_text_record(client, "Банк", "PIN")
    r2 = add_text_record(client, "Почта", "пароль")
    heir_id, _ = add_heir(client, "  Иван  ", [r1, r2, r1])
    heir = db.get(Heir, heir_id)
    assert heir.name == "Иван"
    assert assigned(db, heir_id) == {r1, r2}
    created = events(db, "HEIR_CREATED")[0]
    assert created.meta == {"heir_id": str(heir_id), "record_count": 2}

    page = client.get("/heirs").text
    assert "Иван" in page and "Назначено записей: 2" in page and "Ключ выдан:" in page
    assert "Наследников: 1 из 5" in page


def test_create_heir_flash_on_key_page(client, mail):
    signup(client, mail)
    response = client.post("/heirs", data={"csrf_token": page_csrf(client, "/heirs"), "name": "Иван"})
    assert "Наследник создан" in client.get(response.headers["location"]).text


def test_heir_form_texts(client, mail):
    signup(client, mail)
    form = client.get("/heirs/new").text
    assert "Наследник пока не получит никаких данных — назначьте записи позже" in form
    assert "Создать и получить ключ" in form
    add_text_record(client, "Банк", "PIN")
    form = client.get("/heirs/new").text
    assert "Банк" in form and "(текст)" in form
    page = client.get("/heirs")
    assert "Вы ещё не добавили наследников" in page.text


def test_heir_name_validation(client, mail, db):
    signup(client, mail)
    for name in ("", "   ", "И" * 101):
        response = client.post("/heirs", data={"csrf_token": page_csrf(client, "/heirs"), "name": name})
        assert response.status_code == 400
        assert error_codes(response.text) == ["VALIDATION_ERROR"]
    assert db.scalar(select(func.count()).select_from(Heir)) == 0


def test_edit_heir_name_and_records(client, mail, db):
    signup(client, mail)
    r1 = add_text_record(client, "Банк", "PIN")
    r2 = add_text_record(client, "Почта", "пароль")
    heir_id, _ = add_heir(client, "Иван", [r1])

    form = client.get(f"/heirs/{heir_id}/edit").text
    assert 'value="Иван"' in form and f'value="{r1}" checked' in form and "Сохранить" in form

    response = client.post(
        f"/heirs/{heir_id}/edit",
        data={"csrf_token": page_csrf(client, "/heirs"), "name": "Иван Петрович", "record_ids": [str(r2)]},
    )
    assert response.status_code == 303 and response.headers["location"] == "/heirs"
    assert reload(db, db.get(Heir, heir_id)).name == "Иван Петрович"
    assert assigned(db, heir_id) == {r2}
    assert events(db, "HEIR_UPDATED")[0].meta == {"heir_id": str(heir_id), "record_count": 1}

    response = client.post(f"/heirs/{heir_id}/edit", data={"csrf_token": page_csrf(client, "/heirs"), "name": ""})
    assert response.status_code == 400
    assert assigned(db, heir_id) == {r2}


def test_delete_heir_cascades_and_removes_leftover_documents(client, mail, db, files_dir):
    signup(client, mail)
    r1 = add_text_record(client, "Банк", "PIN")
    heir_id, _ = add_heir(client, "Иван", [r1])
    heir = db.get(Heir, heir_id)
    heir_key = db.execute(select(HeirKey).where(HeirKey.heir_id == heir_id)).scalar_one()
    finished = make_request(db, heir, heir_key, status="REJECTED", doc_stored=True)
    storage.save_document(finished.id, b"%PDF-1.4 leftover")
    request_id = finished.id

    response = client.post(f"/heirs/{heir_id}/delete", data={"csrf_token": page_csrf(client, "/heirs")})
    assert response.status_code == 303 and response.headers["location"] == "/heirs"
    db.expire_all()
    assert db.get(Heir, heir_id) is None
    assert db.scalar(select(func.count()).select_from(HeirKey)) == 0
    assert db.scalar(select(func.count()).select_from(InheritanceRequest)) == 0
    assert assigned(db, heir_id) == set()
    assert db.get(Record, r1) is not None  # записи Владельца не затрагиваются
    assert not storage.doc_path(request_id).exists()
    assert events(db, "HEIR_DELETED")[0].meta == {"heir_id": str(heir_id), "record_count": 1}
    # У событий удалённого запроса request_id обнуляется (ON DELETE SET NULL).
    assert db.execute(select(AuditEvent).where(AuditEvent.request_id == request_id)).first() is None
    assert "Наследник удалён" in client.get("/heirs").text


def test_record_deletion_removes_assignment(client, mail, db):
    signup(client, mail)
    r1 = add_text_record(client, "Банк", "PIN")
    heir_id, _ = add_heir(client, "Иван", [r1])
    page = client.get("/vault").text
    assert "Она назначена наследникам: 1." in page  # подтверждение с числом наследников
    client.post(f"/records/{r1}/delete", data={"csrf_token": page_csrf(client, "/vault")})
    assert assigned(db, heir_id) == set()
    assert "Назначено записей: 0" in client.get("/heirs").text
