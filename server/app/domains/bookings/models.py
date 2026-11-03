"""An approval is what the user agreed to. An attempt is the one try to carry it out."""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import ForeignKey, Index, String, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, UtcDateTime


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    CONSUMED = "consumed"
    REJECTED = "rejected"
    VOIDED = "voided"
    EXPIRED = "expired"


class AttemptStatus(StrEnum):
    OPEN = "open"
    BOOKED = "booked"
    SLOT_TAKEN = "slot_taken"
    HOLD_EXPIRED = "hold_expired"
    PRICE_CHANGED = "price_changed"
    FAILED = "failed"


_PENDING = "status = 'pending'"
_OPEN = "status = 'open'"


class Approval(Base):
    __tablename__ = "approvals"
    __table_args__ = (
        # One question at a time, so a "yes" can only mean one thing.
        Index(
            "uq_approvals_pending_workflow",
            "workflow_id",
            unique=True,
            postgresql_where=text(_PENDING),
            sqlite_where=text(_PENDING),
        ),
        Index("ix_approvals_user_session", "user_id", "session_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    action: Mapped[str] = mapped_column(String(16))
    user_id: Mapped[str] = mapped_column(String(64))
    session_id: Mapped[str] = mapped_column(String(64))
    workflow_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    provider_id: Mapped[str] = mapped_column(String(20))
    provider_name: Mapped[str] = mapped_column(String(120))
    service: Mapped[str] = mapped_column(String(60))
    slot: Mapped[str] = mapped_column(String(40))
    # The hold's quote, which is what the booking will cost.
    price: Mapped[int] = mapped_column()
    # The provider's list price when the hold was taken.
    list_price: Mapped[int] = mapped_column()
    currency: Mapped[str] = mapped_column(String(8))
    max_budget: Mapped[int | None] = mapped_column()
    hold_id: Mapped[str] = mapped_column(String(40))
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)
    approval_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default=ApprovalStatus.PENDING.value)
    closed_reason: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    decided_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class BookingAttempt(Base):
    __tablename__ = "booking_attempts"
    __table_args__ = (
        # Nobody can have two bookings for one provider and slot under way.
        Index(
            "uq_attempts_open_slot",
            "user_id",
            "provider_id",
            "slot",
            unique=True,
            postgresql_where=text(_OPEN),
            sqlite_where=text(_OPEN),
        ),
        Index("ix_attempts_user_session", "user_id", "session_id"),
        Index("ix_attempts_open_lease", "status", "lease_until"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Unique, so one approval can never drive two attempts.
    approval_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("approvals.id"), unique=True)
    user_id: Mapped[str] = mapped_column(String(64))
    session_id: Mapped[str] = mapped_column(String(64))
    workflow_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    # The run that took the user's yes. The saga's events continue its trace.
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    provider_id: Mapped[str] = mapped_column(String(20))
    slot: Mapped[str] = mapped_column(String(40))
    hold_id: Mapped[str] = mapped_column(String(40))
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True)
    status: Mapped[str] = mapped_column(String(16), default=AttemptStatus.OPEN.value)
    lease_until: Mapped[datetime | None] = mapped_column(UtcDateTime)
    # Counts claims, and fences off a worker that lost its lease.
    claims: Mapped[int] = mapped_column(default=0)
    # When a job was last queued for this attempt, so the sweep does not queue a pile of them.
    queued_at: Mapped[datetime] = mapped_column(UtcDateTime)
    # Committed before each call, so a restart knows a call may have happened.
    book_calls: Mapped[int] = mapped_column(default=0)
    last_result: Mapped[str | None] = mapped_column(String(40))
    booking_id: Mapped[str | None] = mapped_column(String(40))
    unconfirmed_notified: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)
