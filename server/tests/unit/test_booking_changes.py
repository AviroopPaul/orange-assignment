"""Cancel and reschedule, on the same approval path, against the real mock provider and verifier."""

from datetime import date, timedelta
from typing import Any

import pytest
from sqlalchemy import update

from app.domains.bookings.matching import Decision, claimed_actions, read_decision
from app.domains.bookings.models import ApprovalStatus, AttemptStatus, BookingAttempt
from app.domains.bookings.schemas import ApprovalCard, DecisionStatus
from app.domains.bookings.service import (
    CHANGE_CARD_LIFE,
    CHANGE_NO_LONGER_VALID_REPLY,
    CHANGE_REJECTED_REPLY,
    NOT_HELD_NOTE,
)
from app.domains.bookings.verification import change_token_for, token_for
from app.domains.gateway.schemas import Caller, RefusalReason, ToolStatus
from app.domains.gateway.service import CallContext, ToolBudget
from app.domains.workflow.models import WorkflowState as S
from mock_provider import catalog
from mock_provider.catalog import CATALOG
from tests.unit import test_bookings
from tests.unit.test_bookings import USER, Stack

NEXT_CHAT = "s2"
stack = test_bookings.stack


async def booked(stack: Stack) -> dict[str, Any]:
    await stack.book(book_outcomes=["success"])
    return (await stack.provider_bookings())[0]


async def cancel_card(stack: Stack, booking_id: str, chat: str = NEXT_CHAT) -> ApprovalCard:
    await stack.core.workflows.record_user_message(USER, chat, "cancel my booking")
    recorder = await stack.recorder(chat)
    result = await stack.core.approvals.propose_cancel(chat, booking_id, stack.context(recorder))
    assert result.get("card_shown_to_user"), result
    view = await stack.core.approvals.view(USER, chat)
    assert view.card is not None
    return view.card


def free_slot(provider_id: str, not_on: str) -> tuple[date, str]:
    """A day and hour this provider works, away from the booking and the default conflict hour."""
    day = date(2026, 11, 9)
    for offset in range(14):
        current = day + timedelta(days=offset)
        for slot in catalog.slots_on(CATALOG[provider_id], current):
            parsed = catalog.parse_slot(slot)
            assert parsed is not None
            if parsed.hour != catalog.DEFAULT_CONFLICT_HOUR and slot != not_on:
                return current, f"{parsed.hour:02d}:00"
    raise AssertionError("no slot found")


async def propose_move(
    stack: Stack, booking: dict[str, Any], day: date, at: str, chat: str = NEXT_CHAT
) -> dict[str, Any]:
    phrase = f"{day.day} {day:%b}"
    await stack.core.workflows.record_user_message(USER, chat, f"move it to {phrase} at {at}")
    recorder = await stack.recorder(chat)
    return await stack.core.approvals.propose_reschedule(
        chat, booking["booking_id"], phrase, at, stack.context(recorder)
    )


async def move_card(stack: Stack, booking: dict[str, Any]) -> ApprovalCard:
    day, at = free_slot(booking["provider_id"], booking["slot"])
    result = await propose_move(stack, booking, day, at)
    assert result.get("card_shown_to_user"), result
    view = await stack.core.approvals.view(USER, NEXT_CHAT)
    assert view.card is not None
    return view.card


def saga_context(token: str | None = None, user: str = USER) -> CallContext:
    return CallContext(user, Caller.SAGA, ToolBudget(5), approval_token=token)


# Cancel


async def test_a_cancel_card_shows_the_booking_and_changes_nothing(stack: Stack) -> None:
    booking = await booked(stack)
    card = await cancel_card(stack, booking["booking_id"])

    assert card.action == "cancel"
    assert card.provider_name == booking["provider_name"]
    assert card.price == booking["price"]
    assert card.slot == booking["slot"]
    assert card.old_slot_label is None
    assert await stack.state(NEXT_CHAT) == S.AWAITING_APPROVAL
    assert "cancel" not in stack.transport.calls
    assert (await stack.provider_bookings())[0]["status"] == "confirmed"


