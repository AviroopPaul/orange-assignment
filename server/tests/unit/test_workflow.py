"""The state machine, the request's rules and the hard limits."""

from unittest.mock import patch

import pytest

from app.core.database import Database
from app.domains.gateway.schemas import RefusalReason
from app.domains.tracing.service import TraceService
from app.domains.workflow import request as details
from app.domains.workflow.filters import MAX_SLOTS_SHOWN, apply_hard_limits
from app.domains.workflow.models import Workflow
from app.domains.workflow.models import WorkflowState as S
from app.domains.workflow.repository import WorkflowRepository
from app.domains.workflow.service import (
    WorkflowConflict,
    WorkflowSearchGuard,
    WorkflowService,
)
from app.domains.workflow.state_machine import TRANSITIONS, IllegalTransition, check_transition
from tests.conftest import FakeClock, make_ready

USER, SESSION = "u1", "s1"
DETAILS = {"service": "plumber", "area": "Indiranagar"}
COMPLETE = {
    "service": "plumber",
    "area": "Indiranagar",
    "start_date": "2026-11-03",
    "end_date": "2026-11-05",
}

# State machine


def test_every_state_has_a_row() -> None:
    assert set(TRANSITIONS) == set(S)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.INTAKE, S.SEARCHING),
        (S.SEARCHING, S.COMPARING),
        (S.COMPARING, S.AWAITING_APPROVAL),
        (S.AWAITING_APPROVAL, S.EXECUTING),
        (S.EXECUTING, S.VERIFYING),
        (S.VERIFYING, S.DONE),
        (S.COMPARING, S.INTAKE),
        (S.VERIFYING, S.EXECUTING),
        (S.DONE, S.DISRUPTED),
        (S.DISRUPTED, S.COMPARING),
        (S.LIMIT_REACHED, S.INTAKE),
    ],
)
def test_legal_moves_pass(current: S, target: S) -> None:
    check_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        # Nothing may skip approval.
        (S.INTAKE, S.EXECUTING),
        (S.SEARCHING, S.EXECUTING),
        (S.COMPARING, S.EXECUTING),
        # Nothing may report success without the status check.
        (S.EXECUTING, S.DONE),
        (S.AWAITING_APPROVAL, S.DONE),
        (S.INTAKE, S.DONE),
        # A booking in flight cannot be abandoned or interrupted by a run limit.
        (S.EXECUTING, S.INTAKE),
        (S.EXECUTING, S.LIMIT_REACHED),
        (S.VERIFYING, S.LIMIT_REACHED),
        # Finished means finished.
        (S.FAILED, S.INTAKE),
        (S.DONE, S.INTAKE),
    ],
)
def test_illegal_moves_raise(current: S, target: S) -> None:
    with pytest.raises(IllegalTransition):
        check_transition(current, target)


def test_only_verifying_reaches_done() -> None:
    reaches_done = {state for state, targets in TRANSITIONS.items() if S.DONE in targets}
    assert reaches_done == {S.VERIFYING}


def test_only_approval_reaches_executing_for_the_first_time() -> None:
    reaches = {state for state, targets in TRANSITIONS.items() if S.EXECUTING in targets}
    assert reaches == {S.AWAITING_APPROVAL, S.VERIFYING}


# Request rules


@pytest.mark.parametrize(
    ("value", "said", "expected"),
    [
        ("Indiranagar", "a plumber in indiranagar please", True),
        ("HSR Layout", "I live in HSR", True),
        ("Jayanagar", "Book the same electrician as last time", False),
        ("Jayanagar", "", False),
        ("MG", "I live on MG road", False),
    ],
)
def test_area_must_come_from_the_user(value: str, said: str, expected: bool) -> None:
    assert details.is_area_grounded(value, said) is expected


