"""Disruption queries. Flush only: the caller owns the transaction."""

import uuid
from collections.abc import Sequence

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.proactive.models import BookingDisruption, DisruptionStatus


class ProactiveRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, row: BookingDisruption) -> None:
        self._session.add(row)
        await self._session.flush()

    async def recorded_booking_ids(self, user_id: str) -> set[str]:
        rows = await self._session.scalars(
            select(BookingDisruption.booking_id).where(BookingDisruption.user_id == user_id)
        )
        return set(rows.all())

    async def note_repeats(self, booking_ids: Sequence[str]) -> None:
        await self._session.execute(
            update(BookingDisruption)
            .where(BookingDisruption.booking_id.in_(booking_ids))
            .values(repeats_seen=BookingDisruption.repeats_seen + 1)
        )

    async def detected(self, limit: int) -> Sequence[BookingDisruption]:
        rows = await self._session.scalars(
            select(BookingDisruption)
            .where(BookingDisruption.status == DisruptionStatus.DETECTED.value)
            .order_by(BookingDisruption.created_at)
            .limit(limit)
        )
        return rows.all()

    async def get_locked(self, disruption_id: uuid.UUID) -> BookingDisruption | None:
        return await self._session.scalar(
            select(BookingDisruption)
            .where(BookingDisruption.id == disruption_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
