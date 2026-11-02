"""Jobs can be lost or delivered twice. The service must stay correct in both cases."""

import asyncio
import uuid

import pytest

from app.core.database import Database
from app.domains.jobs.models import JobStatus
from app.domains.jobs.service import ExecutionResult, JobService
from tests.conftest import FakeClock, RecordingPublisher

LEASE_S = 60
MAX_ATTEMPTS = 3


@pytest.fixture
def calls() -> list[dict]:
    return []


@pytest.fixture
def service(
    db: Database, publisher: RecordingPublisher, clock: FakeClock, calls: list[dict]
) -> JobService:
    async def record(payload: dict) -> None:
        calls.append(payload)

    async def boom(payload: dict) -> None:
        raise RuntimeError("handler failed")

    async def hang(payload: dict) -> None:
        await asyncio.sleep(30)

    return JobService(
        db=db,
        publisher=publisher,
        handlers={"record": record, "boom": boom, "hang": hang},
        clock=clock,
        time_limit_s=0.05,
        lease_s=LEASE_S,
        max_attempts=MAX_ATTEMPTS,
    )


async def enqueue(db: Database, service: JobService, name: str = "record") -> uuid.UUID:
    async with db.session_scope() as session:
        return await service.enqueue_in(session, name, {"n": 1})


async def status_of(service: JobService, job_id: uuid.UUID) -> str:
    job = await service.get(job_id)
    assert job is not None
    return job.status


async def test_job_commits_with_the_callers_transaction(db: Database, service: JobService) -> None:
    with pytest.raises(RuntimeError):
        async with db.session_scope() as session:
            job_id = await service.enqueue_in(session, "record", {})
            raise RuntimeError("the domain change failed")
    assert await service.get(job_id) is None


async def test_publish_sends_pending_jobs_once(
    db: Database, service: JobService, publisher: RecordingPublisher
) -> None:
    job_id = await enqueue(db, service)
    assert await service.publish_pending() == 1
    assert await service.publish_pending() == 0
    assert publisher.published == [job_id]
    assert await status_of(service, job_id) == JobStatus.PUBLISHED


async def test_redis_down_keeps_jobs_pending_for_the_next_sweep(
    db: Database, service: JobService, publisher: RecordingPublisher
) -> None:
    job_id = await enqueue(db, service)
    publisher.broken = True
    assert await service.publish_pending() == 0
    assert await status_of(service, job_id) == JobStatus.PENDING

    publisher.broken = False
    assert await service.sweep() == 1
    assert publisher.published == [job_id]


async def test_execute_runs_handler_and_completes(
    db: Database, service: JobService, calls: list[dict]
) -> None:
    job_id = await enqueue(db, service)
    assert await service.execute(job_id) == ExecutionResult.COMPLETED
    assert calls == [{"n": 1}]
    assert await status_of(service, job_id) == JobStatus.COMPLETED


async def test_second_delivery_after_completion_does_nothing(
    db: Database, service: JobService, calls: list[dict]
) -> None:
    job_id = await enqueue(db, service)
    await service.execute(job_id)
    assert await service.execute(job_id) == ExecutionResult.SKIPPED
    assert len(calls) == 1


async def test_delivery_during_a_live_lease_is_skipped(
    db: Database, publisher: RecordingPublisher, clock: FakeClock
) -> None:
    """The broker can redeliver a job that is still running on another worker."""
    started = asyncio.Event()
    finish = asyncio.Event()
    runs = 0

    async def slow(payload: dict) -> None:
        nonlocal runs
        runs += 1
        started.set()
        await finish.wait()

    service = JobService(db, publisher, {"slow": slow}, clock, 5, LEASE_S, MAX_ATTEMPTS)
    job_id = await enqueue(db, service, "slow")

    first = asyncio.create_task(service.execute(job_id))
    await started.wait()
    assert await service.execute(job_id) == ExecutionResult.SKIPPED
    finish.set()
    assert await first == ExecutionResult.COMPLETED
    assert runs == 1


