"""Behaviour SQLite cannot prove. Needs real PostgreSQL and Redis (make test-integration)."""

import asyncio
import uuid
from datetime import timedelta

import pytest
from alembic import command
from alembic.config import Config
from alembic.util.exc import CommandError
from google.adk.sessions import DatabaseSessionService
from redis.asyncio import Redis

from app.container import build_api_container, build_core
from app.core.clock import SystemClock
from app.core.config import get_settings
from app.core.database import Database
from app.domains.agent.scripted import ScriptedLlm
from app.domains.chat.schemas import ChatRequest, Outcome
from app.domains.jobs.repository import OutboxRepository
from tests.conftest import RecordingPublisher

pytestmark = pytest.mark.integration


def alembic_config() -> Config:
    return Config("alembic.ini")


@pytest.fixture(scope="module", autouse=True)
def migrated_schema() -> None:
    command.upgrade(alembic_config(), "head")


async def test_models_and_migrations_agree() -> None:
    try:
        await asyncio.to_thread(command.check, alembic_config())
    except CommandError as error:
        pytest.fail(f"models changed without a migration: {error}")


async def test_only_one_of_ten_concurrent_claims_wins() -> None:
    db = Database(get_settings().errand_database_url, pool_size=10)
    clock = SystemClock()
    async with db.session_scope() as session:
        job_id = await OutboxRepository(session).add("system.ping", {}, clock.now())

    async def claim() -> object:
        async with db.session_scope() as session:
            return await OutboxRepository(session).claim(
                job_id, clock.now(), timedelta(seconds=60), max_attempts=5
            )

    results = await asyncio.gather(*[claim() for _ in range(10)])
    assert len([result for result in results if result is not None]) == 1
    await db.dispose()


async def test_chat_on_real_services_keeps_history_and_serialises_turns() -> None:
    settings = get_settings()
    db = Database(settings.errand_database_url)
    redis = Redis.from_url(settings.redis_url)
    core = build_core(settings, publisher=RecordingPublisher(), db=db, redis=redis)
    model = ScriptedLlm(model="gemini-3.5-flash-lite", script=["ok"])
    sessions = DatabaseSessionService(db_url=settings.adk_database_url)
    container = build_api_container(core, model=model, session_service=sessions)
    session_id = uuid.uuid4().hex

    responses = await asyncio.gather(
        *[
            container.chat.handle_message(
                ChatRequest(user_id="it", session_id=session_id, message=f"message {n}")
            )
            for n in range(3)
        ]
    )

    assert [response.outcome for response in responses] == [Outcome.DONE] * 3
    last_seen = str(model.requests[-1].contents)
    assert all(f"message {n}" in last_seen for n in range(3))

    restarted = DatabaseSessionService(db_url=settings.adk_database_url)
    stored = await restarted.get_session(
        app_name="errand-agent", user_id="it", session_id=session_id
    )
    assert stored is not None and len(stored.events) == 6

    trace = await container.traces.get_trace(responses[0].run_id)
    assert trace.outcome == "done"
    await sessions.db_engine.dispose()
    await restarted.db_engine.dispose()
    await core.close()
