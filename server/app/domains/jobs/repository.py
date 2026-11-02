"""Outbox queries. Flush only: the caller owns the transaction."""

import uuid
from datetime import datetime, timedelta
from typing import cast

from sqlalchemy import CursorResult, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.jobs.models import JobStatus, OutboxJob

OPEN_STATUSES = [JobStatus.PENDING.value, JobStatus.PUBLISHED.value, JobStatus.RUNNING.value]


class OutboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, job_name: str, payload: dict, now: datetime) -> uuid.UUID:
        job = OutboxJob(job_name=job_name, payload=payload, created_at=now)
        self._session.add(job)
        await self._session.flush()
        return job.id

    async def get(self, job_id: uuid.UUID) -> OutboxJob | None:
        return await self._session.get(OutboxJob, job_id)

    async def pending_ids(self, limit: int) -> list[uuid.UUID]:
        rows = await self._session.scalars(
            select(OutboxJob.id)
            .where(OutboxJob.status == JobStatus.PENDING.value)
            .order_by(OutboxJob.created_at)
            .limit(limit)
        )
        return list(rows)

    async def mark_published(self, job_id: uuid.UUID, now: datetime) -> None:
        await self._session.execute(
            update(OutboxJob)
            .where(OutboxJob.id == job_id, OutboxJob.status == JobStatus.PENDING.value)
            .values(status=JobStatus.PUBLISHED.value, published_at=now)
        )

    async def claim(
        self, job_id: uuid.UUID, now: datetime, lease: timedelta, max_attempts: int
    ) -> OutboxJob | None:
        """Take a job if nobody holds a live lease. One conditional update, so one caller wins."""
        return await self._session.scalar(
            update(OutboxJob)
            .where(
                OutboxJob.id == job_id,
                OutboxJob.status.in_(OPEN_STATUSES),
                or_(OutboxJob.lease_until.is_(None), OutboxJob.lease_until <= now),
                OutboxJob.attempts < max_attempts,
            )
            .values(
                status=JobStatus.RUNNING.value,
                lease_until=now + lease,
                attempts=OutboxJob.attempts + 1,
            )
            .returning(OutboxJob)
        )

    async def complete(self, job_id: uuid.UUID, attempt: int, now: datetime) -> bool:
        """Finish a claimed job. `attempt` fences off a worker that lost its lease."""
        result = await self._session.execute(
            update(OutboxJob)
            .where(
                OutboxJob.id == job_id,
                OutboxJob.status == JobStatus.RUNNING.value,
                OutboxJob.attempts == attempt,
            )
            .values(status=JobStatus.COMPLETED.value, completed_at=now, lease_until=None)
        )
        return cast(CursorResult, result).rowcount > 0

    async def release_after_failure(
        self, job_id: uuid.UUID, attempt: int, error: str, final: bool
    ) -> None:
        await self._session.execute(
            update(OutboxJob)
            .where(
                OutboxJob.id == job_id,
                OutboxJob.status == JobStatus.RUNNING.value,
                OutboxJob.attempts == attempt,
            )
            .values(
                status=JobStatus.FAILED.value if final else JobStatus.PENDING.value,
                lease_until=None,
                last_error=error[:2000],
            )
        )

    async def requeue_stalled(self, now: datetime, lease: timedelta) -> None:
        """Return lost work to pending.

        Covers a published job that no worker claimed in time (Redis was wiped) and a running job
        whose lease expired (the worker died).
        """
        await self._session.execute(
            update(OutboxJob)
            .where(OutboxJob.status == JobStatus.PUBLISHED.value)
            .where(OutboxJob.published_at <= now - lease)
            .values(status=JobStatus.PENDING.value)
        )
        await self._session.execute(
            update(OutboxJob)
            .where(OutboxJob.status == JobStatus.RUNNING.value)
            .where(OutboxJob.lease_until <= now)
            .values(status=JobStatus.PENDING.value, lease_until=None)
        )

    async def fail_exhausted(self, max_attempts: int) -> None:
        await self._session.execute(
            update(OutboxJob)
            .where(OutboxJob.status == JobStatus.PENDING.value)
            .where(OutboxJob.attempts >= max_attempts)
            .values(
                status=JobStatus.FAILED.value,
                last_error="attempts exhausted: the worker died or lost the job each time",
            )
        )
