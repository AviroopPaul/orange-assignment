"""Enqueue, publish, sweep and execute jobs.

Delivery is at-least-once and can repeat while a job still runs, so `execute` claims a lease first.
Recovery comes from `sweep`, not from the broker: the broker only reclaims a dead worker's job when
a new job arrives, and a Redis wipe loses queued jobs entirely.
"""

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from enum import StrEnum
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.database import Database
from app.domains.jobs.models import OutboxJob
from app.domains.jobs.repository import OutboxRepository

logger = logging.getLogger(__name__)

JobHandler = Callable[[dict], Awaitable[None]]


class JobPublisher(Protocol):
    async def publish(self, job_id: uuid.UUID) -> None: ...


class ExecutionResult(StrEnum):
    COMPLETED = "completed"
    RETRY = "retry"
    FAILED = "failed"
    SKIPPED = "skipped"


class JobService:
    def __init__(
        self,
        db: Database,
        publisher: JobPublisher,
        handlers: dict[str, JobHandler],
        clock: Clock,
        time_limit_s: float,
        lease_s: float,
        max_attempts: int,
    ) -> None:
        self._db = db
        self._publisher = publisher
        self._handlers = handlers
        self._clock = clock
        self._time_limit_s = time_limit_s
        self._lease = timedelta(seconds=lease_s)
        self._max_attempts = max_attempts

    async def enqueue_in(self, session: AsyncSession, job_name: str, payload: dict) -> uuid.UUID:
        """Add a job inside the caller's transaction, so it commits with the domain change."""
        return await OutboxRepository(session).add(job_name, payload, self._clock.now())

    async def get(self, job_id: uuid.UUID) -> OutboxJob | None:
        async with self._db.session_scope() as session:
            return await OutboxRepository(session).get(job_id)

    async def publish_pending(self, limit: int = 100) -> int:
        async with self._db.session_scope() as session:
            job_ids = await OutboxRepository(session).pending_ids(limit)

        published = 0
        for job_id in job_ids:
            try:
                await self._publisher.publish(job_id)
            except Exception:
                # Redis is down. Rows stay pending and the next sweep sends them.
                logger.exception("could not publish job %s", job_id)
                break
            async with self._db.session_scope() as session:
                await OutboxRepository(session).mark_published(job_id, self._clock.now())
            published += 1
        return published

    async def sweep(self) -> int:
        async with self._db.session_scope() as session:
            repository = OutboxRepository(session)
            await repository.requeue_stalled(self._clock.now(), self._lease)
            await repository.fail_exhausted(self._max_attempts)
        return await self.publish_pending()

    async def execute(self, job_id: uuid.UUID) -> ExecutionResult:
        async with self._db.session_scope() as session:
            job = await OutboxRepository(session).claim(
                job_id, self._clock.now(), self._lease, self._max_attempts
            )
            if job is None:
                return ExecutionResult.SKIPPED
            job_name, payload, attempt = job.job_name, job.payload, job.attempts

        handler = self._handlers.get(job_name)
        if handler is None:
            await self._release(job_id, attempt, f"no handler for job {job_name}", final=True)
            return ExecutionResult.FAILED

        try:
            async with asyncio.timeout(self._time_limit_s):
                await handler(payload)
        except Exception as error:
            logger.exception("job %s (%s) failed on attempt %d", job_id, job_name, attempt)
            final = attempt >= self._max_attempts
            await self._release(job_id, attempt, repr(error), final=final)
            return ExecutionResult.FAILED if final else ExecutionResult.RETRY

        async with self._db.session_scope() as session:
            finished = await OutboxRepository(session).complete(job_id, attempt, self._clock.now())
        return ExecutionResult.COMPLETED if finished else ExecutionResult.SKIPPED

    async def _release(self, job_id: uuid.UUID, attempt: int, error: str, final: bool) -> None:
        async with self._db.session_scope() as session:
            await OutboxRepository(session).release_after_failure(job_id, attempt, error, final)