async def test_yes_cancels_through_the_providers_503_and_verifies(stack: Stack) -> None:
    booking = await booked(stack)
    card = await cancel_card(stack, booking["booking_id"])
    decision = await stack.yes(card)
    assert decision.status == DecisionStatus.IN_PROGRESS
    assert "not happened yet" in decision.reply

    await stack.run_jobs()

    assert (await stack.provider_bookings())[0]["status"] == "cancelled"
    # The contract's first call fails with a 503, and the gateway retries with the same key.
    assert stack.transport.calls.count("cancel") == 2
    attempt = next(row for row in await stack.attempts() if row.action == "cancel")
    assert attempt.status == AttemptStatus.CANCELLED.value
    assert attempt.idempotency_key == f"cancel:{card.approval_id}"
    assert await stack.state(NEXT_CHAT) == S.INTAKE  # Done, so the next message starts fresh.
    notes = await stack.notes(NEXT_CHAT)
    assert [note.kind for note in notes] == ["booking_cancelled"]
    assert notes[0].text.startswith("Cancelled and verified with the provider")
    kinds = await stack.trace_kinds(attempt.run_id)
    assert kinds.index("booking_status_checked") < kinds.index("booking_cancelled")


async def test_success_needs_the_status_check_not_the_calls_answer(stack: Stack) -> None:
    """The call answered, but the list could not be read. That is not success."""
    booking = await booked(stack)
    card = await cancel_card(stack, booking["booking_id"])
    await stack.yes(card)
    stack.transport.down.add("list_bookings")

    await stack.run_jobs()

    notes = await stack.notes(NEXT_CHAT)
    assert [note.kind for note in notes] == ["booking_not_confirmed"]
    assert "Cancelling" in notes[0].text and "not confirmed yet" in notes[0].text
    attempt = next(row for row in await stack.attempts() if row.action == "cancel")
    assert attempt.status == AttemptStatus.OPEN.value


async def test_cancel_retries_running_out_is_a_clear_failure(stack: Stack) -> None:
    booking = await booked(stack)
    card = await cancel_card(stack, booking["booking_id"])
    await stack.yes(card)
    stack.transport.always_503.add("cancel")

    await stack.run_jobs()

    assert (await stack.provider_bookings())[0]["status"] == "confirmed"
    attempt = next(row for row in await stack.attempts() if row.action == "cancel")
    assert attempt.status == AttemptStatus.FAILED.value
    note = (await stack.notes(NEXT_CHAT))[-1]
    assert note.kind == "change_failed"
    assert "could not cancel" in note.text
    assert "still shows it as active" in note.text
    assert "Cancelled and verified" not in note.text
    assert await stack.state(NEXT_CHAT) == S.COMPARING


async def test_a_job_that_died_after_the_cancel_finishes_with_no_second_cancel(
    stack: Stack,
) -> None:
    booking = await booked(stack)
    card = await cancel_card(stack, booking["booking_id"])
    await stack.yes(card)
    attempt = next(row for row in await stack.attempts() if row.action == "cancel")
    # The worker sent the cancel, then died before it could write anything but the count.
    for _ in range(2):
        await stack.provider.post(
            "/tools/cancel",
            json={"booking_id": booking["booking_id"], "idempotency_key": attempt.idempotency_key},
            headers={"X-User-Id": USER},
        )
    async with stack.db.session_scope() as session:
        await session.execute(
            update(BookingAttempt).where(BookingAttempt.id == attempt.id).values(book_calls=1)
        )
    before = stack.transport.calls.count("cancel")

    await stack.core.saga.run(attempt.id)

    assert stack.transport.calls.count("cancel") == before
    closed = next(row for row in await stack.attempts() if row.action == "cancel")
    assert closed.status == AttemptStatus.CANCELLED.value


