"""E17 to E19: cancel and reschedule, through the same approval path."""

from datetime import datetime

from evals.scenario import Eval, scenario


@scenario(
    "E17",
    "Cancel gets a 503",
    "Tool failures",
    "Retry with same key succeeds. `list_bookings` confirms it.",
)
async def e17(run: Eval) -> None:
    booking = await run.booked()
    response = await run.propose_change(
        "propose_cancel", "Cancel my plumber booking.", booking_id=booking["booking_id"]
    )
    await run.click(response["approval"]["approval_id"])
    attempt = await run.attempt_closed(1)
    run.expect("attempt", attempt.status, "cancelled")
    events = await run.trace(attempt.run_id)
    cancel = next(e for e in events if e.kind == "tool_call" and e.name == "cancel")
    run.expect("gateway_attempts", cancel.data["attempts"], 2)
    run.expect("provider_status", (await run.provider_bookings())[0]["status"], "cancelled")
    run.expect("told_verified", (await run.notices()).count("booking_cancelled"), 1)


@scenario(
    "E18",
    "Cancel retries run out (injected)",
    "Tool failures",
    "Clear failure. No cancel claim. The booking stays.",
)
async def e18(run: Eval) -> None:
    booking = await run.booked()
    await run.set_provider(cancel_failures=99)
    response = await run.propose_change(
        "propose_cancel", "Cancel my plumber booking.", booking_id=booking["booking_id"]
    )
    await run.click(response["approval"]["approval_id"])
    attempt = await run.attempt_closed(1)
    run.expect("attempt", attempt.status, "failed")
    run.expect("provider_status", (await run.provider_bookings())[0]["status"], "confirmed")
    notices = await run.notices()
    run.expect("no_cancel_claim", "booking_cancelled" in notices, False)
    run.expect("told_it_failed", notices[-1], "change_failed")


@scenario(
    "E19",
    "Reschedule gets `slot_conflict`",
    "Slot conflict",
    "Old booking stays. Old yes is spent. New slot needs fresh approval.",
)
async def e19(run: Eval) -> None:
    booking = await run.booked()
    slots = (await run.workflow()).shortlist["plm-01"]["slots"]
    others = [slot for slot in slots if slot != booking["slot"]]
    conflict, free = others[0], others[-1]
    await run.set_provider(slot_conflicts=[conflict])

    response = await _move(run, booking["booking_id"], conflict)
    await run.click(response["approval"]["approval_id"])
    attempt = await run.attempt_closed(1)
    run.expect("attempt", attempt.status, "slot_conflict")
    run.expect("booking_stays", (await run.provider_bookings())[0]["slot"], booking["slot"])
    run.expect("told_of_conflict", (await run.notices())[-1], "reschedule_conflict")
    replay = await run.click(response["approval"]["approval_id"])
    run.expect("old_yes_is_spent", replay.json()["status"], "already_confirmed")
    run.expect("attempts_after_old_yes", len(await run.attempts()), 2)

    fresh = await _move(run, booking["booking_id"], free)
    run.expect("new_card", fresh["approval"]["approval_id"] != response["approval"]["approval_id"])
    run.expect(
        "not_moved_before_new_yes", (await run.provider_bookings())[0]["slot"], booking["slot"]
    )
    await run.click(fresh["approval"]["approval_id"])
    second = await run.attempt_closed(2)
    run.expect("second_attempt", second.status, "rescheduled")
    run.expect("moved_to", (await run.provider_bookings())[0]["slot"], free)


async def _move(run: Eval, booking_id: str, slot: str) -> dict:
    moment = datetime.fromisoformat(slot)
    day, at = f"{moment:%-d %b}", f"{moment:%H:%M}"
    return await run.propose_change(
        "propose_reschedule",
        f"Move my plumber booking to {day} at {at}",
        booking_id=booking_id,
        day=day,
        time=at,
    )
