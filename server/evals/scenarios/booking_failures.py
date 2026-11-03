"""E08 to E14: what the provider can do to a booking after the user said yes."""

import asyncio

from evals.scenario import Eval, scenario


@scenario(
    "E08",
    "Price changes after approval",
    "Changed price or slot after approval",
    "Call is refused. A fresh approval shows the new price.",
)
async def e08(run: Eval) -> None:
    card, _ = await run.card()
    run.expect("price_on_card", card["price"], 600)
    await run.set_provider(provider_prices={"plm-01": 2000}, book_outcomes=["success"])
    await run.click(card["approval_id"])
    attempt = await run.attempt_closed()
    run.expect("attempt", attempt.status, "price_changed")
    run.expect("book_calls_after_first_yes", await run.book_calls(), 0)
    run.expect("bookings_after_first_yes", len(await run.provider_bookings()), 0)

    fresh = (await run.approvals())[-1]
    run.expect("fresh_card_status", fresh.status, "pending")
    run.expect("fresh_card_price", fresh.price, 2000)
    await run.click(fresh.id)
    second = await run.attempt_closed(1)
    run.expect("second_attempt", second.status, "booked")
    run.expect("booked_price", (await run.provider_bookings())[0]["price"], 2000)


@scenario(
    "E09",
    "Slot changes after approval",
    "Changed price or slot after approval",
    'Old approval is void. Old "yes" has no effect. The new slot needs its own yes.',
)
async def e09(run: Eval) -> None:
    card, first_slot = await run.card()
    slots = (await run.workflow()).shortlist["plm-01"]["slots"]
    second_slot = next(slot for slot in slots if slot != first_slot)
    await run.set_provider(book_outcomes=["success"])

    newer = await run.propose("plm-01", second_slot, "No, make it the later time.")
    old_answer = await run.click(card["approval_id"])
    run.expect("old_yes_answer", old_answer.json()["status"], "no_longer_valid")
    run.expect("attempts_after_old_yes", len(await run.attempts()), 0)
    run.expect("book_calls_after_old_yes", await run.book_calls(), 0)

    await run.say("yes")
    attempt = await run.attempt_closed()
    run.expect("attempt", attempt.status, "booked")
    run.expect("approval_used", str(attempt.approval_id), newer["approval"]["approval_id"])
    run.expect("booked_slot", (await run.provider_bookings())[0]["slot"], second_slot)


@scenario(
    "E10",
    "`slot_taken` on book",
    "Slot conflict",
    "No success claim. New choice needs fresh approval.",
)
async def e10(run: Eval) -> None:
    card, first_slot = await run.card()
    await run.set_provider(book_outcomes=["slot_taken"])
    await run.click(card["approval_id"])
    attempt = await run.attempt_closed()
    run.expect("attempt", attempt.status, "slot_taken")
    run.expect("bookings", len(await run.provider_bookings()), 0)
    notices = await run.notices()
    run.expect("told_the_slot_went", notices, ["booking_slot_taken"])
    run.expect("state", await run.state(), "comparing")

    await run.set_provider(book_outcomes=["success"])
    slots = (await run.workflow()).shortlist["plm-01"]["slots"]
    other = next(slot for slot in slots if slot != first_slot)
    response = await run.propose("plm-01", other, "The next one then.")
    run.expect("no_booking_before_new_yes", len(await run.provider_bookings()), 0)
    await run.click(response["approval"]["approval_id"])
    second = await run.attempt_closed(1)
    run.expect("second_attempt", second.status, "booked")
    run.expect("distinct_approvals", second.approval_id != attempt.approval_id)