@pytest.mark.parametrize(
    ("value", "said", "expected"),
    [
        (1500, "under ₹1,500", True),
        (1500, "budget 1500", True),
        (2000, "maybe 2k at most", True),
        (1500, "up to 1.5k", True),
        (1500, "around 15000", False),
        (5000, "any budget", False),
    ],
)
def test_budget_must_come_from_the_user(value: int, said: str, expected: bool) -> None:
    assert details.is_budget_grounded(value, said) is expected


@pytest.mark.parametrize(
    ("name", "value", "fragment"),
    [
        ("earliest_time", "10am", "HH:MM"),
        ("latest_time", "25:00", "HH:MM"),
        ("start_date", "2026-11-05", "resolve_dates"),
        ("end_date", "2026-11-05", "resolve_dates"),
    ],
)
def test_bad_values_are_explained(name: str, value: str, fragment: str) -> None:
    assert fragment in (details.rejection(name, value, "") or "")


def entries(**values: object) -> dict:
    return {name: {"value": value} for name, value in values.items()}


def test_missing_required_lists_only_what_is_missing() -> None:
    assert details.missing_required({}) == list(details.REQUIRED_FOR_SEARCH)
    assert details.missing_required(entries(service="x", area="y")) == ["start_date", "end_date"]


def test_budget_and_time_are_not_required_to_search() -> None:
    assert details.missing_required(entries(**COMPLETE)) == []


def test_search_args_match_the_tool_contract() -> None:
    assert details.search_args(entries(**COMPLETE)) == {
        "service": "plumber",
        "area": "Indiranagar",
        "date_range": {"start": "2026-11-03", "end": "2026-11-05"},
    }


@pytest.mark.parametrize(
    ("values", "field"),
    [
        ({"earliest_time": "12:00", "latest_time": "10:00"}, "latest_time"),
        ({"earliest_time": "10:00", "latest_time": "10:00"}, "latest_time"),
    ],
)
def test_pairs_must_make_sense_together(values: dict, field: str) -> None:
    assert field in details.pair_rejections(entries(**values))


# Hard limits


def provider(id_: str, price: int, slots: list[str]) -> dict:
    return {
        "id": id_,
        "name": id_,
        "price": price,
        "currency": "INR",
        "rating": 4.5,
        "review_count": 10,
        "available_slots": slots,
        "attributes": {"technician": "Ravi", "years_experience": 5, "verified": True},
    }


MORNING = "2026-11-03T09:00:00+05:30"
TEN = "2026-11-03T10:00:00+05:30"
NOON = "2026-11-03T12:00:00+05:30"
EVENING = "2026-11-03T18:00:00+05:30"


def test_budget_is_a_hard_limit_and_includes_the_exact_amount() -> None:
    result = apply_hard_limits(
        [
            provider("under", 999, [MORNING]),
            provider("exact", 1000, [MORNING]),
            provider("over", 1001, [MORNING]),
        ],
        entries(max_budget=1000),
    )
    assert [item["id"] for item in result["providers"]] == ["under", "exact"]
    assert result["removed"]["over_budget"] == 1


def test_earliest_time_includes_that_minute() -> None:
    result = apply_hard_limits(
        [provider("p", 100, [MORNING, TEN, NOON])], entries(earliest_time="10:00")
    )
    assert result["providers"][0]["slots"] == [TEN, NOON]


def test_latest_time_excludes_that_minute_so_mornings_end_before_noon() -> None:
    result = apply_hard_limits(
        [provider("p", 100, [MORNING, TEN, NOON])], entries(latest_time="12:00")
    )
    assert result["providers"][0]["slots"] == [MORNING, TEN]


def test_provider_with_no_slot_in_the_window_is_removed() -> None:
    result = apply_hard_limits(
        [provider("late", 100, [EVENING]), provider("early", 100, [MORNING])],
        entries(latest_time="12:00"),
    )
    assert [item["id"] for item in result["providers"]] == ["early"]
    assert result["removed"]["no_slot_in_time_window"] == 1


def test_with_no_limits_everyone_with_a_slot_stays() -> None:
    result = apply_hard_limits([provider("a", 99999, [MORNING]), provider("b", 1, [EVENING])], {})
    assert len(result["providers"]) == 2
    assert result["removed"] == {"over_budget": 0, "no_slot_in_time_window": 0}


