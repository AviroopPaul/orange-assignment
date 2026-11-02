"""Run and trace queries. Flush only: the caller owns the transaction."""

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select, update
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

    async def finish_run(self, run_id: uuid.UUID, **values: object) -> None:
        await self._session.execute(update(Run).where(Run.id == run_id).values(**values))

    async def get_run(self, run_id: uuid.UUID) -> Run | None:
        return await self._session.get(Run, run_id)

    async def list_events(self, run_id: uuid.UUID) -> Sequence[TraceEvent]:
        result = await self._session.scalars(
            select(TraceEvent).where(TraceEvent.run_id == run_id).order_by(TraceEvent.seq)
        )
        return result.all()
