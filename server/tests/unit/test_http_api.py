"""The full HTTP path: real routers, services and ADK, with a scripted model."""

from collections.abc import AsyncIterator
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from google.adk.sessions import InMemorySessionService
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import OperationalError

from app.container import ApiContainer, build_api_container, build_core
from app.core.config import Settings
from app.core.database import Database
from app.core.heartbeats import SCHEDULER_HEARTBEAT, WORKER_HEARTBEAT_PREFIX
from app.domains.agent.scripted import ScriptedLlm
from app.main import create_app
from tests.conftest import FakeClock, RecordingPublisher


@pytest.fixture
def container(
    settings: Settings,
    db: Database,
    redis: Redis,
    clock: FakeClock,
    publisher: RecordingPublisher,
) -> ApiContainer:
    core = build_core(settings, publisher=publisher, db=db, redis=redis, clock=clock)
    return build_api_container(
        core,
        model=ScriptedLlm(model="gemini-3.5-flash-lite", script=["Which area are you in?"]),
        session_service=InMemorySessionService(),
    )


@pytest.fixture
async def app(container: ApiContainer) -> FastAPI:
    async def factory() -> ApiContainer:
        return container

    application = create_app(factory)
    application.state.container = container
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def test_chat_then_read_its_trace(client: httpx.AsyncClient) -> None:
    response = await client.post("/chat", json={"user_id": "u1", "message": "I need a plumber"})
    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "done"
    assert body["reply"] == "Which area are you in?"

    trace = (await client.get(f"/runs/{body['run_id']}/trace")).json()
    assert trace["session_id"] == body["session_id"]
    assert trace["model_calls"] == 1
    assert trace["input_tokens"] == 10
    assert [event["kind"] for event in trace["events"]] == ["model_call", "outcome"]


@pytest.mark.parametrize(
    "payload",
    [
        {"message": "no user"},
        {"user_id": "u1"},
        {"user_id": "u1", "message": ""},
        {"user_id": "u1", "message": "x" * 4001},
        {"user_id": "bad id with spaces", "message": "hi"},
        {"user_id": "u1", "session_id": "../etc", "message": "hi"},
        {"user_id": "u1", "message": "hi", "approved": True},
    ],
)
async def test_bad_chat_requests_are_rejected(client: httpx.AsyncClient, payload: dict) -> None:
    assert (await client.post("/chat", json=payload)).status_code == 422


async def test_unknown_trace_is_404(client: httpx.AsyncClient) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    assert (await client.get(f"/runs/{missing}/trace")).status_code == 404


async def test_database_down_on_chat_gives_a_clear_503(
    client: httpx.AsyncClient, db: Database
) -> None:
    error = OperationalError("insert", {}, Exception("connection refused"))
    with patch.object(db, "session_scope", side_effect=error):
        response = await client.post("/chat", json={"user_id": "u1", "message": "hi"})
    assert response.status_code == 503
    assert response.json()["error"] == "database_unavailable"
    assert "connection refused" not in response.text


async def test_refused_database_connection_gives_a_clear_503(
    client: httpx.AsyncClient, db: Database
) -> None:
    with patch.object(db, "session_scope", side_effect=ConnectionRefusedError("refused")):
        response = await client.post("/chat", json={"user_id": "u1", "message": "hi"})
    assert response.status_code == 503


async def test_healthz_needs_no_dependencies(client: httpx.AsyncClient) -> None:
    assert (await client.get("/healthz")).json() == {"status": "ok"}


async def test_readyz_passes_when_dependencies_are_up(client: httpx.AsyncClient) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"ready": True, "postgres": True, "redis": True}


async def test_readyz_fails_when_redis_is_down(client: httpx.AsyncClient, redis: Redis) -> None:
    with patch.object(redis, "ping", side_effect=RedisConnectionError("down")):
        response = await client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"ready": False, "postgres": True, "redis": False}


async def test_readyz_fails_when_postgres_is_down(client: httpx.AsyncClient, db: Database) -> None:
    error = OperationalError("select", {}, Exception("down"))
    with patch.object(db, "ping", side_effect=error):
        response = await client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["postgres"] is False


async def test_status_reports_worker_and_scheduler(
    client: httpx.AsyncClient, redis: Redis, container: ApiContainer
) -> None:
    before = (await client.get("/system/status")).json()
    assert before["worker"] is False and before["scheduler"] is False
    assert before["model_configured"] is True

    await container.core.heartbeats.beat(f"{WORKER_HEARTBEAT_PREFIX}host-a")
    await container.core.heartbeats.beat(SCHEDULER_HEARTBEAT)
    after = (await client.get("/system/status")).json()
    assert after["worker"] is True and after["scheduler"] is True


async def test_ping_goes_through_the_outbox_to_the_publisher(
    client: httpx.AsyncClient, publisher: RecordingPublisher, container: ApiContainer
) -> None:
    created = await client.post("/system/ping")
    assert created.status_code == 202
    body = created.json()
    assert body["status"] == "published"
    assert [str(job_id) for job_id in publisher.published] == [body["id"]]

    await container.core.jobs.execute(publisher.published[0])
    done = (await client.get(f"/system/ping/{body['id']}")).json()
    assert done["status"] == "completed"
    assert done["attempts"] == 1


async def test_ping_with_redis_down_is_still_recorded(
    client: httpx.AsyncClient, publisher: RecordingPublisher
) -> None:
    publisher.broken = True
    created = await client.post("/system/ping")
    assert created.status_code == 202
    assert created.json()["status"] == "pending"


async def test_unknown_ping_is_404(client: httpx.AsyncClient) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    assert (await client.get(f"/system/ping/{missing}")).status_code == 404
