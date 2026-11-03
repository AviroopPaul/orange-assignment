"""Mock provider queries. Flush only: the caller owns the transaction."""

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from mock_provider.models import BookAttempt, Booking, Hold, KeyedResult, MockState, SlotClaim

STATE_ROW = 1


class MockRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def state(self, default_seed: int) -> MockState:
        state = await self._session.get(MockState, STATE_ROW, with_for_update=True)
        if state is None:
            state = MockState(
                id=STATE_ROW,
                seed=default_seed,
                clock_offset_s=0.0,
                scenario={},
                book_outcomes_used=0,
            )
            self._session.add(state)
            await self._session.flush()
        return state

    async def read_state(self, default_seed: int) -> MockState:
        state = await self._session.get(MockState, STATE_ROW)
        return state or MockState(
            id=STATE_ROW, seed=default_seed, clock_offset_s=0.0, scenario={}, book_outcomes_used=0
        )

    async def claimed_slots(self, provider_id: str, now: datetime) -> set[str]:
        rows = await self._session.scalars(
            select(SlotClaim.slot).where(
                SlotClaim.provider_id == provider_id,
                or_(SlotClaim.expires_at.is_(None), SlotClaim.expires_at > now),
            )
        )
        return set(rows)

    async def remove_expired_claim(self, provider_id: str, slot: str, now: datetime) -> None:
        await self._session.execute(
            delete(SlotClaim).where(
                SlotClaim.provider_id == provider_id,
                SlotClaim.slot == slot,
                SlotClaim.expires_at.is_not(None),
                SlotClaim.expires_at <= now,
            )
        )

    async def add_claim(
        self, provider_id: str, slot: str, kind: str, expires_at: datetime | None
    ) -> None:
        """Raises IntegrityError if someone else holds the slot."""
        self._session.add(
            SlotClaim(provider_id=provider_id, slot=slot, kind=kind, expires_at=expires_at)
        )
        await self._session.flush()

    async def set_claim(self, provider_id: str, slot: str, kind: str) -> None:
        claim = await self._session.get(SlotClaim, (provider_id, slot))
        if claim is None:
            self._session.add(
                SlotClaim(provider_id=provider_id, slot=slot, kind=kind, expires_at=None)
            )
        else:
            claim.kind = kind
            claim.expires_at = None
        await self._session.flush()

    async def release_claim(self, provider_id: str, slot: str) -> None:
        await self._session.execute(
            delete(SlotClaim).where(SlotClaim.provider_id == provider_id, SlotClaim.slot == slot)
        )

    async def add(self, row: object) -> None:
        self._session.add(row)
        await self._session.flush()

    async def get_hold(self, hold_id: str, lock: bool = False) -> Hold | None:
        return await self._session.get(Hold, hold_id, with_for_update=lock or None)

    async def active_hold_for_slot(self, user_id: str, provider_id: str, slot: str) -> Hold | None:
        return await self._session.scalar(
            select(Hold).where(
                Hold.user_id == user_id,
                Hold.provider_id == provider_id,
                Hold.slot == slot,
                Hold.status == "active",
            )
        )

    async def get_attempt(self, idempotency_key: str) -> BookAttempt | None:
        return await self._session.get(BookAttempt, idempotency_key)

    async def get_booking(self, booking_id: str, lock: bool = False) -> Booking | None:
        return await self._session.get(Booking, booking_id, with_for_update=lock or None)

    async def booking_for_hold(self, hold_id: str) -> Booking | None:
        return await self._session.scalar(select(Booking).where(Booking.hold_id == hold_id))

    async def bookings_for_user(self, user_id: str) -> Sequence[Booking]:
        rows = await self._session.scalars(
            select(Booking).where(Booking.user_id == user_id).order_by(Booking.slot)
        )
        return rows.all()

    async def get_keyed_result(self, idempotency_key: str) -> KeyedResult | None:
        return await self._session.get(KeyedResult, idempotency_key)

    async def wipe_all(self) -> None:
        for model in (KeyedResult, BookAttempt, Booking, Hold, SlotClaim, MockState):
            await self._session.execute(delete(model))
