"""Shared fixtures. Real code paths with fakes only at infrastructure boundaries."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import fakeredis
import pytest
from redis.asyncio import Redis
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.database import Base, Database
from app.domains.jobs import models as job_models  # noqa: F401
from app.domains.tracing import models as tracing_models  # noqa: F401


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
async def redis() -> AsyncIterator[Redis]:
    client = fakeredis.FakeAsyncRedis()
    yield client
    await client.aclose()