async def test_the_job_running_twice_cancels_once(stack: Stack) -> None:
    booking = await booked(stack)
    card = await cancel_card(stack, booking["booking_id"])
    await stack.yes(card)
    await stack.run_jobs()
    calls = stack.transport.calls.count("cancel")
    attempt = next(row for row in await stack.attempts() if row.action == "cancel")

    await stack.core.saga.run(attempt.id)

    assert stack.transport.calls.count("cancel") == calls
    assert len(await stack.notes(NEXT_CHAT)) == 1


async def test_repeated_confirmation_makes_one_attempt(stack: Stack) -> None:
    booking = await booked(stack)
    card = await cancel_card(stack, booking["booking_id"])
    first = await stack.yes(card)
    second = await stack.yes(card)

    assert first.status == DecisionStatus.IN_PROGRESS
    assert second.status == DecisionStatus.ALREADY_CONFIRMED
    assert "only happen once" in second.reply
    assert len([row for row in await stack.attempts() if row.action == "cancel"]) == 1


async def test_saying_no_keeps_the_booking(stack: Stack) -> None:
    booking = await booked(stack)
    card = await cancel_card(stack, booking["booking_id"])

    decision = await stack.core.approvals.reject_click(card.approval_id, USER)

    assert decision.status == DecisionStatus.REJECTED
    assert decision.reply == CHANGE_REJECTED_REPLY
    await stack.run_jobs()
    assert "cancel" not in stack.transport.calls
    assert (await stack.provider_bookings())[0]["status"] == "confirmed"


async def test_a_card_that_ran_out_changes_nothing(stack: Stack) -> None:
    booking = await booked(stack)
    card = await cancel_card(stack, booking["booking_id"])
    stack.clock.advance(CHANGE_CARD_LIFE.total_seconds() + 1)

    decision = await stack.yes(card)

    assert decision.status == DecisionStatus.NO_LONGER_VALID
    assert "ran out" in decision.reply
    assert (await stack.approval(card.approval_id)).status == ApprovalStatus.EXPIRED.value
    await stack.run_jobs()
    assert "cancel" not in stack.transport.calls


async def test_a_change_of_details_voids_the_card(stack: Stack) -> None:
    booking = await booked(stack)
    card = await cancel_card(stack, booking["booking_id"])
    await stack.core.workflows.record_user_message(USER, NEXT_CHAT, "a plumber in Indiranagar")
    await stack.core.workflows.update_request(USER, NEXT_CHAT, {"service": "plumber"})

    decision = await stack.yes(card)

    assert decision.status == DecisionStatus.NO_LONGER_VALID
    assert decision.reply == CHANGE_NO_LONGER_VALID_REPLY
    assert "cancel" not in stack.transport.calls


async def test_a_second_chat_cannot_run_a_second_change_at_once(stack: Stack) -> None:
    booking = await booked(stack)
    first = await cancel_card(stack, booking["booking_id"], NEXT_CHAT)
    second = await cancel_card(stack, booking["booking_id"], "s3")
    assert (await stack.yes(first)).status == DecisionStatus.IN_PROGRESS

    decision = await stack.yes(second)

    assert decision.status == DecisionStatus.NO_LONGER_VALID
    assert "already in progress" in decision.reply
    assert (await stack.approval(second.approval_id)).status == ApprovalStatus.VOIDED.value


async def test_a_booking_the_provider_dropped_is_not_reported_as_cancelled_by_us(
    stack: Stack,
) -> None:
    booking = await booked(stack)
    card = await cancel_card(stack, booking["booking_id"])
    await stack.yes(card)
    await stack.provider.post("/admin/provider-cancel", json={"booking_id": booking["booking_id"]})

    await stack.run_jobs()

    assert "cancel" not in stack.transport.calls
    note = (await stack.notes(NEXT_CHAT))[-1]
    assert note.kind == "change_failed"
    assert "cancelled_by_provider" in note.text


