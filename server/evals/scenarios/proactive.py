"""E24 and E25: the provider cancels, and nobody asked."""

import asyncio

from app.domains.agent.scripted import Pause
from evals.scenario import Eval, scenario

BUDGET = 1000


@scenario(
    "E24",
    "Provider cancels with no user prompt",
    "Proactive cancellation",
    "Detected and notified. Replacement fits preferences. Fresh approval is required. "
    "A user turn in progress still completes.",
)
async def e24(run: Eval) -> None:
    await run.set_provider(book_outcomes=["success"])
    shortlist = await run.search(extra="under 1000", max_budget=BUDGET)
    response = await run.propose("plm-01", shortlist["plm-01"]["slots"][0])
    await run.click(response["approval"]["approval_id"])
    attempt = await run.attempt_closed()
    run.expect("first_attempt", attempt.status, "booked")
    approvals_before = len(await run.approvals())

    # A real turn holds the lock while the provider cancels and several polls go by.
    turn = asyncio.create_task(run.say("One question while I wait", Pause(5, "Sure.")))
    await asyncio.sleep(1)
    await run.provider_cancels(str(attempt.booking_id))
    await asyncio.sleep(3)
    # The news is only a row, so it needs no lock. Reopening the errand waits for the turn.
    during = await run.notices()
    run.expect("user_told_during_the_turn", "provider_cancelled" in during, True)
    run.expect("errand_left_alone_during_the_turn", "replacement_options" in during, False)
    reply = await turn
    run.expect("turn_outcome", reply["outcome"], "needs_input")
    run.expect("turn_reason", reply["reason"], None)

    async def offered() -> bool:
        return "replacement_options" in await run.notices()

    await run.wait_until("the replacement notice", offered)
    notices = await run.notices()
    run.expect("cancel_notices", notices.count("provider_cancelled"), 1)
    run.expect("replacement_notices", notices.count("replacement_options"), 1)
    row = (await run.disruptions())[0]
    run.expect("decision_reason", row.reason, "replacements_offered")

    options = (await run.workflow()).shortlist
    prices = [item["price"] for item in options.values()]
    run.expect("cancelled_provider_not_offered", "plm-01" in options, False)
    run.expect_that("replacement_prices", sorted(prices), bool(prices) and max(prices) <= BUDGET)
    run.expect("no_new_card_by_itself", len(await run.approvals()), approvals_before)
    run.expect("no_new_booking_by_itself", len(await run.attempts()), 1)

    await run.set_provider(book_outcomes=["success"])
    provider_id = next(iter(options))
    fresh = await run.propose(provider_id, options[provider_id]["slots"][0], "Book that one.")
    run.expect("rebook_needs_a_card", fresh["outcome"], "awaiting_approval")
    await run.click(fresh["approval"]["approval_id"])
    second = await run.attempt_closed(1)
    run.expect("rebooked", second.status, "booked")
    run.expect("rebooked_with", second.provider_id, provider_id)


@scenario(
    "E25",
    "Duplicate event, or past booking",
    "Proactive cancellation",
    "One notice for a duplicate. None for a past booking.",
)
async def e25(run: Eval) -> None:
    first = await run.booked()
    await run.provider_cancels(first["booking_id"])

    async def repeated() -> bool:
        rows = await run.disruptions()
        return bool(rows) and rows[0].repeats_seen >= 2

    await run.wait_until("two more polls to see the same cancel", repeated)
    run.expect("notices_for_duplicate", (await run.notices()).count("provider_cancelled"), 1)
    before = await run.notices()

    run.session = "second-chat-e25"
    second = await run.booked("plm-04")
    await run.advance_provider_clock(60 * 86400)
    await run.provider_cancels(second["booking_id"])

    async def recorded() -> list:
        return [row for row in await run.disruptions() if row.reason == "past_booking"]

    await run.wait_until("the past booking decision", recorded)
    after = await run.notices()
    new = after[len(before) :]
    run.expect("notices_for_past_booking", [kind for kind in new if kind != "booking_verified"], [])
