"""Run and trace queries. Flush only: the caller owns the transaction."""

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.tracing.models import Run, TraceEvent


class TraceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_run(self, user_id: str, session_id: str, now: datetime) -> uuid.UUID:
        run = Run(user_id=user_id, session_id=session_id, started_at=now)
        self._session.add(run)
        await self._session.flush()
        return run.id

    async def add_event(self, event: TraceEvent) -> None:
        self._session.add(event)
        await self._session.flush()

    async def last_seq(self, run_id: uuid.UUID) -> int:
        found = await self._session.scalar(
            select(func.max(TraceEvent.seq)).where(TraceEvent.run_id == run_id)
        )
        return found or 0

    async def finish_run(self, run_id: uuid.UUID, **values: object) -> None:
        await self._session.execute(update(Run).where(Run.id == run_id).values(**values))

    async def get_run(self, run_id: uuid.UUID) -> Run | None:
        return await self._session.get(Run, run_id)

    async def runs_for_user(self, user_id: str) -> Sequence[Run]:
        result = await self._session.scalars(select(Run).where(Run.user_id == user_id))
        return result.all()

    async def open_run_ids(self, started_before: datetime) -> Sequence[uuid.UUID]:
        result = await self._session.scalars(
            select(Run.id).where(Run.finished_at.is_(None), Run.started_at < started_before)
        )
        return result.all()

    async def list_events(self, run_id: uuid.UUID) -> Sequence[TraceEvent]:
        result = await self._session.scalars(
            select(TraceEvent).where(TraceEvent.run_id == run_id).order_by(TraceEvent.seq)
        )
        return result.all()
