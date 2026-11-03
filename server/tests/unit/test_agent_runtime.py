"""Runs the real ADK loop with a scripted model, so no network is needed."""

import pytest
from google.adk.errors._stale_session_error import StaleSessionError
from google.adk.sessions import InMemorySessionService
from google.genai import errors as genai_errors

from app.core.config import Settings
from app.core.database import Database
from app.domains.agent.llm import build_model, build_request_config
from app.domains.agent.prompts import SYSTEM_PROMPT
from app.domains.agent.runtime import APP_NAME, AdkAgentRuntime
from app.domains.agent.scripted import ScriptedLlm, ToolCall
from app.domains.bookings.service import ApprovalService
from app.domains.chat.ports import ModelCallLimitExceeded, ModelUnavailable, SessionConflict
from app.domains.gateway.service import ToolGateway
from app.domains.memory.service import MemoryService
from app.domains.tracing.service import RunRecorder, TraceService
from app.domains.workflow.service import WorkflowService
from tests.conftest import FakeClock


@pytest.fixture
def traces(db: Database, clock: FakeClock) -> TraceService:
    return TraceService(db, clock)


@pytest.fixture
async def recorder(traces: TraceService) -> RunRecorder:
    return await traces.start_run("u1", "s1")


@pytest.fixture
def runtime_with(
    bookings: ApprovalService,
    gateway: ToolGateway,
    workflow: WorkflowService,
    clock: FakeClock,
    memory: MemoryService,
):
    def build(
        script: list, sessions: InMemorySessionService | None = None, max_calls: int = 8
    ) -> tuple[AdkAgentRuntime, ScriptedLlm, InMemorySessionService]:
        model = ScriptedLlm(model="gemini-3.5-flash-lite", script=script)
        sessions = sessions or InMemorySessionService()
        runtime = AdkAgentRuntime(
            model,
            sessions,
            gateway,
            workflow,
            bookings,
            memory,
            max_model_calls=max_calls,
            max_tool_calls=12,
            today=lambda: clock.now().date(),
        )
        return runtime, model, sessions

    return build


async def test_returns_the_model_reply_and_sends_the_system_prompt(
    recorder: RunRecorder, runtime_with
) -> None:
    runtime, model, _ = runtime_with(["Which area are you in?"])
    reply = await runtime.run_turn("u1", "s1", "I need a plumber", recorder)

    assert reply == "Which area are you in?"
    assert "Errand Agent" in str(model.requests[0].config.system_instruction)
    assert SYSTEM_PROMPT.splitlines()[0] in str(model.requests[0].config.system_instruction)


async def test_second_turn_sees_the_first_turn(
    recorder: RunRecorder, runtime_with, memory: MemoryService
) -> None:
    runtime, model, _ = runtime_with(["Second reply."])
    await memory.record_turn("u1", "s1", "user", "My area is Indiranagar")
    await memory.record_turn("u1", "s1", "assistant", "First reply.")
    await memory.record_turn("u1", "s1", "user", "And the budget is 1500")
    await runtime.run_turn("u1", "s1", "And the budget is 1500", recorder)

    seen = str(model.requests[0].contents)
    assert "My area is Indiranagar" in seen
    assert "First reply." in seen
    assert "And the budget is 1500" in seen


async def test_sessions_are_separate_per_user(recorder: RunRecorder, runtime_with) -> None:
    runtime, model, sessions = runtime_with(["ok"])
    await runtime.run_turn("u1", "s1", "secret of user one", recorder)
    await runtime.run_turn("u2", "s1", "hello", recorder)

    assert "secret of user one" not in str(model.requests[1].contents)
    assert await sessions.get_session(app_name=APP_NAME, user_id="u2", session_id="s1")


async def test_trace_records_tokens_cost_and_latency(
    recorder: RunRecorder, traces: TraceService, runtime_with
) -> None:
    runtime, _, _ = runtime_with(["Done."])
    await runtime.run_turn("u1", "s1", "hi", recorder)

    trace = await traces.get_trace(recorder.run_id)
    event = next(item for item in trace.events if item.kind == "model_call")
    assert event.name == "gemini-3.5-flash-lite"
    assert (event.input_tokens, event.output_tokens) == (10, 5)
    assert event.estimated_cost_usd == pytest.approx((10 * 0.30 + 5 * 2.50) / 1_000_000)
    assert event.latency_ms is not None and event.latency_ms >= 0


async def test_invented_tool_runs_nothing_and_the_model_is_told(
    recorder: RunRecorder, traces: TraceService, runtime_with
) -> None:
    runtime, model, _ = runtime_with([ToolCall("book", {"hold_id": "h1"}), "I can't book that."])
    reply = await runtime.run_turn("u1", "s1", "Book it now", recorder)

    assert reply == "I can't book that."
    assert "is not available" in str(model.requests[1].contents)

    trace = await traces.get_trace(recorder.run_id)
    refused = [event for event in trace.events if event.kind == "tool_refused"]
    assert [event.name for event in refused] == ["book"]
    assert refused[0].data["args"] == {"hold_id": "h1"}
    model_calls = [event for event in trace.events if event.kind == "model_call"]
    assert model_calls[0].data["requested_tools"] == ["book"]


async def test_looping_model_stops_at_the_call_limit(recorder: RunRecorder, runtime_with) -> None:
    runtime, model, _ = runtime_with([ToolCall("book")], max_calls=3)
    with pytest.raises(ModelCallLimitExceeded):
        await runtime.run_turn("u1", "s1", "loop", recorder)
    assert len(model.requests) == 3


async def test_model_error_is_translated_and_traced(
    recorder: RunRecorder, traces: TraceService, runtime_with
) -> None:
    error = genai_errors.ServerError(503, {"error": {"message": "overloaded"}})
    runtime, _, _ = runtime_with([error])
    with pytest.raises(ModelUnavailable):
        await runtime.run_turn("u1", "s1", "hi", recorder)

    trace = await traces.get_trace(recorder.run_id)
    assert [event.kind for event in trace.events] == ["context_assembled", "model_error"]
    assert trace.events[1].data["error"] == "ServerError"


async def test_stale_session_write_is_translated(recorder: RunRecorder, runtime_with) -> None:
    class StaleSessions(InMemorySessionService):
        async def append_event(self, session, event):  # type: ignore[no-untyped-def]
            raise StaleSessionError("modified in storage")

    runtime, _, _ = runtime_with(["ok"], sessions=StaleSessions())
    with pytest.raises(SessionConflict):
        await runtime.run_turn("u1", "s1", "hi", recorder)


def test_live_model_has_a_retry_and_a_request_timeout() -> None:
    settings = Settings(model_mode="live", google_api_key="test", model_name="gemini-x")
    model = build_model(settings)

    assert model.model == "gemini-x"
    assert model.retry_options.attempts == 2  # type: ignore[attr-defined]
    assert set(model.retry_options.http_status_codes) == {503, 504}  # type: ignore[attr-defined]
    http_options = build_request_config(settings).http_options
    assert http_options is not None and http_options.timeout == 30_000


def test_scripted_mode_builds_the_scripted_model() -> None:
    assert isinstance(build_model(Settings(model_mode="scripted")), ScriptedLlm)