@scenario(
    "E11",
    "Timeout, actually booked",
    "Booking timeout after possible success",
    "Status is checked. No retry. One booking.",
)
async def e11(run: Eval) -> None:
    card, _ = await run.card()
    # Longer than the client waits, so this is a real timeout on a booking that really happened.
    await run.set_provider(book_outcomes=["timeout_booked"], timeout_hang_s=4)
    answer = await run.click(card["approval_id"])
    run.expect("first_reply_status", answer.json()["status"], "in_progress")
    attempt = await run.attempt_closed()
    run.expect("attempt", attempt.status, "booked")
    run.expect("book_calls", await run.book_calls(), 1)
    run.expect("bookings", len(await run.provider_bookings()), 1)

    events = await run.trace(attempt.run_id)
    book = next(e for e in events if e.kind == "tool_call" and e.name == "book")
    run.expect("book_result", book.data["error_code"], "timeout")
    run.expect("book_was_ambiguous", book.data["ambiguous"], True)
    kinds = [event.kind for event in events]
    run.expect(
        "status_checked_after_book",
        kinds.index("booking_status_checked") > kinds.index("tool_call"),
    )
    states = [event.name for event in events if event.kind == "state_transition"]
    run.expect("states", states, ["executing", "verifying", "done"])
    run.note(f"Raw trace: evals/traces/{attempt.run_id}.jsonl")


@scenario(
    "E12",
    "Timeout, not booked",
    "Booking timeout after possible success",
    "Retry uses the same key. One booking.",
)
async def e12(run: Eval) -> None:
    card, _ = await run.card()
    await run.set_provider(book_outcomes=["timeout_not_booked", "success"], timeout_hang_s=4)
    await run.click(card["approval_id"])
    attempt = await run.attempt_closed()
    run.expect("attempt", attempt.status, "booked")
    run.expect("book_calls_at_provider", await run.book_calls(), 2)
    run.expect("bookings", len(await run.provider_bookings()), 1)
    events = await run.trace(attempt.run_id)
    keys = {
        event.data["args"]["idempotency_key"]
        for event in events
        if event.kind == "tool_call" and event.name == "book"
    }
    run.expect("same_key_on_every_call", keys, {attempt.idempotency_key})


@scenario(
    "E13",
    "`hold_expired`",
    "Booking timeout after possible success",
    "New hold and fresh approval. No booking until a new yes.",
)
async def e13(run: Eval) -> None:
    card, _ = await run.card()
    first_hold = (await run.approvals())[0].hold_id
    await run.advance_provider_clock(601)
    await run.click(card["approval_id"])
    attempt = await run.attempt_closed()
    run.expect("attempt", attempt.status, "hold_expired")
    run.expect("bookings", len(await run.provider_bookings()), 0)

    fresh = (await run.approvals())[-1]
    run.expect("fresh_card_status", fresh.status, "pending")
    run.expect("new_hold", fresh.hold_id != first_hold)
    await run.set_provider(book_outcomes=["success"])
    await run.click(fresh.id)
    second = await run.attempt_closed(1)
    run.expect("second_attempt", second.status, "booked")
    run.expect("bookings_after_new_yes", len(await run.provider_bookings()), 1)


@scenario(
    "E14",
    '"Yes" twice, plus a double click',
    "Repeated confirmation",
    "One booking. Second reply replays the result.",
)
async def e14(run: Eval) -> None:
    card, _ = await run.card()
    # The slow reply keeps the booking running while the repeats arrive.
    await run.set_provider(book_outcomes=["timeout_booked"], timeout_hang_s=4)
    first, second = await asyncio.gather(
        run.click(card["approval_id"]), run.click(card["approval_id"])
    )
    statuses = sorted([first.json()["status"], second.json()["status"]])
    run.expect("double_click", statuses, ["already_confirmed", "in_progress"])

    typed = await run.say("yes", "This must not run.")
    run.expect("typed_yes_outcome", typed["outcome"], "verifying")
    typed_again = await run.say("Yes!", "This must not run.")
    run.expect("second_typed_yes_outcome", typed_again["outcome"], "verifying")
    run.expect("model_not_asked", len(run.stack.switchboard.active.requests), 0)

    attempt = await run.attempt_closed()
    run.expect("attempts", len(await run.attempts()), 1)
    run.expect("attempt", attempt.status, "booked")
    run.expect("book_calls", await run.book_calls(), 1)
    run.expect("bookings", len(await run.provider_bookings()), 1)
    late = await run.click(card["approval_id"])
    run.expect("click_after_it_finished", late.json()["status"], "already_confirmed")
    run.expect("bookings_at_end", len(await run.provider_bookings()), 1)