async def test_a_booking_that_moved_after_the_yes_is_refused_and_nothing_is_sent(
    stack: Stack,
) -> None:
    booking = await booked(stack)
    card = await cancel_card(stack, booking["booking_id"])
    await stack.yes(card)
    day, at = free_slot(booking["provider_id"], booking["slot"])
    moved = f"{day.isoformat()}T{at}:00+05:30"
    response = await stack.provider.post(
        "/tools/reschedule",
        json={"booking_id": booking["booking_id"], "slot": moved, "idempotency_key": "elsewhere"},
        headers={"X-User-Id": USER},
    )
    assert response.status_code == 200, response.text

    await stack.run_jobs()

    assert "cancel" not in stack.transport.calls
    attempt = next(row for row in await stack.attempts() if row.action == "cancel")
    assert attempt.status == AttemptStatus.FAILED.value
    assert attempt.book_calls == 0
    assert "approval_mismatch" in await stack.trace_kinds(attempt.run_id)
    assert (await stack.provider_bookings())[0]["status"] == "confirmed"


# What may be proposed


@pytest.mark.parametrize("booking_id", ["bkg-nobody", "bkg-demo-ac-servicing"])
async def test_only_the_users_own_bookings_can_be_proposed(stack: Stack, booking_id: str) -> None:
    await stack.core.workflows.record_user_message(USER, NEXT_CHAT, "cancel it")
    result = await stack.core.approvals.propose_cancel(NEXT_CHAT, booking_id, stack.context())
    assert result["error"] == "booking_not_found"
    assert (await stack.core.approvals.view(USER, NEXT_CHAT)).card is None


async def test_a_cancelled_booking_cannot_be_proposed_again(stack: Stack) -> None:
    booking = await booked(stack)
    await stack.yes(await cancel_card(stack, booking["booking_id"]))
    await stack.run_jobs()

    await stack.core.workflows.record_user_message(USER, "s3", "cancel it again")
    result = await stack.core.approvals.propose_cancel("s3", booking["booking_id"], stack.context())
    assert result["error"] == "booking_not_active"


async def test_proposing_twice_shows_the_same_card(stack: Stack) -> None:
    booking = await booked(stack)
    card = await cancel_card(stack, booking["booking_id"])
    again = await cancel_card(stack, booking["booking_id"])
    assert again.approval_id == card.approval_id
    assert len([row for row in await stack.approvals() if row.action == "cancel"]) == 1


async def test_a_change_cannot_be_proposed_while_one_is_running(stack: Stack) -> None:
    booking = await booked(stack)
    await stack.yes(await cancel_card(stack, booking["booking_id"]))
    result = await stack.core.approvals.propose_cancel("s3", booking["booking_id"], stack.context())
    assert result["error"] == "booking_in_progress"


async def test_list_failure_shows_no_card(stack: Stack) -> None:
    booking = await booked(stack)
    await stack.core.workflows.record_user_message(USER, NEXT_CHAT, "cancel it")
    stack.transport.down.add("list_bookings")
    result = await stack.core.approvals.propose_cancel(
        NEXT_CHAT, booking["booking_id"], stack.context()
    )
    assert "Nothing changed" in result["message"]
    assert (await stack.core.approvals.view(USER, NEXT_CHAT)).card is None


# Reschedule


async def test_a_move_card_shows_both_times_and_moves_nothing(stack: Stack) -> None:
    booking = await booked(stack)
    card = await move_card(stack, booking)

    assert card.action == "reschedule"
    assert card.old_slot_label is not None
    assert card.slot != booking["slot"]
    assert card.notes == [NOT_HELD_NOTE]
    assert "reschedule" not in stack.transport.calls
    assert (await stack.provider_bookings())[0]["slot"] == booking["slot"]


