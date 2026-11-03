"""Every exit path must end with one outcome, a clear reply and a finished run."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from redis.asyncio import Redis

from app.core.database import Database
from app.core.locks import RedisTurnLock, TurnLockTimeout, TurnLockUnavailable
from app.domains.bookings.service import ApprovalService
from app.domains.chat.ports import (
    ModelCallLimitExceeded,
    ModelRateLimited,
    ModelUnavailable,
    SessionConflict,
    ToolCallLimitExceeded,
)
from app.domains.chat.schemas import ChatRequest, FailureReason, Outcome
from app.domains.chat.service import FAILURE_REPLIES, ChatService
from app.domains.gateway.client import ProviderClient
from app.domains.gateway.service import DenyAllApprovals, ToolGateway
from app.domains.jobs.service import JobService
from app.domains.memory.service import MemoryService
from app.domains.notifications.service import NotificationService
from app.domains.tracing.service import RunRecorder, TraceService
from app.domains.workflow.models import WorkflowState
from app.domains.workflow.service import WorkflowService
from tests.conftest import FakeClock, RecordingPublisher, make_ready


class FakeRuntime:
    def __init__(self, result: str | Exception = "Which area are you in?") -> None:
        self.result = result
        self.calls: list[tuple[str, str, str]] = []

    async def rotate_session(self, user_id: str, session_id: str) -> None:
        return None

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
    workflows = WorkflowService(traces._db, traces._clock)
    return ChatService(
        runtime=runtime,
        turn_lock=lock or RedisTurnLock(redis, ttl_s=10, wait_s=0.1),  # type: ignore[arg-type]
        traces=traces,
        workflows=workflows,
        bookings=ApprovalService(
            traces._db,
            traces._clock,
            ToolGateway(ProviderClient("http://nowhere", 1), DenyAllApprovals(), 0),
            workflows,
            JobService(traces._db, RecordingPublisher(), {}, traces._clock, 45, 60, 5),
            traces,
            NotificationService(traces._db, traces._clock),
        ),
        run_deadline_s=deadline_s,
        model_is_configured=configured,
        memory=MemoryService(traces._db, traces._clock, workflows, traces),
    )


REQUEST = ChatRequest(user_id="u1", session_id="s1", message="Find me a plumber")


async def test_model_reply_is_returned(traces: TraceService, redis: Redis) -> None:
    runtime = FakeRuntime()
    response = await build(runtime, traces, redis).handle_message(REQUEST)

    assert response.outcome == Outcome.NEEDS_INPUT
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
    assert [response.reason for response in responses] == [None, None]


def test_every_failure_reason_has_a_reply() -> None:
    assert set(FAILURE_REPLIES) == set(FailureReason)


def test_failure_replies_follow_the_six_outcome_contract() -> None:
    assert {outcome for outcome, _ in FAILURE_REPLIES.values()} <= set(Outcome)
    assert len(Outcome) == 6


async def test_reply_is_done_once_nothing_is_missing(
    traces: TraceService, redis: Redis, workflow: WorkflowService
) -> None:
    await make_ready(workflow, "u1", "s1")
    response = await build(FakeRuntime("Here you go."), traces, redis).handle_message(REQUEST)
    assert response.outcome == Outcome.DONE


async def test_users_words_reach_the_workflow(
    traces: TraceService, redis: Redis, workflow: WorkflowService, db
) -> None:
    await build(FakeRuntime(), traces, redis).handle_message(
        ChatRequest(user_id="u1", session_id="s1", message="I live in Koramangala")
    )
    result = await workflow.update_request("u1", "s1", {"area": "Koramangala"})
    assert "rejected" not in result


@pytest.mark.parametrize(
    ("retry_after", "seconds", "expected"),
    [(45.2, 46, "in about 46 seconds"), (None, None, "shortly")],
)
async def test_rate_limit_says_busy_and_when_to_retry(
    traces: TraceService,
    redis: Redis,
    retry_after: float | None,
    seconds: int | None,
    expected: str,
) -> None:
    runtime = FakeRuntime(ModelRateLimited(retry_after))
    response = await build(runtime, traces, redis).handle_message(REQUEST)

    assert response.outcome == Outcome.LIMIT_REACHED
    assert response.reason == FailureReason.MODEL_RATE_LIMITED
    assert response.retry_after_s == seconds
    assert expected in response.reply
    assert "busy" in response.reply
    assert "not responding" not in response.reply

    trace = await traces.get_trace(response.run_id)
    event = next(item for item in trace.events if item.kind == "model_rate_limited")
    assert event.data["retry_after_s"] == seconds


async def test_rate_limit_does_not_stop_the_errand(
    traces: TraceService, redis: Redis, workflow: WorkflowService
) -> None:
    await build(FakeRuntime(ModelRateLimited(10)), traces, redis).handle_message(REQUEST)
    assert (await workflow.snapshot("u1", "s1")).state == WorkflowState.INTAKE


@pytest.mark.parametrize("error", [ModelCallLimitExceeded("8"), ToolCallLimitExceeded("12")])
async def test_hitting_a_run_limit_is_recorded_and_the_next_message_resumes(
    traces: TraceService, redis: Redis, workflow: WorkflowService, error: Exception
) -> None:
    await build(FakeRuntime(error), traces, redis).handle_message(REQUEST)
    assert (await workflow.snapshot("u1", "s1")).state == WorkflowState.LIMIT_REACHED

    await build(FakeRuntime(), traces, redis).handle_message(REQUEST)
    assert (await workflow.snapshot("u1", "s1")).state == WorkflowState.INTAKE


async def test_deadline_is_recorded_as_a_limit(
    traces: TraceService, redis: Redis, workflow: WorkflowService
) -> None:
    await build(HangingRuntime(), traces, redis, deadline_s=0.05).handle_message(REQUEST)
    assert (await workflow.snapshot("u1", "s1")).state == WorkflowState.LIMIT_REACHED


class SavingDatesRuntime(FakeRuntime):
    """Stands in for a model that saved dates this turn, then wrote `result` as its reply."""

    def __init__(self, workflow: WorkflowService, result: str, phrase: str = "Saturday") -> None:
        super().__init__(result)
        self.workflow = workflow
        self.phrase = phrase

    async def run_turn(
        self, user_id: str, session_id: str, message: str, recorder: RunRecorder
    ) -> str:
        await self.workflow.resolve_dates(user_id, session_id, self.phrase)
        return await super().run_turn(user_id, session_id, message, recorder)


ON_SATURDAY = ChatRequest(user_id="u1", session_id="s1", message="A plumber on Saturday")


async def test_user_always_sees_the_dates_even_if_the_model_leaves_them_out(
    traces: TraceService, redis: Redis, workflow: WorkflowService
) -> None:
    runtime = SavingDatesRuntime(workflow, "Thanks. What is your budget?")
    response = await build(runtime, traces, redis).handle_message(ON_SATURDAY)

    assert response.reply == ("Dates: This Saturday (07/11/2026).\n\nThanks. What is your budget?")
    assert response.outcome == Outcome.NEEDS_INPUT


async def test_a_reply_that_shows_the_dates_is_left_as_written(
    traces: TraceService, redis: Redis, workflow: WorkflowService
) -> None:
    written = "Got it, this Saturday (07/11/2026). Which area are you in?"
    runtime = SavingDatesRuntime(workflow, written)
    response = await build(runtime, traces, redis).handle_message(ON_SATURDAY)
    assert response.reply == written


async def test_a_range_needs_both_ends_shown(
    traces: TraceService, redis: Redis, workflow: WorkflowService
) -> None:
    runtime = SavingDatesRuntime(workflow, "Starting 02/11/2026. Which area?", "this week")
    request = ChatRequest(user_id="u1", session_id="s1", message="A plumber this week")
    response = await build(runtime, traces, redis).handle_message(request)
    assert response.reply.startswith("Dates: This week (02/11/2026 to 08/11/2026).")


async def test_the_dates_are_shown_once(
    traces: TraceService, redis: Redis, workflow: WorkflowService
) -> None:
    runtime = SavingDatesRuntime(workflow, "Which area?")
    await build(runtime, traces, redis).handle_message(ON_SATURDAY)

    next_message = ChatRequest(user_id="u1", session_id="s1", message="Indiranagar")
    response = await build(FakeRuntime("Searching."), traces, redis).handle_message(next_message)
    assert response.reply == "Searching."


async def test_dates_from_a_failed_turn_are_shown_by_the_next_reply(
    traces: TraceService, redis: Redis, workflow: WorkflowService
) -> None:
    await workflow.record_user_message("u1", "s1", "A plumber on Saturday")
    await workflow.resolve_dates("u1", "s1", "Saturday")

    next_message = ChatRequest(user_id="u1", session_id="s1", message="Indiranagar")
    response = await build(FakeRuntime("Searching."), traces, redis).handle_message(next_message)
    assert response.reply.startswith("Dates: This Saturday (07/11/2026).")


async def test_a_plain_yes_is_not_read_as_a_date_answer(
    traces: TraceService, redis: Redis, workflow: WorkflowService
) -> None:
    yes = ChatRequest(user_id="u1", session_id="s1", message="yes")
    response = await build(FakeRuntime("What would you like done?"), traces, redis).handle_message(
        yes
    )
    assert response.reply == "What would you like done?"
    assert (await workflow.snapshot("u1", "s1")).details == {}
