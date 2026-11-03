"""Shared fixtures. Real code paths with fakes only at infrastructure boundaries."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import fakeredis
import httpx
import pytest
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.database import Base, Database
from app.domains.bookings import models as booking_models  # noqa: F401
from app.domains.bookings.service import ApprovalService
from app.domains.gateway.client import ProviderClient
from app.domains.gateway.service import DenyAllApprovals, ToolGateway
from app.domains.jobs import models as job_models  # noqa: F401
from app.domains.jobs.service import JobService
from app.domains.memory import models as memory_models  # noqa: F401
from app.domains.memory.service import MemoryService
from app.domains.notifications import models as notification_models  # noqa: F401
from app.domains.notifications.service import NotificationService
from app.domains.tracing import models as tracing_models  # noqa: F401
from app.domains.tracing.service import TraceService
from app.domains.workflow import models as workflow_models  # noqa: F401
from app.domains.workflow.service import WorkflowService
from mock_provider.config import MockSettings
from mock_provider.main import create_app as create_mock_app
from mock_provider.models import MockBase


class FakeClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


class RecordingPublisher:
    """Stands in for Redis. Set `broken` to simulate Redis being down."""

    def __init__(self) -> None:
        self.published: list[uuid.UUID] = []
        self.broken = False

    async def publish(self, job_id: uuid.UUID) -> None:
        if self.broken:
            raise ConnectionError("redis is down")
        self.published.append(job_id)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def publisher() -> RecordingPublisher:
    return RecordingPublisher()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        model_mode="scripted",
        errand_database_url="sqlite+aiosqlite://",
        adk_database_url="sqlite+aiosqlite://",
        turn_lock_wait_s=0.2,
        _env_file=None,
    )


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    database = Database(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield database
    await database.dispose()


@pytest.fixture
def workflow(db: Database, clock: FakeClock) -> WorkflowService:
    return WorkflowService(db, clock)


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    client = fakeredis.FakeAsyncRedis()
    yield client
    await client.aclose()


@pytest.fixture
async def provider_app(clock: FakeClock) -> AsyncIterator[FastAPI]:
    """The real mock provider on in-memory SQLite. Tests talk to it through its real routes."""
    database = Database(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with database.engine.begin() as connection:
        await connection.run_sync(MockBase.metadata.create_all)
    settings = MockSettings(mock_eval_mode=True, mock_timeout_hang_s=0)
    yield create_mock_app(settings=settings, db=database, real_now=clock.now)
    await database.dispose()


@pytest.fixture
def provider_transport(provider_app: FastAPI) -> httpx.AsyncBaseTransport:
    return httpx.ASGITransport(app=provider_app)


@pytest.fixture
async def gateway(provider_transport: httpx.AsyncBaseTransport) -> AsyncIterator[ToolGateway]:
    client = ProviderClient("http://provider", timeout_s=1, transport=provider_transport)

    async def no_wait(seconds: float) -> None:
        return None

    yield ToolGateway(client, DenyAllApprovals(), retry_backoff_s=0, sleep=no_wait)
    await client.close()


@pytest.fixture
def jobs(db: Database, clock: FakeClock, publisher: RecordingPublisher) -> JobService:
    return JobService(db, publisher, {}, clock, time_limit_s=45, lease_s=60, max_attempts=5)


@pytest.fixture
def notifications(db: Database, clock: FakeClock) -> NotificationService:
    return NotificationService(db, clock)


@pytest.fixture
def memory(db: Database, clock: FakeClock, workflow: WorkflowService) -> MemoryService:
    return MemoryService(db, clock, workflow, TraceService(db, clock))


@pytest.fixture
def bookings(
    db: Database,
    clock: FakeClock,
    gateway: ToolGateway,
    workflow: WorkflowService,
    jobs: JobService,
    notifications: NotificationService,
) -> ApprovalService:
    return ApprovalService(
        db, clock, gateway, workflow, jobs, TraceService(db, clock), notifications
    )


async def make_ready(
    workflow: WorkflowService,
    user: str,
    session: str,
    phrase: str = "3 Nov to 5 Nov",
    service: str = "plumber",
    area: str = "Indiranagar",
    **extra: object,
) -> None:
    """A request with every detail saved, the way a real user gets there."""
    said = " ".join(str(value) for value in extra.values())
    await workflow.record_user_message(user, session, f"{service} in {area} {phrase} {said}")
    await workflow.update_request(user, session, {"service": service, "area": area, **extra})
    await workflow.resolve_dates(user, session, phrase)
