"""Модели SQLAlchemy по эталонной схеме 2.3.1."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    SmallInteger,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

TZ = DateTime(timezone=True)

ACTIVE_STATUSES = frozenset({"PENDING_REVIEW", "DOCUMENT_ACCEPTED", "WAITING_CANCELLATION"})
TERMINAL_STATUSES = frozenset({"REJECTED", "CANCELLED", "RELEASED"})
ALL_STATUSES = ACTIVE_STATUSES | TERMINAL_STATUSES


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    last_name: Mapped[str] = mapped_column(Text, nullable=False)
    first_name: Mapped[str] = mapped_column(Text, nullable=False)
    middle_name: Mapped[str | None] = mapped_column(Text)
    email_verified_at: Mapped[datetime | None] = mapped_column(TZ)
    last_login_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False)


class Record(Base):
    __tablename__ = "records"
    __table_args__ = (
        CheckConstraint("type IN ('text', 'file')", name="records_type_check"),
        CheckConstraint("char_length(title) BETWEEN 1 AND 200", name="records_title_check"),
        CheckConstraint("size_bytes >= 0", name="records_size_bytes_check"),
        CheckConstraint(
            "(type = 'text' AND ciphertext IS NOT NULL AND file_name IS NULL AND mime_type IS NULL)"
            " OR (type = 'file' AND ciphertext IS NULL AND file_name IS NOT NULL AND mime_type IS NOT NULL)",
            name="records_check",
        ),
        Index("records_user_idx", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    file_name: Mapped[str | None] = mapped_column(Text)
    mime_type: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    key_version: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TZ, nullable=False)


class Heir(Base):
    __tablename__ = "heirs"
    __table_args__ = (
        CheckConstraint("char_length(name) BETWEEN 1 AND 100", name="heirs_name_check"),
        Index("heirs_user_idx", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False)


class HeirKey(Base):
    __tablename__ = "heir_keys"
    __table_args__ = (
        Index(
            "heir_keys_one_active_idx",
            "heir_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    heir_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("heirs.id", ondelete="CASCADE"), nullable=False
    )
    key_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(TZ)


heir_record_access = Table(
    "heir_record_access",
    Base.metadata,
    Column("heir_id", UUID(as_uuid=True), ForeignKey("heirs.id", ondelete="CASCADE"), primary_key=True),
    Column("record_id", UUID(as_uuid=True), ForeignKey("records.id", ondelete="CASCADE"), primary_key=True),
)


class InheritanceRequest(Base):
    __tablename__ = "inheritance_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING_REVIEW', 'DOCUMENT_ACCEPTED', 'WAITING_CANCELLATION',"
            " 'RELEASED', 'REJECTED', 'CANCELLED')",
            name="inheritance_requests_status_check",
        ),
        CheckConstraint(
            "doc_mime_type IN ('image/jpeg', 'image/png', 'application/pdf')",
            name="inheritance_requests_doc_mime_type_check",
        ),
        CheckConstraint(
            "status <> 'WAITING_CANCELLATION' OR (notified_at IS NOT NULL AND waiting_until IS NOT NULL)",
            name="inheritance_requests_check",
        ),
        CheckConstraint(
            "status <> 'RELEASED' OR (released_at IS NOT NULL AND waiting_until IS NOT NULL)",
            name="inheritance_requests_check1",
        ),
        CheckConstraint(
            "status <> 'CANCELLED' OR cancelled_at IS NOT NULL", name="inheritance_requests_check2"
        ),
        CheckConstraint(
            "status <> 'REJECTED' OR (rejected_at IS NOT NULL AND check_result IS NOT NULL)",
            name="inheritance_requests_check3",
        ),
        Index(
            "requests_one_active_per_heir_idx",
            "heir_id",
            unique=True,
            postgresql_where=text(
                "status IN ('PENDING_REVIEW', 'DOCUMENT_ACCEPTED', 'WAITING_CANCELLATION')"
            ),
        ),
        Index("requests_status_idx", "status", "waiting_until"),
        Index("requests_key_created_idx", "heir_key_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    heir_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("heirs.id", ondelete="CASCADE"), nullable=False
    )
    heir_key_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("heir_keys.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    heir_contact_email: Mapped[str | None] = mapped_column(Text)
    doc_mime_type: Mapped[str] = mapped_column(Text, nullable=False)
    doc_stored: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    ocr_attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    check_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    notified_at: Mapped[datetime | None] = mapped_column(TZ)
    waiting_until: Mapped[datetime | None] = mapped_column(TZ)
    reminder_sent_at: Mapped[datetime | None] = mapped_column(TZ)
    released_at: Mapped[datetime | None] = mapped_column(TZ)
    cancelled_at: Mapped[datetime | None] = mapped_column(TZ)
    rejected_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TZ, nullable=False)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("audit_user_created_idx", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    request_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inheritance_requests.id", ondelete="SET NULL")
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False)


class SystemState(Base):
    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TZ, nullable=False)
