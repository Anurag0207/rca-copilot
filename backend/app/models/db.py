"""
SQLAlchemy async ORM models for app metadata.

Tables:
  - users          — authenticated user accounts
  - session_records — lightweight session metadata (no result data)
  - audit_logs      — query audit trail (prompt + generated SQL; no result rows)

IMPORTANT: Result row data is NEVER written to PostgreSQL. Only session metadata
and audit records are stored here. Live session state (including result references)
lives in Redis.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    ARRAY,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""


class User(Base):
    """
    Authenticated user account.

    Passwords are stored as bcrypt hashes. The ``roles`` column is a
    PostgreSQL text array forwarded to the MCP layer for RBAC passthrough.
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    roles: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    session_records: Mapped[list["SessionRecord"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    audit_logs: Mapped[list["AuditLog"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} username={self.username!r}>"


class SessionRecord(Base):
    """
    Lightweight metadata record for a conversation session.

    Stores only identifiers and counters — no conversation content and no
    result data. Full session state (history, download tokens) lives in Redis
    under key `session:{user_id}:{redis_session_id}`.
    """

    __tablename__ = "session_records"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    redis_session_id: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False
    )
    connection_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_activity: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    turn_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Relationships
    user: Mapped["User"] = relationship(back_populates="session_records")
    audit_logs: Mapped[list["AuditLog"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<SessionRecord id={self.id} user_id={self.user_id}>"


class AuditLog(Base):
    """
    Audit trail for every NL query processed.

    Records the user's prompt and the generated HQL/SQL for compliance and
    debugging. Result rows are NEVER stored here — only the query text itself.
    """

    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("session_records.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    prompt_text: Mapped[str] = mapped_column(Text, nullable=False)
    generated_query: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_format: Mapped[str | None] = mapped_column(String(16), nullable=True)
    connection_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)  # AuditStatus enum value
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    # Relationships
    user: Mapped["User"] = relationship(back_populates="audit_logs")
    session: Mapped["SessionRecord | None"] = relationship(back_populates="audit_logs")

    def __repr__(self) -> str:
        return f"<AuditLog id={self.id} status={self.status!r}>"


async def create_db_tables(engine: "AsyncEngine") -> None:
    """
    Create all tables in the database.

    For development only — use Alembic migrations in staging/production.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
