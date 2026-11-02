"""Counts real HTTP attempts against a local server, using the app's own model config.

The retry sits inside the Gemini SDK, and an SDK or ADK upgrade could drop it without any other test
noticing. No external network is used.
"""

from collections.abc import AsyncIterator

import pytest
from aiohttp import web
from google.adk.sessions import InMemorySessionService

from app.core.config import Settings
from app.core.database import Database
from app.domains.agent.llm import build_model, build_request_config
from app.domains.agent.runtime import AdkAgentRuntime
from app.domains.chat.ports import ModelUnavailable
from app.domains.tracing.service import TraceService
from tests.conftest import FakeClock


class StubModelServer:
    def __init__(self) -> None:
        self.attempts = 0
        self.status = 503
        self.url = ""

    async def handle(self, request: web.Request) -> web.Response:
        self.attempts += 1
        return web.json_response(
            {"error": {"code": self.status, "status": "ERROR", "message": "stub"}},
            status=self.status,
        )


@pytest.fixture
async def stub() -> AsyncIterator[StubModelServer]:
    server = StubModelServer()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", server.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    server.url = f"http://127.0.0.1:{port}"
    yield server
    await runner.cleanup()


async def run_turn(stub: StubModelServer, db: Database, clock: FakeClock) -> None:
    settings = Settings(model_mode="live", google_api_key="test-key", model_name="stub-model")
    model = build_model(settings)
    model.base_url = stub.url  # type: ignore[attr-defined]
    runtime = AdkAgentRuntime(
        model, InMemorySessionService(), 8, request_config=build_request_config(settings)
    )
    recorder = await TraceService(db, clock).start_run("u1", "s1")
    await runtime.run_turn("u1", "s1", "hi", recorder)


@pytest.mark.parametrize("status", [429, 503, 504])
async def test_retryable_status_gets_exactly_one_retry(
    stub: StubModelServer, db: Database, clock: FakeClock, status: int
) -> None:
    stub.status = status
    with pytest.raises(ModelUnavailable):
        await run_turn(stub, db, clock)
    assert stub.attempts == 2


async def test_permanent_error_is_not_retried(
    stub: StubModelServer, db: Database, clock: FakeClock
) -> None:
    stub.status = 404
    with pytest.raises(ModelUnavailable):
        await run_turn(stub, db, clock)
    assert stub.attempts == 1
