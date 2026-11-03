"""Provider cancels (E24, E25), the event stream and trace files, against the real mock provider."""

import json
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.core.clock import SystemClock
from app.core.database import Database
from app.domains.memory.assembler import Turn, with_updates
from app.domains.proactive.models import BookingDisruption
from app.domains.proactive.policy import Reason, decide, replacement_search
from app.domains.proactive.service import CANCEL_NOTICE, REPLACEMENT_NOTICE
from app.domains.tracing.jsonl import JsonlTraceFiles
from app.domains.tracing.service import TraceService
from app.domains.workflow.models import WorkflowState as S
from tests.unit import test_bookings
from tests.unit.test_bookings import SESSION, USER, Stack

stack = test_bookings.stack


async def booked_then_cancelled(stack: Stack) -> dict[str, Any]:
    await stack.book(book_outcomes=["success"])
    booking = (await stack.provider_bookings())[0]
    response = await stack.provider.post(
        "/admin/provider-cancel", json={"booking_id": booking["booking_id"]}
    )
    assert response.status_code == 200, response.text
    return booking


async def rows(stack: Stack) -> list[BookingDisruption]:
    async with stack.db.session_scope() as session:
        return list(await session.scalars(select(BookingDisruption)))


async def test_cancel_is_told_once_with_replacements_and_no_hold(stack: Stack) -> None:
    booking = await booked_then_cancelled(stack)
    holds_before = stack.transport.calls.count("hold_slot")

    await stack.core.proactive.poll()
    await stack.core.proactive.poll()

    kinds = [note.kind for note in await stack.notes()]
    assert kinds.count(CANCEL_NOTICE) == 1
    assert kinds.count(REPLACEMENT_NOTICE) == 1
    assert stack.transport.calls.count("hold_slot") == holds_before
    assert await stack.state() == S.COMPARING
    assert (await stack.core.approvals.view(USER, SESSION)).card is None

    row = (await rows(stack))[0]
    assert row.reason == Reason.REPLACEMENTS_OFFERED.value
    assert row.repeats_seen == 1
    snapshot = await stack.core.workflows.snapshot(USER, SESSION)
    assert booking["provider_id"] not in snapshot.shortlist
    assert snapshot.shortlist


async def test_a_running_turn_delays_the_reopen_but_not_the_news(stack: Stack) -> None:
    await booked_then_cancelled(stack)

    async with stack.core.turn_lock.hold(f"{USER}:{SESSION}"):
        await stack.core.proactive.poll()
        assert [note.kind for note in await stack.notes()].count(CANCEL_NOTICE) == 1
        assert (await rows(stack))[0].status == "detected"

    await stack.core.proactive.poll()
    assert (await rows(stack))[0].status == "closed"
    assert await stack.state() == S.COMPARING


async def test_another_errand_in_the_chat_is_left_alone(stack: Stack) -> None:
    await booked_then_cancelled(stack)
    await stack.core.workflows.record_user_message(USER, SESSION, "electrician in Koramangala")
    await stack.core.workflows.update_request(
        USER, SESSION, {"service": "electrician", "area": "Koramangala"}
    )

    await stack.core.proactive.poll()

    assert (await rows(stack))[0].reason == Reason.ERRAND_IN_PROGRESS.value
    snapshot = await stack.core.workflows.snapshot(USER, SESSION)
    assert snapshot.details["service"] == "electrician"


def test_the_systems_news_sits_in_the_chat_in_time_order() -> None:
    """BUG: "thanks" after a verified booking got "please confirm the card"."""
    start = datetime(2026, 11, 3, 10, tzinfo=UTC)
    asked = Turn("assistant", "Please confirm the card.", start)
    thanks = Turn("user", "thanks", start + timedelta(minutes=2))
    booked = Turn("assistant", "Booked and verified.", start + timedelta(minutes=1))

    assert with_updates([asked, thanks], [booked]) == [asked, booked, thanks]


def test_past_bookings_and_repeats_get_no_notice() -> None:
    cancelled = {"status": "cancelled_by_provider", "when": "upcoming"}
    assert decide(cancelled, True, False, False).notify
    past = decide({**cancelled, "when": "past"}, True, False, False)
    assert (past.notify, past.record, past.reason) == (False, True, Reason.PAST_BOOKING)
    assert decide(cancelled, True, True, False).reason == Reason.DUPLICATE
    assert decide(cancelled, False, False, False).reason == Reason.NOT_OUR_BOOKING
    assert decide(cancelled, True, False, True).reason == Reason.CHANGE_IN_PROGRESS
    assert not decide({"status": "confirmed", "when": "upcoming"}, True, False, False).notify


def test_replacement_search_uses_only_saved_limits() -> None:
    assert replacement_search({}, date(2026, 11, 3)) == Reason.NO_PREFERENCES
    request = {
        name: {"value": value}
        for name, value in {
            "service": "plumber",
            "area": "Indiranagar",
            "start_date": "2026-11-01",
            "end_date": "2026-11-05",
        }.items()
    }
    args = replacement_search(request, date(2026, 11, 3))
    assert isinstance(args, dict)
    assert args["date_range"] == {"start": "2026-11-03", "end": "2026-11-05"}
    assert replacement_search(request, date(2026, 11, 9)) == Reason.DATES_PASSED


async def test_stream_replays_after_the_cursor_and_then_pushes(stack: Stack) -> None:
    notifications = stack.core.notifications
    async with stack.db.session_scope() as session:
        await notifications.add_in(session, USER, SESSION, "a", "first")
        await notifications.add_in(session, USER, SESSION, "b", "second")
    first = (await stack.notes())[0]

    stream = notifications.stream(USER, first.id)
    item = await anext(stream)
    assert item is not None and item.text == "second"
    await stream.aclose()


async def test_trace_file_matches_the_database_and_scrubs(db: Database, tmp_path: Path) -> None:
    clock = SystemClock()
    files = JsonlTraceFiles(tmp_path, "api")
    traces = TraceService(db, clock, files)
    recorder = await traces.start_run(USER, SESSION)
    await recorder.event("tool_call", name="search", data={"area": "Indiranagar"})
    worker = await TraceService(db, clock, JsonlTraceFiles(tmp_path, "worker")).resume_run(
        recorder.run_id
    )
    await worker.event("saga_claimed")

    path = files.path_for(recorder.run_id)
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert [line["process"] for line in lines] == ["api", "api", "worker"]
    assert [line["seq"] for line in lines] == [0, 1, 2]
    assert {line["run_id"] for line in lines} == {str(recorder.run_id)}

    await traces.scrub_user(USER, re.compile("Indiranagar"))
    assert "Indiranagar" not in path.read_text()
    assert uuid.UUID(lines[0]["run_id"]) == recorder.run_id