def test_nothing_fits_says_why() -> None:
    result = apply_hard_limits([provider("a", 5000, [MORNING])], entries(max_budget=100))
    assert result["providers"] == []
    assert result["removed"]["over_budget"] == 1


def test_result_is_compact_and_says_what_was_cut() -> None:
    slots = [f"2026-11-0{day}T09:00:00+05:30" for day in range(3, 10)]
    item = apply_hard_limits([provider("p", 100, slots)], {})["providers"][0]
    assert len(item["slots"]) == MAX_SLOTS_SHOWN
    assert item["more_slots"] == 1
    assert "attributes" not in item


def test_unreadable_slot_is_dropped_rather_than_trusted() -> None:
    result = apply_hard_limits([provider("p", 100, ["soon"])], {})
    assert result["providers"] == []


# Service


async def started(
    workflow: WorkflowService, words: str = "plumber in Indiranagar under 1500"
) -> None:
    await workflow.record_user_message(USER, SESSION, words)


async def test_new_session_starts_at_intake_with_everything_missing(
    workflow: WorkflowService,
) -> None:
    snapshot = await workflow.snapshot(USER, SESSION)
    assert snapshot.state == S.INTAKE
    assert snapshot.workflow_id is None
    assert snapshot.missing_required == list(details.REQUIRED_FOR_SEARCH)


async def test_update_merges_one_field_and_leaves_the_rest(workflow: WorkflowService) -> None:
    await make_ready(workflow, USER, SESSION)
    await workflow.record_user_message(USER, SESSION, "under 1500")
    await workflow.update_request(USER, SESSION, {"max_budget": 1500})
    await workflow.update_request(USER, SESSION, {"earliest_time": "10:00"})

    snapshot = await workflow.snapshot(USER, SESSION)
    assert snapshot.details == {**COMPLETE, "earliest_time": "10:00", "max_budget": 1500}
    assert snapshot.missing_required == []


async def test_each_field_records_its_source_and_time(
    workflow: WorkflowService, db: Database, clock: FakeClock
) -> None:
    await started(workflow)
    await workflow.update_request(USER, SESSION, {"service": "plumber"})
    async with db.session_scope() as session:
        row = await WorkflowRepository(session).get_active(USER, SESSION)
    assert row is not None
    assert row.request["service"] == {
        "value": "plumber",
        "source": "user",
        "updated_at": clock.now().isoformat(),
    }


async def test_rejected_detail_does_not_block_the_good_ones(workflow: WorkflowService) -> None:
    await workflow.record_user_message(USER, SESSION, "I need a plumber")
    result = await workflow.update_request(
        USER, SESSION, {"service": "plumber", "area": "Jayanagar", "max_budget": 999}
    )
    assert set(result["rejected"]) == {"area", "max_budget"}
    assert result["saved"] == {"service": "plumber"}
    assert result["missing_fields"] == ["area", "start_date", "end_date"]


async def test_area_named_in_an_earlier_message_is_accepted(workflow: WorkflowService) -> None:
    await workflow.record_user_message(USER, SESSION, "I live in Koramangala")
    await workflow.record_user_message(USER, SESSION, "I need a plumber")
    result = await workflow.update_request(USER, SESSION, {"area": "Koramangala"})
    assert "rejected" not in result


async def test_bad_time_pair_keeps_the_earlier_value(workflow: WorkflowService) -> None:
    await make_ready(workflow, USER, SESSION)
    await workflow.update_request(USER, SESSION, {"latest_time": "12:00"})
    result = await workflow.update_request(
        USER, SESSION, {"latest_time": "08:00", "earliest_time": "10:00"}
    )
    assert "latest_time" in result["rejected"]
    assert result["saved"]["latest_time"] == "12:00"


