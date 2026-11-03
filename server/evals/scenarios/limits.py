"""E27, E28 and E30: a model or a tool that misbehaves."""

import os
from unittest.mock import patch

from aiohttp import web
from pydantic import SecretStr

from app.core.config import Settings
from app.domains.agent.llm import build_model
from app.domains.agent.scripted import ToolCall
from evals.scenario import Eval, scenario


@scenario(
    "E27",
    "Model loops until the call limit",
    "Limit exhaustion",
    "Stops at the limit with a clear reply.",
)
async def e27(run: Eval) -> None:
    response = await run.say("List my bookings", ToolCall("list_bookings"))
    run.expect("outcome", response["outcome"], "limit_reached")
    run.expect("reason", response["reason"], "model_call_limit")
    # ADK writes the context for the call it then refuses, so count what reached the model.
    calls = len(run.stack.switchboard.active.requests)
    run.expect("model_calls_made", calls, run.stack.settings.max_model_calls)
    run.expect("has_a_reply", bool(response["reply"]))
    run.expect("state", await run.state(), "limit_reached")


@scenario(
    "E28",
    "Tool calls hit the limit",
    "Limit exhaustion",
    "Gateway refuses. Clear reply.",
)
async def e28(run: Eval) -> None:
    limit = run.stack.settings.max_tool_calls
    response = await run.say("List my bookings", [ToolCall("list_bookings")] * (limit + 3))
    run.expect("outcome", response["outcome"], "limit_reached")
    run.expect("reason", response["reason"], "tool_call_limit")
    refused = [e.data["reason"] for e in await run.all_events("tool_refused")]
    run.expect_that("refused_for_budget", refused, "budget_exhausted" in refused)
    calls = await run.all_events("tool_call")
    run.expect_that("tool_calls_made", len(calls), len(calls) <= limit)
    run.expect("has_a_reply", bool(response["reply"]))


class StubModel:
    """Answers like Gemini during an outage, on a local port. Counts what reaches it."""

    def __init__(self) -> None:
        self.attempts = 0
        self.runner: web.AppRunner | None = None
        self.url = ""

    async def _handle(self, request: web.Request) -> web.Response:
        self.attempts += 1
        error = {"code": 503, "status": "UNAVAILABLE", "message": "stub outage"}
        return web.json_response({"error": error}, status=503)

    async def start(self) -> None:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 18200)
        await site.start()
        self.url = "http://127.0.0.1:18200"

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()


@scenario(
    "E30",
    "Model 503 and unexpected tool error",
    "Tool failures",
    "One retry, then a clear reply. No hang.",
)
async def e30(run: Eval) -> None:
    stub = StubModel()
    await stub.start()
    settings = Settings(
        model_mode="live", google_api_key=SecretStr("stub-key"), model_name="stub-model"
    )
    model = build_model(settings)
    model.base_url = stub.url  # type: ignore[attr-defined]
    run.stack.switchboard.active = model
    try:
        with patch.dict(os.environ, {"GOOGLE_API_KEY": "stub-key"}):
            response = await run.post_chat("Hello")
    finally:
        await stub.stop()
    run.expect("model_requests_with_one_retry", stub.attempts, 2)
    run.expect("outcome", response["outcome"], "tool_failed")
    run.expect("reason", response["reason"], "model_unavailable")

    workflows = run.stack.container.core.workflows
    await run.say(
        "I need a plumber in Indiranagar next week",
        ToolCall("update_request", {"service": "plumber", "area": "Indiranagar"}),
        ToolCall("resolve_dates", {"phrase": "next week"}),
        "Saved.",
    )
    with patch.object(workflows, "finish_search", side_effect=RuntimeError("injected")):
        failed = await run.say(
            "Search now", ToolCall("search_providers"), "That search failed. Please try again."
        )
    errors = [event.name for event in await run.all_events("tool_error")]
    run.expect("tool_error_traced", errors, ["search_providers"])
    run.expect("model_told_and_replied", failed["outcome"], "done")
    recovered = await run.say("Try again", ToolCall("search_providers"), "Here are some options.")
    run.expect("next_search_works", recovered["reason"], None)
    run.expect("errand_kept", (await run.state()) in ("comparing",), True)
