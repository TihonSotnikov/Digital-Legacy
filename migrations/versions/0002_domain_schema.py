"""Эталонная схема 2.3.1: пользователи, записи, наследники, ключи, запросы, аудит

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# DDL повторяет эталонную схему 2.3.1 дословно (system_state создана в 0001).
UPGRADE = [
    """
    CREATE TABLE users (
        id                 UUID PRIMARY KEY,
        email              TEXT NOT NULL UNIQUE,          -- в нижнем регистре
        password_hash      TEXT NOT NULL,                 -- argon2id
        last_name          TEXT NOT NULL,
        first_name         TEXT NOT NULL,
        middle_name        TEXT,                          -- NULL, если отчества нет
        email_verified_at  TIMESTAMPTZ,
        last_login_at      TIMESTAMPTZ,
        created_at         TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE records (
        id                 UUID PRIMARY KEY,
        user_id            UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        type               TEXT NOT NULL CHECK (type IN ('text', 'file')),
        title              TEXT NOT NULL CHECK (char_length(title) BETWEEN 1 AND 200),
        ciphertext         BYTEA,       -- только text: nonce(12) || шифротекст с тегом
        file_name          TEXT,        -- только file: исходное имя, только для отображения
        mime_type          TEXT,        -- только file
        size_bytes         BIGINT NOT NULL CHECK (size_bytes >= 0),  -- размер открытых данных
        key_version        SMALLINT NOT NULL DEFAULT 1,
        created_at         TIMESTAMPTZ NOT NULL,
        updated_at         TIMESTAMPTZ NOT NULL,
        CHECK (
            (type = 'text' AND ciphertext IS NOT NULL AND file_name IS NULL AND mime_type IS NULL)
         OR (type = 'file' AND ciphertext IS NULL AND file_name IS NOT NULL AND mime_type IS NOT NULL)
        )
    )
    """,
    "CREATE INDEX records_user_idx ON records (user_id)",
    """
    CREATE TABLE heirs (
        id                 UUID PRIMARY KEY,
        user_id            UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name               TEXT NOT NULL CHECK (char_length(name) BETWEEN 1 AND 100),
        created_at         TIMESTAMPTZ NOT NULL
    )
    """,
    "CREATE INDEX heirs_user_idx ON heirs (user_id)",
    """
    CREATE TABLE heir_keys (
        id                 UUID PRIMARY KEY,
        heir_id            UUID NOT NULL REFERENCES heirs(id) ON DELETE CASCADE,
        key_hash           CHAR(64) NOT NULL UNIQUE,      -- sha256, hex
        created_at         TIMESTAMPTZ NOT NULL,
        revoked_at         TIMESTAMPTZ
    )
    """,
    "CREATE UNIQUE INDEX heir_keys_one_active_idx ON heir_keys (heir_id) WHERE revoked_at IS NULL",
    """
    CREATE TABLE heir_record_access (
        heir_id            UUID NOT NULL REFERENCES heirs(id) ON DELETE CASCADE,
        record_id          UUID NOT NULL REFERENCES records(id) ON DELETE CASCADE,
        PRIMARY KEY (heir_id, record_id)
    )
    """,
    """
    CREATE TABLE inheritance_requests (
        id                 UUID PRIMARY KEY,
        heir_id            UUID NOT NULL REFERENCES heirs(id) ON DELETE CASCADE,
        heir_key_id        UUID NOT NULL REFERENCES heir_keys(id) ON DELETE CASCADE,
        status             TEXT NOT NULL CHECK (status IN (
                               'PENDING_REVIEW', 'DOCUMENT_ACCEPTED', 'WAITING_CANCELLATION',
                               'RELEASED', 'REJECTED', 'CANCELLED')),
        heir_contact_email TEXT,
        doc_mime_type      TEXT NOT NULL CHECK (doc_mime_type IN ('image/jpeg', 'image/png', 'application/pdf')),
        doc_stored         BOOLEAN NOT NULL DEFAULT TRUE, -- временный файл документа ещё на диске
        ocr_attempts       SMALLINT NOT NULL DEFAULT 0,
        check_result       JSONB,
        notified_at        TIMESTAMPTZ,
        waiting_until      TIMESTAMPTZ,
        reminder_sent_at   TIMESTAMPTZ,
        released_at        TIMESTAMPTZ,
        cancelled_at       TIMESTAMPTZ,
        rejected_at        TIMESTAMPTZ,
        created_at         TIMESTAMPTZ NOT NULL,
        updated_at         TIMESTAMPTZ NOT NULL,
        CHECK (status <> 'WAITING_CANCELLATION' OR (notified_at IS NOT NULL AND waiting_until IS NOT NULL)),
        CHECK (status <> 'RELEASED'  OR (released_at IS NOT NULL AND waiting_until IS NOT NULL)),
        CHECK (status <> 'CANCELLED' OR cancelled_at IS NOT NULL),
        CHECK (status <> 'REJECTED'  OR (rejected_at IS NOT NULL AND check_result IS NOT NULL))
    )
    """,
    """
    CREATE UNIQUE INDEX requests_one_active_per_heir_idx ON inheritance_requests (heir_id)
        WHERE status IN ('PENDING_REVIEW', 'DOCUMENT_ACCEPTED', 'WAITING_CANCELLATION')
    """,
    "CREATE INDEX requests_status_idx ON inheritance_requests (status, waiting_until)",
    "CREATE INDEX requests_key_created_idx ON inheritance_requests (heir_key_id, created_at)",
    """
    CREATE TABLE audit_events (
        id                 BIGSERIAL PRIMARY KEY,
        user_id            UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,  -- Владелец, к которому относится событие
        request_id         UUID REFERENCES inheritance_requests(id) ON DELETE SET NULL,
        event_type         TEXT NOT NULL,
        meta               JSONB NOT NULL DEFAULT '{}'::jsonb,  -- без секретов и содержимого
        created_at         TIMESTAMPTZ NOT NULL
    )
    """,
    "CREATE INDEX audit_user_created_idx ON audit_events (user_id, created_at)",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for table in (
        "audit_events",
        "inheritance_requests",
        "heir_record_access",
        "heir_keys",
        "heirs",
        "records",
        "users",
    ):
        op.execute(f"DROP TABLE {table}")