async def test_yes_moves_it_and_the_list_verifies(stack: Stack) -> None:
    booking = await booked(stack)
    card = await move_card(stack, booking)
    await stack.yes(card)
    await stack.run_jobs()

    now = (await stack.provider_bookings())[0]
    assert now["slot"] == card.slot
    assert now["status"] == "confirmed"
    attempt = next(row for row in await stack.attempts() if row.action == "reschedule")
    assert attempt.status == AttemptStatus.RESCHEDULED.value
    assert attempt.idempotency_key == f"reschedule:{card.approval_id}"
    note = (await stack.notes(NEXT_CHAT))[-1]
    assert note.kind == "booking_rescheduled"
    assert card.slot_label in note.text
    assert card.old_slot_label in note.text


async def test_slot_conflict_keeps_the_old_booking_and_a_new_time_needs_a_new_yes(
    stack: Stack,
) -> None:
    booking = await booked(stack)
    card = await move_card(stack, booking)
    await stack.scenario(slot_conflicts=[card.slot])
    await stack.yes(card)
    await stack.run_jobs()

    assert stack.transport.calls.count("reschedule") == 1
    assert (await stack.provider_bookings())[0]["slot"] == booking["slot"]
    attempt = next(row for row in await stack.attempts() if row.action == "reschedule")
    assert attempt.status == AttemptStatus.SLOT_CONFLICT.value
    assert (await stack.approval(card.approval_id)).status == ApprovalStatus.CONSUMED.value
    note = (await stack.notes(NEXT_CHAT))[-1]
    assert note.kind == "reschedule_conflict"
    assert "did not move" in note.text
    assert await stack.state(NEXT_CHAT) == S.COMPARING
    # The old yes is spent. Nothing is open to answer until a new card is shown.
    assert (await stack.core.approvals.view(USER, NEXT_CHAT)).card is None


async def test_a_time_the_provider_does_not_work_is_a_clear_failure(stack: Stack) -> None:
    booking = await booked(stack)
    day = date(2026, 11, 9)
    while catalog.slots_on(CATALOG[booking["provider_id"]], day):
        day += timedelta(days=1)  # Its day off.
    result = await propose_move(stack, booking, day, "10:00")
    assert result.get("card_shown_to_user"), result
    view = await stack.core.approvals.view(USER, NEXT_CHAT)
    assert view.card is not None
    await stack.yes(view.card)
    await stack.run_jobs()

    note = (await stack.notes(NEXT_CHAT))[-1]
    assert note.kind == "change_failed"
    assert "does not work at" in note.text
    assert (await stack.provider_bookings())[0]["slot"] == booking["slot"]


async def test_the_model_cannot_supply_a_day_the_user_never_said(stack: Stack) -> None:
    booking = await booked(stack)
    await stack.core.workflows.record_user_message(USER, NEXT_CHAT, "move my booking")
    result = await stack.core.approvals.propose_reschedule(
        NEXT_CHAT, booking["booking_id"], "14 Nov", "10:00", stack.context()
    )
    assert result["error"] == "date_not_stated"


async def test_the_model_cannot_pick_a_time(stack: Stack) -> None:
    """From the user's chat: they gave only a day, and the model chose 10:00."""
    booking = await booked(stack)
    await stack.core.workflows.record_user_message(USER, NEXT_CHAT, "move my AC to next week")
    await stack.core.workflows.record_user_message(USER, NEXT_CHAT, "around wednesday")
    result = await stack.core.approvals.propose_reschedule(
        NEXT_CHAT, booking["booking_id"], "wednesday", "10:00", stack.context()
    )
    assert result["error"] == "time_not_stated"
    assert (await stack.core.approvals.view(USER, NEXT_CHAT)).card is None


async def test_words_over_two_messages_reach_next_week(stack: Stack) -> None:
    """From the user's chat, with the words joined the way the prompt asks."""
    booking = await booked(stack)
    workflows = stack.core.workflows
    await workflows.record_user_message(USER, NEXT_CHAT, "move my AC to next week")
    await workflows.record_user_message(USER, NEXT_CHAT, "around wednesday, at 10am")
    # The conftest clock's today is Monday 2 November 2026.
    day = date(2026, 11, 11)
    assert await workflows.read_day_and_time(USER, NEXT_CHAT, "wednesday next week", "10:00") == day
    assert booking["status"] == "confirmed"