async def test_optional_detail_can_be_cleared_but_a_required_one_cannot(
    workflow: WorkflowService,
) -> None:
    await make_ready(workflow, USER, SESSION)
    await workflow.update_request(USER, SESSION, {"latest_time": "12:00"})
    result = await workflow.update_request(USER, SESSION, {}, clear_fields=["latest_time", "area"])
    assert "latest_time" not in result["saved"]
    assert result["saved"]["area"] == "Indiranagar"
    assert "area" in result["rejected"]


async def test_begin_search_with_details_missing_does_not_move_state(
    workflow: WorkflowService,
) -> None:
    await started(workflow)
    await workflow.update_request(USER, SESSION, {"service": "plumber"})
    assert await workflow.begin_search(USER, SESSION) == {"service": "plumber"}
    assert (await workflow.snapshot(USER, SESSION)).state == S.INTAKE


async def test_search_moves_through_the_states(workflow: WorkflowService) -> None:
    await make_ready(workflow, USER, SESSION)
    await workflow.begin_search(USER, SESSION)
    assert (await workflow.snapshot(USER, SESSION)).state == S.SEARCHING
    await workflow.finish_search(USER, SESSION, [])
    assert (await workflow.snapshot(USER, SESSION)).state == S.COMPARING


async def test_correcting_a_search_input_goes_back_to_intake(workflow: WorkflowService) -> None:
    await make_ready(workflow, USER, SESSION)
    await workflow.begin_search(USER, SESSION)
    await workflow.finish_search(USER, SESSION, [])
    await workflow.record_user_message(USER, SESSION, "actually under 1500")

    result = await workflow.update_request(USER, SESSION, {"max_budget": 1500})

    assert result["search_needed"] is True
    assert (await workflow.snapshot(USER, SESSION)).state == S.INTAKE


async def test_saving_the_same_value_changes_nothing(workflow: WorkflowService) -> None:
    await make_ready(workflow, USER, SESSION)
    await workflow.begin_search(USER, SESSION)
    await workflow.finish_search(USER, SESSION, [])

    result = await workflow.update_request(USER, SESSION, {"area": "Indiranagar"})

    assert result["search_needed"] is False
    assert (await workflow.snapshot(USER, SESSION)).state == S.COMPARING


async def test_preference_change_does_not_force_a_new_search(workflow: WorkflowService) -> None:
    await make_ready(workflow, USER, SESSION)
    await workflow.begin_search(USER, SESSION)
    await workflow.finish_search(USER, SESSION, [])
    await workflow.update_request(USER, SESSION, {"preferences": ["Hindi speaking"]})
    assert (await workflow.snapshot(USER, SESSION)).state == S.COMPARING


async def test_state_changes_are_traced(
    workflow: WorkflowService, db: Database, clock: FakeClock
) -> None:
    traces = TraceService(db, clock)
    recorder = await traces.start_run(USER, SESSION)
    await make_ready(workflow, USER, SESSION)
    await workflow.begin_search(USER, SESSION, recorder)
    await workflow.finish_search(USER, SESSION, [], recorder)

    trace = await traces.get_trace(recorder.run_id)
    assert [event.data for event in trace.events] == [
        {"from": "intake", "to": "searching"},
        {"from": "searching", "to": "comparing"},
    ]


async def test_state_survives_a_restart(db: Database, clock: FakeClock) -> None:
    first = WorkflowService(db, clock)
    await make_ready(first, USER, SESSION)
    second = WorkflowService(db, clock)
    assert (await second.snapshot(USER, SESSION)).details == COMPLETE


async def test_crash_during_a_search_recovers_on_the_next_message(
    workflow: WorkflowService,
) -> None:
    await make_ready(workflow, USER, SESSION)
    await workflow.begin_search(USER, SESSION)
    await workflow.record_user_message(USER, SESSION, "hello?")
    assert (await workflow.snapshot(USER, SESSION)).state == S.INTAKE


async def test_sessions_do_not_share_a_workflow(workflow: WorkflowService) -> None:
    await make_ready(workflow, USER, SESSION)
    assert (await workflow.snapshot(USER, "other")).details == {}
    assert (await workflow.snapshot("other-user", SESSION)).details == {}


