"""Real taskiq workers on real Redis: the failures the blueprint says we survive."""

import asyncio
import os
import signal
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from alembic import command
from alembic.config import Config
from redis.asyncio import Redis

from app.container import build_core
from app.core.config import Settings
from app.core.database import Database
from app.domains.jobs.broker import build_broker
from app.domains.jobs.models import JobStatus
from app.domains.jobs.service import JobService

pytestmark = pytest.mark.integration

LEASE_S = 3
WORKER_ENV = {
    "JOB_TIME_LIMIT_S": "2",
    "JOB_LEASE_S": str(LEASE_S),
    "BROKER_IDLE_TIMEOUT_S": "30",
    "BROKER_QUEUE_NAME": "errand.jobs.recovery",
}


@pytest.fixture(scope="module", autouse=True)
def migrated_schema() -> None:
    command.upgrade(Config("alembic.ini"), "head")


@pytest.fixture
def settings() -> Settings:
    return Settings(
        job_time_limit_s=2,
        job_lease_s=LEASE_S,
        broker_idle_timeout_s=30,
        broker_queue_name="errand.jobs.recovery",
    )


@pytest.fixture
async def jobs(settings: Settings) -> AsyncIterator[JobService]:
    broker = build_broker(settings)
    redis = Redis.from_url(settings.redis_url)
    await redis.flushdb()
    await broker.startup()
    core = build_core(
        settings, broker=broker, redis=redis, db=Database(settings.errand_database_url)
    )
    yield core.jobs
    await broker.shutdown()
    await core.close()


@pytest.fixture
def workers() -> Iterator[list[subprocess.Popen]]:
    started: list[subprocess.Popen] = []
    yield started
    for process in started:
        kill(process)


def start_worker(started: list[subprocess.Popen]) -> subprocess.Popen:
    process = subprocess.Popen(
        [sys.executable, "-m", "taskiq", "worker", "app.worker:broker", "--workers", "1"],
        env={**os.environ, **WORKER_ENV},
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    started.append(process)
    return process


def kill(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


async def enqueue(settings: Settings, jobs: JobService, hold_s: float = 0) -> uuid.UUID:
    db = Database(settings.errand_database_url)
    async with db.session_scope() as session:
        job_id = await jobs.enqueue_in(session, "system.ping", {"hold_s": hold_s})
    await db.dispose()
    return job_id


async def wait_for_status(
    jobs: JobService, job_id: uuid.UUID, status: JobStatus, limit_s: float = 25
) -> None:
    async with asyncio.timeout(limit_s):
        while True:
            job = await jobs.get(job_id)
            if job is not None and job.status == status:
                return
            await asyncio.sleep(0.1)


async def test_job_runs_through_a_real_worker(
    settings: Settings, jobs: JobService, workers: list[subprocess.Popen]
) -> None:
    start_worker(workers)
    job_id = await enqueue(settings, jobs)
    await jobs.publish_pending()
    await wait_for_status(jobs, job_id, JobStatus.COMPLETED)


async def test_job_published_before_any_worker_exists_is_not_lost(
    settings: Settings, jobs: JobService, workers: list[subprocess.Popen]
) -> None:
    job_id = await enqueue(settings, jobs)
    await jobs.publish_pending()
    await asyncio.sleep(0.5)
    start_worker(workers)
    await wait_for_status(jobs, job_id, JobStatus.COMPLETED)


async def test_killed_worker_on_a_quiet_queue_recovers_through_the_sweep(
    settings: Settings, jobs: JobService, workers: list[subprocess.Popen]
) -> None:
    """E36. The broker alone would never bring this job back, because no new job arrives."""
    first = start_worker(workers)
    job_id = await enqueue(settings, jobs, hold_s=1.5)
    await jobs.publish_pending()
    await wait_for_status(jobs, job_id, JobStatus.RUNNING)
    kill(first)

    start_worker(workers)
    await asyncio.sleep(LEASE_S + 0.5)
    assert await jobs.sweep() == 1
    await wait_for_status(jobs, job_id, JobStatus.COMPLETED)

    job = await jobs.get(job_id)
    assert job is not None and job.attempts == 2


async def test_redis_wipe_with_a_job_queued_recovers_through_the_sweep(
    settings: Settings, jobs: JobService, workers: list[subprocess.Popen]
) -> None:
    """E31. The job is gone from Redis, but PostgreSQL still knows about it."""
    job_id = await enqueue(settings, jobs)
    await jobs.publish_pending()
    redis = Redis.from_url(settings.redis_url)
    await redis.flushdb()

    start_worker(workers)
    await asyncio.sleep(LEASE_S + 0.5)
    assert await jobs.sweep() == 1
    await wait_for_status(jobs, job_id, JobStatus.COMPLETED)
    await redis.aclose()


async def test_running_worker_survives_a_redis_wipe(
    settings: Settings, jobs: JobService, workers: list[subprocess.Popen]
) -> None:
    start_worker(workers)
    before = await enqueue(settings, jobs)
    await jobs.publish_pending()
    await wait_for_status(jobs, before, JobStatus.COMPLETED)

    redis = Redis.from_url(settings.redis_url)
    await redis.flushdb()
    await redis.aclose()

    after = await enqueue(settings, jobs)
    await jobs.publish_pending()
    await wait_for_status(jobs, after, JobStatus.COMPLETED)
    assert workers[0].poll() is None
