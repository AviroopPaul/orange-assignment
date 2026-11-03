"""Approval and attempt queries. Flush only: the caller owns the transaction."""

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import cast

from sqlalchemy import CursorResult, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.bookings.models import Approval, ApprovalStatus, AttemptStatus, BookingAttempt

_PENDING = ApprovalStatus.PENDING.value
_OPEN = AttemptStatus.OPEN.value
_VERIFIED = (
    AttemptStatus.BOOKED.value,
    AttemptStatus.CANCELLED.value,
    AttemptStatus.RESCHEDULED.value,
)

_KEEPS_A_BOOKING = (AttemptStatus.BOOKED.value, AttemptStatus.RESCHEDULED.value)


class BookingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, row: Approval | BookingAttempt) -> None:
        self._session.add(row)
        await self._session.flush()

    # Approvals

    async def get_approval(self, approval_id: uuid.UUID) -> Approval | None:
        return await self._session.scalar(
            select(Approval)
            .where(Approval.id == approval_id)
            .execution_options(populate_existing=True)
        )

    async def pending_approval(self, user_id: str, session_id: str) -> Approval | None:
        return await self._session.scalar(
            select(Approval)
            .where(
                Approval.user_id == user_id,
                Approval.session_id == session_id,
                Approval.status == _PENDING,
            )
            .order_by(Approval.created_at.desc())
            .limit(1)
        )

    async def consume(self, approval_id: uuid.UUID, now: datetime) -> bool:
        """One conditional update, so only one of many confirmations can win."""
        result = await self._session.execute(
            update(Approval)
            .where(
                Approval.id == approval_id,
                Approval.status == _PENDING,
                Approval.expires_at > now,
            )
            .values(status=ApprovalStatus.CONSUMED.value, decided_at=now)
        )
        return cast(CursorResult, result).rowcount > 0

    async def close_approval(
        self, approval_id: uuid.UUID, status: ApprovalStatus, reason: str, now: datetime
    ) -> bool:
        result = await self._session.execute(
            update(Approval)
            .where(Approval.id == approval_id, Approval.status == _PENDING)
            .values(status=status.value, closed_reason=reason, decided_at=now)
        )
        return cast(CursorResult, result).rowcount > 0

    async def void_pending_for_workflow(
        self, workflow_id: uuid.UUID, reason: str, now: datetime
    ) -> None:
        await self._session.execute(
            update(Approval)
            .where(Approval.workflow_id == workflow_id, Approval.status == _PENDING)
            .values(status=ApprovalStatus.VOIDED.value, closed_reason=reason, decided_at=now)
        )

    # Attempts

    async def get_attempt(self, attempt_id: uuid.UUID) -> BookingAttempt | None:
        return await self._session.scalar(
            select(BookingAttempt)
            .where(BookingAttempt.id == attempt_id)
            .execution_options(populate_existing=True)
        )

    async def attempt_for_approval(self, approval_id: uuid.UUID) -> BookingAttempt | None:
        return await self._session.scalar(
            select(BookingAttempt).where(BookingAttempt.approval_id == approval_id)
        )

    async def open_attempt(self, user_id: str, session_id: str) -> BookingAttempt | None:
        return await self._session.scalar(
            select(BookingAttempt)
            .where(
                BookingAttempt.user_id == user_id,
                BookingAttempt.session_id == session_id,
                BookingAttempt.status == _OPEN,
            )
            .order_by(BookingAttempt.created_at.desc())
            .limit(1)
        )

    async def open_attempt_for_slot(
        self, user_id: str, provider_id: str, slot: str
    ) -> BookingAttempt | None:
        return await self._session.scalar(
            select(BookingAttempt).where(
                BookingAttempt.user_id == user_id,
                BookingAttempt.provider_id == provider_id,
                BookingAttempt.slot == slot,
                BookingAttempt.status == _OPEN,
            )
        )

    async def open_attempt_for_booking(self, booking_id: str) -> BookingAttempt | None:
        return await self._session.scalar(
            select(BookingAttempt).where(
                BookingAttempt.booking_id == booking_id, BookingAttempt.status == _OPEN
            )
        )

    async def verified_actions(self, user_id: str, session_id: str) -> set[str]:
        rows = await self._session.scalars(
            select(BookingAttempt.action)
            .where(
                BookingAttempt.user_id == user_id,
                BookingAttempt.session_id == session_id,
                BookingAttempt.status.in_(_VERIFIED),
            )
            .distinct()
        )
        return set(rows)

    async def claim_attempt(
        self, attempt_id: uuid.UUID, now: datetime, lease: timedelta
    ) -> BookingAttempt | None:
        """Take an open attempt if nobody holds a live lease. One caller wins."""
        return await self._session.scalar(
            update(BookingAttempt)
            .where(
                BookingAttempt.id == attempt_id,
                BookingAttempt.status == _OPEN,
                or_(BookingAttempt.lease_until.is_(None), BookingAttempt.lease_until <= now),
            )
            .values(lease_until=now + lease, claims=BookingAttempt.claims + 1, updated_at=now)
            .returning(BookingAttempt)
            .execution_options(populate_existing=True)
        )

    async def update_claimed(
        self, attempt_id: uuid.UUID, claim: int, now: datetime, **values: object
    ) -> bool:
        """Write only while this worker still holds the claim."""
        result = await self._session.execute(
            update(BookingAttempt)
            .where(
                BookingAttempt.id == attempt_id,
                BookingAttempt.status == _OPEN,
                BookingAttempt.claims == claim,
            )
            .values(updated_at=now, **values)
        )
        return cast(CursorResult, result).rowcount > 0

    async def close_attempt(
        self, attempt_id: uuid.UUID, status: AttemptStatus, now: datetime, **values: object
    ) -> bool:
        """The first worker to reach an answer closes it. Any later one writes nothing."""
        result = await self._session.execute(
            update(BookingAttempt)
            .where(BookingAttempt.id == attempt_id, BookingAttempt.status == _OPEN)
            .values(status=status.value, lease_until=None, updated_at=now, **values)
        )
        return cast(CursorResult, result).rowcount > 0

    async def users_with_bookings(self) -> Sequence[str]:
        rows = await self._session.scalars(
            select(BookingAttempt.user_id)
            .where(
                BookingAttempt.status.in_(_KEEPS_A_BOOKING),
                BookingAttempt.booking_id.is_not(None),
            )
            .distinct()
        )
        return rows.all()

    async def attempts_with_bookings(self, user_id: str) -> Sequence[BookingAttempt]:
        """Oldest first, so a booking's first row is the one that made it."""
        rows = await self._session.scalars(
            select(BookingAttempt)
            .where(
                BookingAttempt.user_id == user_id,
                BookingAttempt.status.in_(_KEEPS_A_BOOKING),
                BookingAttempt.booking_id.is_not(None),
            )
            .order_by(BookingAttempt.created_at)
        )
        return rows.all()

    async def pending_for_booking(self, booking_id: str) -> Sequence[Approval]:
        rows = await self._session.scalars(
            select(Approval).where(Approval.booking_id == booking_id, Approval.status == _PENDING)
        )
        return rows.all()

    async def attempts_to_requeue(
        self, now: datetime, lease: timedelta, limit: int = 100
    ) -> Sequence[BookingAttempt]:
        rows = await self._session.scalars(
            select(BookingAttempt)
            .where(
                BookingAttempt.status == _OPEN,
                or_(BookingAttempt.lease_until.is_(None), BookingAttempt.lease_until <= now),
                BookingAttempt.queued_at <= now - lease,
            )
            .limit(limit)
        )
        return rows.all()
