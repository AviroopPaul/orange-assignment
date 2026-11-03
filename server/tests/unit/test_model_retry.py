"""Counts real HTTP attempts against a local server, using the app's own model config.

The retry sits inside the Gemini SDK, and an SDK or ADK upgrade could drop it without any other test
noticing. No external network is used.
"""

import time
from collections.abc import AsyncIterator

import pytest
from aiohttp import web
from google.adk.sessions import InMemorySessionService

from app.core.config import Settings
from app.core.database import Database
from app.domains.agent.llm import build_model, build_request_config
from app.domains.agent.runtime import AdkAgentRuntime
from app.domains.bookings.service import ApprovalService
from app.domains.chat.ports import ModelRateLimited, ModelUnavailable
from app.domains.gateway.service import ToolGateway
from app.domains.tracing.service import TraceService
from app.domains.workflow.service import WorkflowService
from tests.conftest import FakeClock


class StubModelServer:
    def __init__(self) -> None:
        self.attempts = 0
        self.status = 503
        self.retry_delay: str | None = None
        self.url = ""

    async def handle(self, request: web.Request) -> web.Response:
        self.attempts += 1
        error: dict = {"code": self.status, "status": "ERROR", "message": "stub"}
        if self.retry_delay:
            error["details"] = [
                {
                    "@type": "type.googleapis.com/google.rpc.RetryInfo",
                    "retryDelay": self.retry_delay,
                }
            ]
        return web.json_response({"error": error}, status=self.status)


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


async def run_turn(
    bookings: ApprovalService,
    stub: StubModelServer,
    db: Database,
    clock: FakeClock,
    gateway: ToolGateway,
    workflow: WorkflowService,
) -> None:
    settings = Settings(model_mode="live", google_api_key="test-key", model_name="stub-model")
    model = build_model(settings)
    model.base_url = stub.url  # type: ignore[attr-defined]
    runtime = AdkAgentRuntime(
        model,
        InMemorySessionService(),
        gateway,
        workflow,
        bookings,
        max_model_calls=8,
        max_tool_calls=12,
        today=lambda: clock.now().date(),
        request_config=build_request_config(settings),
    )
    recorder = await TraceService(db, clock).start_run("u1", "s1")
    await runtime.run_turn("u1", "s1", "hi", recorder)


@pytest.mark.parametrize("status", [503, 504])
async def test_retryable_status_gets_exactly_one_retry(
    bookings: ApprovalService,
    stub: StubModelServer,
    db: Database,
    clock: FakeClock,
    gateway: ToolGateway,
    workflow: WorkflowService,
    status: int,
) -> None:
    stub.status = status
    with pytest.raises(ModelUnavailable):
        await run_turn(bookings, stub, db, clock, gateway, workflow)
    assert stub.attempts == 2


async def test_rate_limit_waits_as_long_as_gemini_asks_then_tries_once_more(
    bookings: ApprovalService,
    stub: StubModelServer,
    db: Database,
    clock: FakeClock,
    gateway: ToolGateway,
    workflow: WorkflowService,
) -> None:
    stub.status = 429
    stub.retry_delay = "1.5s"
    started = time.monotonic()
    with pytest.raises(ModelRateLimited) as caught:
        await run_turn(bookings, stub, db, clock, gateway, workflow)
    assert time.monotonic() - started >= 1.5
    assert stub.attempts == 2
    assert caught.value.retry_after_s == 1.5


async def test_rate_limit_with_no_stated_delay_still_retries_once(
    bookings: ApprovalService,
    stub: StubModelServer,
    db: Database,
    clock: FakeClock,
    gateway: ToolGateway,
    workflow: WorkflowService,
) -> None:
    stub.status = 429
    with pytest.raises(ModelRateLimited) as caught:
        await run_turn(bookings, stub, db, clock, gateway, workflow)
    assert stub.attempts == 2
    assert caught.value.retry_after_s is None


async def test_long_rate_limit_is_not_waited_for(
    bookings: ApprovalService,
    stub: StubModelServer,
    db: Database,
    clock: FakeClock,
    gateway: ToolGateway,
    workflow: WorkflowService,
) -> None:
    """Over the limit, the user is told right away instead of being left waiting."""
    stub.status = 429
    stub.retry_delay = "45s"
    started = time.monotonic()
    with pytest.raises(ModelRateLimited) as caught:
        await run_turn(bookings, stub, db, clock, gateway, workflow)
    assert time.monotonic() - started < 5
    assert stub.attempts == 1
    assert caught.value.retry_after_s == 45


async def test_permanent_error_is_not_retried(
    bookings: ApprovalService,
    stub: StubModelServer,
    db: Database,
    clock: FakeClock,
    gateway: ToolGateway,
    workflow: WorkflowService,
) -> None:
    stub.status = 404
    with pytest.raises(ModelUnavailable):
        await run_turn(bookings, stub, db, clock, gateway, workflow)
    assert stub.attempts == 1