async def test_a_range_needs_one_day(stack: Stack) -> None:
    booking = await booked(stack)
    await stack.core.workflows.record_user_message(USER, NEXT_CHAT, "move it to next week")
    result = await stack.core.approvals.propose_reschedule(
        NEXT_CHAT, booking["booking_id"], "next week", "10:00", stack.context()
    )
    assert result["error"] == "need_one_day"


async def test_a_time_that_has_passed_is_refused(stack: Stack) -> None:
    booking = await booked(stack)
    await stack.core.workflows.record_user_message(USER, NEXT_CHAT, "move it to today at 06:00")
    result = await stack.core.approvals.propose_reschedule(
        NEXT_CHAT, booking["booking_id"], "today", "06:00", stack.context()
    )
    assert result["error"] == "time_has_passed"


async def test_the_same_time_is_not_a_move(stack: Stack) -> None:
    booking = await booked(stack)
    parsed = catalog.parse_slot(booking["slot"])
    assert parsed is not None
    result = await propose_move(stack, booking, parsed.date(), f"{parsed.hour:02d}:00")
    assert result["error"] == "same_slot"


async def test_moving_a_booking_leaves_an_errand_in_progress_alone(stack: Stack) -> None:
    booking = await booked(stack)
    workflows = stack.core.workflows
    await workflows.record_user_message(USER, NEXT_CHAT, "a plumber in HSR on 20 Nov")
    await workflows.update_request(USER, NEXT_CHAT, {"service": "plumber", "area": "HSR"})
    await workflows.resolve_dates(USER, NEXT_CHAT, "20 Nov")

    card = await move_card(stack, booking)
    await stack.yes(card)
    await stack.run_jobs()

    snapshot = await workflows.snapshot(USER, NEXT_CHAT)
    assert snapshot.state == S.COMPARING
    assert snapshot.details["service"] == "plumber"
    assert snapshot.details["start_date"] == "2026-11-20"


# The gateway and the verifier


async def test_cancel_and_reschedule_are_refused_with_no_token(stack: Stack) -> None:
    booking = await booked(stack)
    for tool, args in (
        ("cancel", {"booking_id": booking["booking_id"], "idempotency_key": "cancel:x"}),
        (
            "reschedule",
            {
                "booking_id": booking["booking_id"],
                "slot": booking["slot"],
                "idempotency_key": "reschedule:x",
            },
        ),
    ):
        result = await stack.core.gateway.call(tool, args, saga_context())
        assert result.status == ToolStatus.REFUSED
        assert result.error_code == RefusalReason.APPROVAL_REQUIRED.value
    assert "cancel" not in stack.transport.calls
    assert "reschedule" not in stack.transport.calls


async def _running_cancel(stack: Stack) -> tuple[dict[str, Any], Any, BookingAttempt]:
    booking = await booked(stack)
    card = await cancel_card(stack, booking["booking_id"])
    await stack.yes(card)
    approval = await stack.approval(card.approval_id)
    attempt = next(row for row in await stack.attempts() if row.action == "cancel")
    return booking, approval, attempt


async def test_a_real_token_passes_only_for_its_exact_call(stack: Stack) -> None:
    booking, approval, attempt = await _running_cancel(stack)
    token = change_token_for(approval, booking)
    verifier = stack.core.gateway._approvals
    good = {"booking_id": booking["booking_id"], "idempotency_key": attempt.idempotency_key}

    assert await verifier.is_valid(token, "cancel", good, USER) is True
    assert await verifier.is_valid(token, "cancel", good, "someone-else") is False
    assert await verifier.is_valid(token, "reschedule", good, USER) is False
    assert await verifier.is_valid(token, "book", good, USER) is False
    assert (
        await verifier.is_valid(token, "cancel", {**good, "booking_id": "bkg-other"}, USER) is False
    )
    assert (
        await verifier.is_valid(token, "cancel", {**good, "idempotency_key": "cancel:x"}, USER)
        is False
    )