async def test_worker_that_lost_its_lease_cannot_finish_the_job(
    db: Database, publisher: RecordingPublisher, clock: FakeClock
) -> None:
    started = asyncio.Event()
    finish = asyncio.Event()

    async def slow(payload: dict) -> None:
        started.set()
        await finish.wait()

    service = JobService(db, publisher, {"slow": slow}, clock, 5, LEASE_S, MAX_ATTEMPTS)
    job_id = await enqueue(db, service, "slow")

    first = asyncio.create_task(service.execute(job_id))
    await started.wait()
    clock.advance(LEASE_S + 1)
    await service.sweep()
    second = asyncio.create_task(service.execute(job_id))
    await asyncio.sleep(0.01)
    finish.set()

    assert await second == ExecutionResult.COMPLETED
    assert await first == ExecutionResult.SKIPPED
    job = await service.get(job_id)
    assert job is not None and job.attempts == 2


async def test_dead_worker_job_returns_after_lease_expiry(
    db: Database, service: JobService, publisher: RecordingPublisher, clock: FakeClock
) -> None:
    job_id = await enqueue(db, service)
    async with db.session_scope() as session:
        from datetime import timedelta

        from app.domains.jobs.repository import OutboxRepository

        await OutboxRepository(session).claim(
            job_id, clock.now(), timedelta(seconds=LEASE_S), MAX_ATTEMPTS
        )

    assert await service.sweep() == 0
    assert await status_of(service, job_id) == JobStatus.RUNNING

    clock.advance(LEASE_S + 1)
    assert await service.sweep() == 1
    assert publisher.published == [job_id]
    assert await service.execute(job_id) == ExecutionResult.COMPLETED


async def test_published_job_lost_by_redis_is_sent_again(
    db: Database, service: JobService, publisher: RecordingPublisher, clock: FakeClock
) -> None:
    job_id = await enqueue(db, service)
    await service.publish_pending()

    assert await service.sweep() == 0
    clock.advance(LEASE_S + 1)
    assert await service.sweep() == 1
    assert publisher.published == [job_id, job_id]


async def test_failed_job_retries_then_stops(db: Database, service: JobService) -> None:
    job_id = await enqueue(db, service, "boom")
    assert await service.execute(job_id) == ExecutionResult.RETRY
    assert await status_of(service, job_id) == JobStatus.PENDING
    assert await service.execute(job_id) == ExecutionResult.RETRY
    assert await service.execute(job_id) == ExecutionResult.FAILED

    job = await service.get(job_id)
    assert job is not None
    assert job.status == JobStatus.FAILED
    assert "handler failed" in (job.last_error or "")
    assert await service.execute(job_id) == ExecutionResult.SKIPPED


async def test_job_over_its_time_limit_is_stopped(db: Database, service: JobService) -> None:
    job_id = await enqueue(db, service, "hang")
    assert await service.execute(job_id) == ExecutionResult.RETRY
    job = await service.get(job_id)
    assert job is not None and "TimeoutError" in (job.last_error or "")


async def test_unknown_job_fails_without_retry(db: Database, service: JobService) -> None:
    job_id = await enqueue(db, service, "nobody-handles-this")
    assert await service.execute(job_id) == ExecutionResult.FAILED
    assert await status_of(service, job_id) == JobStatus.FAILED


async def test_job_that_keeps_killing_workers_is_given_up(
    db: Database, service: JobService, clock: FakeClock
) -> None:
    from datetime import timedelta

    from app.domains.jobs.repository import OutboxRepository

    job_id = await enqueue(db, service)
    for _ in range(MAX_ATTEMPTS):
        async with db.session_scope() as session:
            await OutboxRepository(session).claim(
                job_id, clock.now(), timedelta(seconds=LEASE_S), MAX_ATTEMPTS
            )
        clock.advance(LEASE_S + 1)
        await service.sweep()

    job = await service.get(job_id)
    assert job is not None
    assert job.status == JobStatus.FAILED
    assert "attempts exhausted" in (job.last_error or "")


async def test_unknown_id_is_skipped(service: JobService) -> None:
    assert await service.execute(uuid.uuid4()) == ExecutionResult.SKIPPED
