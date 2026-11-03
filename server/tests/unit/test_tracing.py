import uuid
from unittest.mock import patch

import pytest
from sqlalchemy.exc import OperationalError

from app.core.database import Database
from app.domains.tracing.pricing import estimate_cost_usd
from app.domains.tracing.service import ModelUsage, RunNotFound, TraceService
from tests.conftest import FakeClock


@pytest.fixture
def traces(db: Database, clock: FakeClock) -> TraceService:
    return TraceService(db, clock)


def test_cost_uses_input_and_output_prices() -> None:
    assert estimate_cost_usd("gemini-3.5-flash-lite", 1_000_000, 1_000_000) == pytest.approx(2.80)


def test_unknown_model_has_no_cost_rather_than_a_wrong_one() -> None:
    assert estimate_cost_usd("some-other-model", 100, 100) is None


async def test_run_records_ordered_events_and_totals(traces: TraceService) -> None:
    recorder = await traces.start_run("u1", "s1")
    await recorder.model_call(
        "gemini-3.5-flash-lite", ModelUsage(12.5, 100, 20), {"requested_tools": []}
    )
    await recorder.model_call(
        "gemini-3.5-flash-lite", ModelUsage(10.0, 200, 30), {"requested_tools": ["x"]}
    )
    await traces.finish_run(recorder, "done", None, "All set.")

    trace = await traces.get_trace(recorder.run_id)
    assert [event.seq for event in trace.events] == [1, 2, 3]
    assert [event.kind for event in trace.events] == ["model_call", "model_call", "outcome"]
    assert trace.events[0].latency_ms == 12.5
    assert trace.events[1].data == {"requested_tools": ["x"]}
    assert (trace.model_calls, trace.input_tokens, trace.output_tokens) == (2, 300, 50)
    assert trace.estimated_cost_usd == pytest.approx((300 * 0.30 + 50 * 2.50) / 1_000_000)
    assert trace.outcome == "done"
    assert trace.reply == "All set."
    assert trace.finished_at is not None


async def test_unknown_run_is_reported(traces: TraceService) -> None:
    with pytest.raises(RunNotFound):
        await traces.get_trace(uuid.uuid4())


async def test_trace_write_failure_does_not_reach_the_caller(
    traces: TraceService, db: Database
) -> None:
    recorder = await traces.start_run("u1", "s1")
    error = OperationalError("insert", {}, Exception("database is down"))
    with patch.object(db, "session_scope", side_effect=error):
        await recorder.event("model_error")
        await traces.finish_run(recorder, "tool_failed", "model_unavailable", "Sorry.")


async def test_two_writers_on_one_run_keep_every_event(db: Database, clock: FakeClock) -> None:
    """The saga continues a run's trace, maybe while the turn that started it still finishes."""
    traces = TraceService(db, clock)
    first = await traces.start_run("u1", "s1")
    await first.event("one")
    second = await traces.resume_run(first.run_id)
    await second.event("two")
    await first.event("three")
    await second.event("four")

    trace = await traces.get_trace(first.run_id)
    assert sorted(event.kind for event in trace.events) == ["four", "one", "three", "two"]
    assert len({event.seq for event in trace.events}) == 4


async def test_settling_a_run_keeps_its_model_counts(db: Database, clock: FakeClock) -> None:
    traces = TraceService(db, clock)
    recorder = await traces.start_run("u1", "s1")
    await recorder.model_call("gemini-3.5-flash-lite", ModelUsage(10, 100, 20), {})
    await traces.finish_run(recorder, "verifying", None, "in progress")

    resumed = await traces.resume_run(recorder.run_id)
    await traces.settle_run(resumed, "done", "booked")

    trace = await traces.get_trace(recorder.run_id)
    assert (trace.outcome, trace.reply) == ("done", "booked")
    assert trace.input_tokens == 100
