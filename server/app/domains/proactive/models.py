"""What we decided about a booking the provider cancelled. One row per booking, ever."""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Index, String, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, UtcDateTime


class DisruptionStatus(StrEnum):
    # The user knows about the cancel. Replacements are still to come.
    DETECTED = "detected"
    CLOSED = "closed"
    # Nothing was said to the user. `reason` says why.
    IGNORED = "ignored"


class BookingDisruption(Base):
    __tablename__ = "booking_disruptions"
    __table_args__ = (
        Index(
            "ix_disruptions_detected",
            "created_at",
            postgresql_where=text("status = 'detected'"),
            sqlite_where=text("status = 'detected'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Unique, so a repeated event or two polls at once can only ever make one notice.
    booking_id: Mapped[str] = mapped_column(String(40), unique=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    session_id: Mapped[str] = mapped_column(String(64))
    workflow_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    provider_id: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(32))
    search_failures: Mapped[int] = mapped_column(default=0)
    # How many later polls saw the same cancel and said nothing.
    repeats_seen: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)