@pytest.mark.parametrize(
    "change", [{"price": 1}, {"slot": "2026-11-20T10:00:00+05:30"}, {"provider_id": "plm-99"}]
)
async def test_a_token_from_a_booking_that_differs_is_refused(
    stack: Stack, change: dict[str, Any]
) -> None:
    booking, approval, attempt = await _running_cancel(stack)
    token = change_token_for(approval, {**booking, **change})
    args = {"booking_id": booking["booking_id"], "idempotency_key": attempt.idempotency_key}
    assert await stack.core.gateway._approvals.is_valid(token, "cancel", args, USER) is False


async def test_a_token_from_an_old_booking_approval_cannot_cancel(stack: Stack) -> None:
    card = await stack.book(book_outcomes=["success"])
    booking = (await stack.provider_bookings())[0]
    approval = await stack.approval(card.approval_id)
    token = token_for(approval, approval.list_price)
    args = {"booking_id": booking["booking_id"], "idempotency_key": f"cancel:{approval.id}"}
    result = await stack.core.gateway.call("cancel", args, saga_context(token))
    assert result.status == ToolStatus.REFUSED
    assert "cancel" not in stack.transport.calls


async def test_a_consumed_approval_is_needed(stack: Stack) -> None:
    booking = await booked(stack)
    card = await cancel_card(stack, booking["booking_id"])  # Not answered.
    approval = await stack.approval(card.approval_id)
    token = change_token_for(approval, booking)
    args = {"booking_id": booking["booking_id"], "idempotency_key": f"cancel:{approval.id}"}
    result = await stack.core.gateway.call("cancel", args, saga_context(token))
    assert result.status == ToolStatus.REFUSED


# Reading the user's words


@pytest.mark.parametrize(
    ("message", "action", "expected"),
    [
        ("cancel it", "cancel", Decision.YES),
        ("yes, cancel it", "cancel", Decision.YES),
        ("yes", "cancel", Decision.YES),
        ("confirm", "reschedule", Decision.YES),
        ("move it", "reschedule", Decision.YES),
        ("book it", "book", Decision.YES),
        ("no", "cancel", Decision.NO),
        ("don't cancel", "cancel", Decision.NO),
        ("keep it", "cancel", Decision.NO),
        ("keep it", "reschedule", Decision.NO),
        ("dont book", "book", Decision.NO),
        # A word for another action is not an answer to this card.
        ("book it", "cancel", None),
        ("book it", "reschedule", None),
        ("cancel it", "book", None),
        ("cancel it", "reschedule", None),
        ("move it", "cancel", None),
        ("keep it", "book", None),
        # On any card, a bare "cancel" could mean "close this".
        ("cancel", "cancel", None),
        ("cancel", "book", None),
        ("yes, but next week", "reschedule", None),
    ],
)
def test_words_are_read_against_the_cards_action(
    message: str, action: str, expected: Decision | None
) -> None:
    assert read_decision(message, action) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Your booking has been cancelled.", {"cancel"}),
        ("I've cancelled it for you.", {"cancel"}),
        ("It is now rescheduled to Friday.", {"reschedule"}),
        ("Your booking has been moved to Friday.", {"reschedule"}),
        ("I have moved it.", {"reschedule"}),
        ("You are all booked.", {"book"}),
        ("It is not cancelled yet.", set()),
        ("Nothing has been moved.", set()),
        ("Once you confirm, it will be cancelled.", set()),
        ("Do you want this moved to Friday?", set()),
    ],
)
def test_a_claim_is_read_as_the_action_it_names(text: str, expected: set[str]) -> None:
    assert claimed_actions(text) == expected
