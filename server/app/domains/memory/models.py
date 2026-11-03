"""Facts about a user, our editable copy of the chat, and what each model call was sent."""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import ForeignKey, Index, String, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, BigIntPk, JsonColumn, UtcDateTime

ALL_SCOPES = "all"


class MemorySource(StrEnum):
    """Explicit only if the user's own words were checked. Everything else is inferred."""

    EXPLICIT = "explicit"
    INFERRED = "inferred"


class ChatNote(StrEnum):
    """What the chat must still show about this fact."""

    # Saved in this turn, so the reply says so.
    SAVED = "saved"
    # Not trusted. The chat is asking the user, until their next message.
    ASK = "ask"


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    FORGOTTEN = "forgotten"


_TRUSTED = "status = 'active' AND (source = 'explicit' OR confirmed_at IS NOT NULL)"


class Memory(Base):
    __tablename__ = "memories"
    __table_args__ = (
        # One current value per fact. Unconfirmed guesses wait beside it, and never count.
        Index(
            "uq_memories_trusted_key",
            "user_id",
            "key",
            "scope",
            unique=True,
            postgresql_where=text(_TRUSTED),
            sqlite_where=text(_TRUSTED),
        ),
        Index("ix_memories_user_status", "user_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str] = mapped_column(String(64))
    key: Mapped[str] = mapped_column(String(40))
    value: Mapped[str] = mapped_column(Text)
    # A service name, or "all".
    scope: Mapped[str] = mapped_column(String(40), default=ALL_SCOPES)
    source: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default=MemoryStatus.ACTIVE.value)
    # The user's own sentence. None when the user set the value on the memory panel.
    quote: Mapped[str | None] = mapped_column(Text)
    session_id: Mapped[str | None] = mapped_column(String(64))
    confirmed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    # This row's text must still be removed from stored copies. A forgotten value is wiped after.
    scrub_pending: Mapped[bool] = mapped_column(default=False)
    chat_note: Mapped[str | None] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)


class TurnView(Base):
    """The chat as the model may see it. ADK's log is append-only; this one can be redacted."""

    __tablename__ = "turn_view"
    __table_args__ = (Index("ix_turn_view_user_session", "user_id", "session_id", "id"),)

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64))
    session_id: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)


class ContextLog(Base):
    """Written before each model call. If this write fails, the call does not run."""

    __tablename__ = "context_log"
    __table_args__ = (Index("ix_context_log_user", "user_id", "id"),)

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(String(64))
    session_id: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JsonColumn)
    tokens: Mapped[dict] = mapped_column(JsonColumn)
    estimated_tokens: Mapped[int] = mapped_column()
    budget: Mapped[int] = mapped_column()
    # Gemini's own count, added after the call. Offline runs leave it empty.
    actual_input_tokens: Mapped[int | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)


class SessionRotation(Base):
    """This ADK session holds text that was removed. It is deleted under the session's turn lock."""

    __tablename__ = "session_rotations"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    requested_at: Mapped[datetime] = mapped_column(UtcDateTime)
