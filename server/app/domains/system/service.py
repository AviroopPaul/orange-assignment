"""Readiness, and a ping job that proves the whole path: outbox, Redis, worker, PostgreSQL."""

import asyncio
import logging
import uuid

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from app.core.database import Database
from app.core.heartbeats import SCHEDULER_HEARTBEAT, WORKER_HEARTBEAT_PREFIX, Heartbeats
from app.domains.jobs.service import JobService
from app.domains.system.schemas import PingRead, Readiness, SystemStatus

logger = logging.getLogger(__name__)

PING_JOB = "system.ping"


class PingNotFound(Exception):
    pass


async def handle_ping(payload: dict) -> None:
    """Does nothing but wait if asked, so tests can kill a worker in the middle of a job."""
    await asyncio.sleep(float(payload.get("hold_s", 0)))


class SystemService:
    def __init__(
        self,
        db: Database,
        redis: Redis,
        heartbeats: Heartbeats,
        jobs: JobService,
        model_configured: bool,
    ) -> None:
        self._db = db
        self._redis = redis
        self._heartbeats = heartbeats
        self._jobs = jobs
        self._model_configured = model_configured

    async def readiness(self) -> Readiness:
        postgres = await self._postgres_ok()
        redis = await self._redis_ok()
        return Readiness(ready=postgres and redis, postgres=postgres, redis=redis)

    async def status(self) -> SystemStatus:
        readiness = await self.readiness()
        worker = scheduler = False
        if readiness.redis:
            worker = await self._heartbeats.any_alive(WORKER_HEARTBEAT_PREFIX)
            scheduler = await self._heartbeats.is_alive(SCHEDULER_HEARTBEAT)
        return SystemStatus(
            **readiness.model_dump(),
            worker=worker,
            scheduler=scheduler,
            model_configured=self._model_configured,
        )

    async def request_ping(self) -> PingRead:
        async with self._db.session_scope() as session:
            job_id = await self._jobs.enqueue_in(session, PING_JOB, {})
        await self._jobs.publish_pending()
        return await self.get_ping(job_id)

    async def get_ping(self, job_id: uuid.UUID) -> PingRead:
        job = await self._jobs.get(job_id)
        if job is None or job.job_name != PING_JOB:
            raise PingNotFound(str(job_id))
        return PingRead(
            id=job.id, status=job.status, attempts=job.attempts, last_error=job.last_error
        )

    async def _postgres_ok(self) -> bool:
        try:
            await self._db.ping()
        except (SQLAlchemyError, OSError):
            logger.warning("PostgreSQL is not reachable")
            return False
        return True

    async def _redis_ok(self) -> bool:
        try:
            await self._redis.ping()
        except (RedisError, OSError):
            logger.warning("Redis is not reachable")
            return False
        return True