async def _force_state(db: Database, state: S) -> None:
    async with db.session_scope() as session:
        row = await WorkflowRepository(session).get_active(USER, SESSION)
        assert row is not None
        await WorkflowRepository(session).save(row.id, row.version, state=state.value)


async def test_search_cannot_interrupt_a_booking_in_flight(
    workflow: WorkflowService, db: Database
) -> None:
    await make_ready(workflow, USER, SESSION)
    await _force_state(db, S.EXECUTING)
    await workflow.begin_search(USER, SESSION)
    assert (await workflow.snapshot(USER, SESSION)).state == S.EXECUTING
    guard = WorkflowSearchGuard(workflow, USER, SESSION)
    blocked = await guard.check("search_providers", {})
    assert blocked is not None
    assert "booking request is in progress" in blocked[1]


@pytest.mark.parametrize("state", [S.EXECUTING, S.VERIFYING])
async def test_run_limit_leaves_a_booking_in_flight_alone(
    workflow: WorkflowService, db: Database, state: S
) -> None:
    await started(workflow)
    await _force_state(db, state)
    await workflow.mark_limit_reached(USER, SESSION)
    assert (await workflow.snapshot(USER, SESSION)).state == state


async def test_stale_write_is_refused(workflow: WorkflowService, db: Database) -> None:
    await started(workflow)
    async with db.session_scope() as session:
        repository = WorkflowRepository(session)
        row = await repository.get_active(USER, SESSION)
        assert row is not None
        read_version = row.version
        assert await repository.save(row.id, read_version, state=S.SEARCHING.value)
        assert not await repository.save(row.id, read_version, state=S.INTAKE.value)


async def test_constant_conflict_gives_up_instead_of_looping(workflow: WorkflowService) -> None:
    await started(workflow)
    with patch.object(WorkflowRepository, "save", return_value=False):
        with pytest.raises(WorkflowConflict):
            await workflow.update_request(USER, SESSION, {"service": "plumber"})


async def test_user_text_is_bounded(workflow: WorkflowService, db: Database) -> None:
    for _ in range(30):
        await workflow.record_user_message(USER, SESSION, "x" * 1000)
    async with db.session_scope() as session:
        row = await WorkflowRepository(session).get_active(USER, SESSION)
    assert isinstance(row, Workflow)
    assert len(row.user_text) <= 20_000


# Guard


async def test_guard_refuses_a_search_with_details_missing(workflow: WorkflowService) -> None:
    await started(workflow)
    await workflow.update_request(USER, SESSION, {"service": "plumber"})
    guard = WorkflowSearchGuard(workflow, USER, SESSION)
    blocked = await guard.check("search_providers", {"service": "plumber"})
    assert blocked is not None
    assert blocked[0] == RefusalReason.MISSING_DETAILS
    assert "area" in blocked[1]


async def test_guard_refuses_a_search_that_differs_from_the_saved_request(
    workflow: WorkflowService,
) -> None:
    await make_ready(workflow, USER, SESSION)
    guard = WorkflowSearchGuard(workflow, USER, SESSION)
    args = details.search_args(entries(**{**COMPLETE, "area": "Jayanagar"}))
    blocked = await guard.check("search_providers", args)
    assert blocked is not None
    assert blocked[0] == RefusalReason.REQUEST_MISMATCH


async def test_guard_allows_the_saved_request(workflow: WorkflowService) -> None:
    await make_ready(workflow, USER, SESSION)
    guard = WorkflowSearchGuard(workflow, USER, SESSION)
    assert await guard.check("search_providers", details.search_args(entries(**COMPLETE))) is None


async def test_guard_only_concerns_search(workflow: WorkflowService) -> None:
    guard = WorkflowSearchGuard(workflow, USER, SESSION)
    assert await guard.check("get_provider", {"provider_id": "plm-01"}) is None


# Dates: clear words are saved, and shown back to the user


