"""The provider's world. The catalog is fixed code; only what changes lives here."""

from datetime import datetime

from sqlalchemy import Float, ForeignKey, MetaData, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.database import JsonColumn, UtcDateTime


class MockBase(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class MockState(MockBase):
    """One row: the seed, the moved clock and the active scenario."""

    __tablename__ = "mock_state"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    seed: Mapped[int] = mapped_column()
    clock_offset_s: Mapped[float] = mapped_column(Float, default=0.0)
    scenario: Mapped[dict] = mapped_column(JsonColumn, default=dict)
    book_outcomes_used: Mapped[int] = mapped_column(default=0)


class SlotClaim(MockBase):
    """Whoever holds this row owns the slot. The primary key lets only one claimant win."""

    __tablename__ = "slot_claims"

    provider_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    slot: Mapped[str] = mapped_column(String(40), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class Hold(MockBase):
    __tablename__ = "holds"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    provider_id: Mapped[str] = mapped_column(String(20))
    slot: Mapped[str] = mapped_column(String(40))
    quoted_price: Mapped[int] = mapped_column()
    search_price: Mapped[int] = mapped_column()
    status: Mapped[str] = mapped_column(String(20), default="active")
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)


class Booking(MockBase):
    __tablename__ = "bookings"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    provider_id: Mapped[str] = mapped_column(String(20))
    provider_name: Mapped[str] = mapped_column(String(100))
    service: Mapped[str] = mapped_column(String(40))
    slot: Mapped[str] = mapped_column(String(40))
    price: Mapped[int] = mapped_column()
    status: Mapped[str] = mapped_column(String(30), default="confirmed")
    hold_id: Mapped[str | None] = mapped_column(String(40), index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(100), unique=True)
    cancel_calls: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)


class BookAttempt(MockBase):
    """What each idempotency key did. A key can never create a second booking."""

    __tablename__ = "book_attempts"

    idempotency_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    hold_id: Mapped[str] = mapped_column(ForeignKey("holds.id"))
    user_id: Mapped[str] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(default=0)
    result: Mapped[str] = mapped_column(String(30))
    booking_id: Mapped[str | None] = mapped_column(String(40))


class KeyedResult(MockBase):
    """Stored replies for `cancel` and `reschedule`, so a repeated key changes nothing."""

    __tablename__ = "keyed_results"

    idempotency_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    operation: Mapped[str] = mapped_column(String(20))
    booking_id: Mapped[str] = mapped_column(String(40))
    response: Mapped[dict] = mapped_column(JsonColumn)
