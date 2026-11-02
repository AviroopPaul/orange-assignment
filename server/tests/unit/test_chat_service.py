"""Every exit path must end with one outcome, a clear reply and a finished run."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from redis.asyncio import Redis

from app.core.database import Database
from app.core.locks import RedisTurnLock, TurnLockTimeout, TurnLockUnavailable
from app.domains.chat.ports import ModelCallLimitExceeded, ModelUnavailable, SessionConflict
from app.domains.chat.schemas import ChatRequest, FailureReason, Outcome
from app.domains.chat.service import FAILURE_REPLIES, ChatService
from app.domains.tracing.service import RunRecorder, TraceService
from tests.conftest import FakeClock


class FakeRuntime:
    def __init__(self, result: str | Exception = "Which area are you in?") -> None:
        self.result = result
        self.calls: list[tuple[str, str, str]] = []

    async def run_turn(
        self, user_id: str, session_id: str, message: str, recorder: RunRecorder
    ) -> str:
        self.calls.append((user_id, session_id, message))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class HangingRuntime(FakeRuntime):
    async def run_turn(
        self, user_id: str, session_id: str, message: str, recorder: RunRecorder
    ) -> str:
        await asyncio.sleep(30)
        return "never"


class RaisingLock:
    def __init__(self, error: Exception) -> None:
        self.error = error

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        raise self.error
        yield


@pytest.fixture
def traces(db: Database, clock: FakeClock) -> TraceService:
    return TraceService(db, clock)


def build(
    runtime: FakeRuntime,
    traces: TraceService,
    redis: Redis,
    lock: object | None = None,
    configured: bool = True,
    deadline_s: float = 5,
) -> ChatService:
    return ChatService(
        runtime=runtime,
        turn_lock=lock or RedisTurnLock(redis, ttl_s=10, wait_s=0.1),  # type: ignore[arg-type]
        traces=traces,
        run_deadline_s=deadline_s,
        model_is_configured=configured,
    )


REQUEST = ChatRequest(user_id="u1", session_id="s1", message="Find me a plumber")


async def test_model_reply_is_returned(traces: TraceService, redis: Redis) -> None:
    runtime = FakeRuntime()
    response = await build(runtime, traces, redis).handle_message(REQUEST)

    assert response.outcome == Outcome.DONE
    assert response.reason is None
    assert response.reply == "Which area are you in?"
    assert response.session_id == "s1"
    assert runtime.calls == [("u1", "s1", "Find me a plumber")]


async def test_new_session_gets_a_generated_id(traces: TraceService, redis: Redis) -> None:
    runtime = FakeRuntime()
    service = build(runtime, traces, redis)
    first = await service.handle_message(ChatRequest(user_id="u1", message="hi"))
    second = await service.handle_message(ChatRequest(user_id="u1", message="hi"))
    assert first.session_id and first.session_id != second.session_id


@pytest.mark.parametrize(
    ("result", "reason", "outcome"),
    [
        (ModelUnavailable("503"), FailureReason.MODEL_UNAVAILABLE, Outcome.TOOL_FAILED),
        (ModelCallLimitExceeded("8"), FailureReason.MODEL_CALL_LIMIT, Outcome.LIMIT_REACHED),
        (SessionConflict("stale"), FailureReason.SESSION_CONFLICT, Outcome.TOOL_FAILED),
        (RuntimeError("bug"), FailureReason.UNEXPECTED_ERROR, Outcome.TOOL_FAILED),
        ("", FailureReason.EMPTY_MODEL_REPLY, Outcome.TOOL_FAILED),
        ("   \n", FailureReason.EMPTY_MODEL_REPLY, Outcome.TOOL_FAILED),
    ],
)
async def test_runtime_failures_map_to_a_clear_outcome(
    traces: TraceService,
    redis: Redis,
    result: str | Exception,
    reason: FailureReason,
    outcome: Outcome,
) -> None:
    response = await build(FakeRuntime(result), traces, redis).handle_message(REQUEST)

    assert response.outcome == outcome
    assert response.reason == reason
    assert response.reply == FAILURE_REPLIES[reason][1]

    trace = await traces.get_trace(response.run_id)
    assert trace.outcome == outcome.value
    assert trace.reason == reason.value
    assert trace.finished_at is not None
    assert trace.events[-1].kind == "outcome"


async def test_internal_error_text_never_reaches_the_user(
    traces: TraceService, redis: Redis
) -> None:
    runtime = FakeRuntime(RuntimeError("password=hunter2 at db.internal"))
    response = await build(runtime, traces, redis).handle_message(REQUEST)
    assert "hunter2" not in response.reply


async def test_run_deadline_stops_a_stuck_model(traces: TraceService, redis: Redis) -> None:
    service = build(HangingRuntime(), traces, redis, deadline_s=0.05)
    response = await asyncio.wait_for(service.handle_message(REQUEST), timeout=2)
    assert response.outcome == Outcome.LIMIT_REACHED
    assert response.reason == FailureReason.RUN_DEADLINE


async def test_no_key_replies_clearly_and_never_calls_the_model(
    traces: TraceService, redis: Redis
) -> None:
    runtime = FakeRuntime()
    response = await build(runtime, traces, redis, configured=False).handle_message(REQUEST)
    assert response.reason == FailureReason.MODEL_NOT_CONFIGURED
    assert "GOOGLE_API_KEY" in response.reply
    assert runtime.calls == []


async def test_redis_down_fails_closed_and_never_calls_the_model(
    traces: TraceService, redis: Redis
) -> None:
    runtime = FakeRuntime()
    lock = RaisingLock(TurnLockUnavailable("down"))
    response = await build(runtime, traces, redis, lock=lock).handle_message(REQUEST)
    assert response.reason == FailureReason.LOCK_UNAVAILABLE
    assert runtime.calls == []


async def test_busy_session_replies_clearly_and_never_calls_the_model(
    traces: TraceService, redis: Redis
) -> None:
    runtime = FakeRuntime()
    lock = RaisingLock(TurnLockTimeout("s1"))
    response = await build(runtime, traces, redis, lock=lock).handle_message(REQUEST)
    assert response.reason == FailureReason.SESSION_BUSY
    assert runtime.calls == []


async def test_two_messages_at_once_run_one_after_the_other(
    traces: TraceService, redis: Redis
) -> None:
    active = 0
    peak = 0

    class Tracking(FakeRuntime):
        async def run_turn(
            self, user_id: str, session_id: str, message: str, recorder: RunRecorder
        ) -> str:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            return "ok"

    service = build(Tracking(), traces, redis, lock=RedisTurnLock(redis, ttl_s=5, wait_s=2))
    responses = await asyncio.gather(
        service.handle_message(REQUEST), service.handle_message(REQUEST)
    )

    assert peak == 1
    assert [response.outcome for response in responses] == [Outcome.DONE, Outcome.DONE]


def test_every_failure_reason_has_a_reply() -> None:
    assert set(FAILURE_REPLIES) == set(FailureReason)


def test_failure_replies_follow_the_six_outcome_contract() -> None:
    assert {outcome for outcome, _ in FAILURE_REPLIES.values()} <= set(Outcome)
    assert len(Outcome) == 6