async def asked(workflow: WorkflowService, words: str = "I need it on Saturday") -> None:
    await workflow.record_user_message(USER, SESSION, words)


async def test_clear_dates_are_saved_with_no_question(
    workflow: WorkflowService, db: Database
) -> None:
    await asked(workflow)
    result = await workflow.resolve_dates(USER, SESSION, "Saturday")

    assert result["saved"] is True
    assert result["dates"] == "This Saturday (07/11/2026)"
    snapshot = await workflow.snapshot(USER, SESSION)
    assert snapshot.details["start_date"] == "2026-11-07"
    assert snapshot.details["end_date"] == "2026-11-07"
    assert "start_date" not in snapshot.missing_required
    async with db.session_scope() as session:
        row = await WorkflowRepository(session).get_active(USER, SESSION)
    assert row is not None
    assert row.request["start_date"]["source"] == "user"


async def test_saved_dates_wait_to_be_shown_until_a_reply_shows_them(
    workflow: WorkflowService,
) -> None:
    await asked(workflow)
    await workflow.resolve_dates(USER, SESSION, "Saturday")
    snapshot = await workflow.snapshot(USER, SESSION)
    assert snapshot.dates_to_show == {
        "label": "This Saturday (07/11/2026)",
        "shown": ["07/11/2026"],
    }

    await workflow.note_dates_shown(USER, SESSION, "This Saturday (07/11/2026)")
    snapshot = await workflow.snapshot(USER, SESSION)
    assert snapshot.dates_to_show is None
    assert snapshot.details["start_date"] == "2026-11-07"


async def test_showing_old_dates_does_not_clear_newer_ones(workflow: WorkflowService) -> None:
    await asked(workflow, "Saturday or Sunday")
    await workflow.resolve_dates(USER, SESSION, "Saturday")
    await workflow.resolve_dates(USER, SESSION, "Sunday")

    await workflow.note_dates_shown(USER, SESSION, "This Saturday (07/11/2026)")

    snapshot = await workflow.snapshot(USER, SESSION)
    assert snapshot.dates_to_show is not None
    assert snapshot.dates_to_show["label"] == "This Sunday (08/11/2026)"


@pytest.mark.parametrize("message", ["yes", "no", "hmm", "not sure"])
async def test_a_short_answer_changes_no_dates(workflow: WorkflowService, message: str) -> None:
    await asked(workflow)
    await workflow.resolve_dates(USER, SESSION, "Saturday")
    await workflow.record_user_message(USER, SESSION, message)
    assert (await workflow.snapshot(USER, SESSION)).details["start_date"] == "2026-11-07"


async def test_yes_alone_saves_no_dates(workflow: WorkflowService) -> None:
    await workflow.record_user_message(USER, SESSION, "yes")
    assert (await workflow.snapshot(USER, SESSION)).details == {}


async def test_no_date_mentioned_means_nothing_can_be_saved(workflow: WorkflowService) -> None:
    await workflow.record_user_message(USER, SESSION, "I need a plumber in Indiranagar")
    result = await workflow.resolve_dates(USER, SESSION, "tomorrow")

    assert result["error"] == "date_not_stated"
    assert "Ask them" in result["message"]
    snapshot = await workflow.snapshot(USER, SESSION)
    assert "start_date" in snapshot.missing_required
    assert snapshot.dates_to_show is None


async def test_words_it_cannot_resolve_ask_the_user(workflow: WorkflowService) -> None:
    await workflow.record_user_message(USER, SESSION, "I need it soon")
    result = await workflow.resolve_dates(USER, SESSION, "soon")
    assert result["error"] == "date_not_understood"
    assert "start_date" in (await workflow.snapshot(USER, SESSION)).missing_required


async def test_a_past_date_is_refused(workflow: WorkflowService) -> None:
    await workflow.record_user_message(USER, SESSION, "on 01/01/2026")
    result = await workflow.resolve_dates(USER, SESSION, "01/01/2026")
    assert result["error"] == "date_not_usable"
    assert "start_date" in (await workflow.snapshot(USER, SESSION)).missing_required


async def test_update_request_cannot_set_dates(workflow: WorkflowService) -> None:
    await asked(workflow, "plumber on 2026-11-07")
    result = await workflow.update_request(USER, SESSION, {"start_date": "2026-11-07"})
    assert "start_date" in result["rejected"]
    assert "start_date" not in result["saved"]


async def test_new_dates_replace_old_ones_and_old_results_stop_counting(
    workflow: WorkflowService,
) -> None:
    await make_ready(workflow, USER, SESSION)
    await workflow.begin_search(USER, SESSION)
    await workflow.finish_search(USER, SESSION, [])

    await workflow.record_user_message(USER, SESSION, "actually Saturday")
    result = await workflow.resolve_dates(USER, SESSION, "Saturday")

    assert result["saved"] is True
    snapshot = await workflow.snapshot(USER, SESSION)
    assert snapshot.state == S.INTAKE
    assert snapshot.details["start_date"] == "2026-11-07"
    assert snapshot.missing_required == []


async def test_a_range_can_narrow_to_one_day(workflow: WorkflowService) -> None:
    """From the user's own chat: "this week", then "make it on friday"."""
    await asked(workflow, "plumber this week")
    await workflow.resolve_dates(USER, SESSION, "this week")
    await workflow.record_user_message(USER, SESSION, "make it on friday")
    await workflow.resolve_dates(USER, SESSION, "friday")

    snapshot = await workflow.snapshot(USER, SESSION)
    assert snapshot.details["start_date"] == "2026-11-06"
    assert snapshot.details["end_date"] == "2026-11-06"


async def test_resolving_the_saved_dates_again_changes_nothing(workflow: WorkflowService) -> None:
    await make_ready(workflow, USER, SESSION)
    result = await workflow.resolve_dates(USER, SESSION, "3 Nov to 5 Nov")
    assert result["already_saved"] is True
    assert (await workflow.snapshot(USER, SESSION)).missing_required == []


async def test_guard_refuses_a_search_with_no_dates(workflow: WorkflowService) -> None:
    await asked(workflow, "plumber in Indiranagar")
    await workflow.update_request(USER, SESSION, DETAILS)

    blocked = await WorkflowSearchGuard(workflow, USER, SESSION).check("search_providers", {})
    assert blocked is not None
    assert blocked[0] == RefusalReason.MISSING_DETAILS
    assert "start_date" in blocked[1]


async def test_saving_dates_is_traced(
    workflow: WorkflowService, db: Database, clock: FakeClock
) -> None:
    traces = TraceService(db, clock)
    recorder = await traces.start_run(USER, SESSION)
    await asked(workflow)
    await workflow.resolve_dates(USER, SESSION, "Saturday", recorder)

    trace = await traces.get_trace(recorder.run_id)
    assert [event.kind for event in trace.events] == ["dates_saved"]
    assert "07/11/2026" in trace.events[0].data["dates"]


async def test_repeating_saved_dates_says_to_search_instead_of_refusing(
    workflow: WorkflowService,
) -> None:
    """Seen live: the model re-sent the date string, and then asked all over again."""
    await asked(workflow)
    await workflow.resolve_dates(USER, SESSION, "Saturday")

    result = await workflow.resolve_dates(USER, SESSION, "This Saturday (07/11/2026)")

    assert result["already_saved"] is True
    assert "Search now" in result["message"]
    assert (await workflow.snapshot(USER, SESSION)).details["start_date"] == "2026-11-07"


async def test_a_date_the_user_never_said_is_still_refused_when_others_are_saved(
    workflow: WorkflowService,
) -> None:
    await asked(workflow)
    await workflow.resolve_dates(USER, SESSION, "Saturday")
    result = await workflow.resolve_dates(USER, SESSION, "14/11/2026")
    assert result["error"] == "date_not_stated"
    assert (await workflow.snapshot(USER, SESSION)).details["start_date"] == "2026-11-07"
