"""E01 to E03, and E20: the ordinary path."""

from datetime import date, timedelta

from app.domains.agent.scripted import ToolCall
from evals.scenario import Eval, scenario
from mock_provider import catalog

AREA = "Koramangala"


@scenario(
    "E01",
    "Full booking from a partial request",
    "Core flow",
    "Only missing fields are requested. Search is refused until they are known. "
    "Booking is verified.",
)
async def e01(run: Eval) -> None:
    first = await run.say(
        "I need a plumber.",
        ToolCall("update_request", {"service": "plumber"}),
        ToolCall("search_providers"),
        "Which area, and when?",
    )
    run.expect("first_outcome", first["outcome"], "needs_input")
    refused = [event.name for event in await run.all_events("tool_refused")]
    run.expect("search_refused_while_details_missing", refused, ["search_providers"])

    second = await run.say(
        "In Indiranagar.", ToolCall("update_request", {"area": "Indiranagar"}), "When?"
    )
    run.expect("second_outcome", second["outcome"], "needs_input")
    run.expect("service_kept", (await run.details())["service"], "plumber")

    await run.say(
        "Next week",
        ToolCall("resolve_dates", {"phrase": "next week"}),
        ToolCall("search_providers"),
        "Here are some options.",
    )
    await run.set_provider(book_outcomes=["success"])
    shortlist = (await run.workflow()).shortlist
    response = await run.propose("plm-01", shortlist["plm-01"]["slots"][0])
    run.expect("outcome_with_card", response["outcome"], "awaiting_approval")
    run.expect("book_calls_before_yes", await run.book_calls(), 0)

    await run.click(response["approval"]["approval_id"])
    attempt = await run.attempt_closed()
    run.expect("attempt", attempt.status, "booked")
    run.expect("bookings", len(await run.provider_bookings()), 1)
    run.expect("verified_notice", (await run.notices()).count("booking_verified"), 1)
    run.expect("state", await run.state(), "done")


@scenario(
    "E02",
    "User corrects the budget",
    "Core flow",
    "Budget changes. Area, service and dates stay. Providers over the new budget are removed.",
)
async def e02(run: Eval) -> None:
    await run.search(extra="under 1500", max_budget=1500)
    before = await run.details()
    run.expect("budget_at_first", before["max_budget"], 1500)

    await run.say(
        "Actually make the budget 900.",
        ToolCall("update_request", {"max_budget": 900}),
        ToolCall("search_providers"),
        "Updated.",
    )
    after = await run.details()
    run.expect("budget_now", after["max_budget"], 900)
    for name in ("service", "area", "start_date", "end_date"):
        run.expect(f"{name}_unchanged", after[name] == before[name])
    prices = [item["price"] for item in (await run.workflow()).shortlist.values()]
    run.expect_that("prices_left", sorted(prices), bool(prices) and max(prices) <= 900)


@scenario(
    "E03",
    "Area known from memory",
    "Core flow",
    "A new chat fills the area from memory, with its source recorded.",
)
async def e03(run: Eval) -> None:
    await run.say(
        "I live in Indiranagar",
        ToolCall(
            "propose_memory",
            {"key": "area", "value": "Indiranagar", "quote": "I live in Indiranagar"},
        ),
        "Saved.",
    )
    memory = (await run.memories())[0]
    run.expect("memory_source", memory.source, "explicit")

    other_chat = "second-chat-e03"
    await run.say(
        "I need a plumber next week",
        ToolCall("update_request", {"service": "plumber", "area": "Indiranagar"}),
        ToolCall("resolve_dates", {"phrase": "next week"}),
        ToolCall("search_providers"),
        "Using your area, Indiranagar.",
        session=other_chat,
    )
    workflow = await run.workflow(other_chat)
    run.expect("area_source", workflow.request["area"]["source"], "memory")
    run.expect("area_value", workflow.request["area"]["value"], "Indiranagar")
    filled = [event.data for event in await run.all_events("filled_from_memory")]
    run.expect("traced", filled, [{"fields": ["area"]}])


def _days(start: str, end: str) -> list[date]:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    return [first + timedelta(days=n) for n in range((last - first).days + 1)]


def _hour(slot: str) -> int:
    parsed = catalog.parse_slot(slot)
    assert parsed is not None
    return parsed.hour


@scenario(
    "E20",
    '"Same electrician, not before 10"',
    "Core flow",
    "Provider comes from history, in a new chat. Earlier slots are removed. Booking is verified.",
)
async def e20(run: Eval) -> None:
    run.user = "demo-user"
    # The seeded past booking is with ele-02, and that provider does open at 09:00 that week.
    await run.say(
        "Book the same electrician as last time, but not before 10 a.m.",
        ToolCall("use_past_provider", {"service": "electrician"}),
        ToolCall("update_request", {"earliest_time": "10:00"}),
        "Sharma Electrical again. Which area, and when?",
    )
    first = await run.workflow()
    run.expect("provider_source", first.request["provider_id"]["source"], "history")
    run.expect("provider_from_history", first.request["provider_id"]["value"], "ele-02")
    run.expect("service_from_history", first.request["service"]["value"], "electrician")

    await run.say(
        f"In {AREA}, next week",
        ToolCall("update_request", {"area": AREA}),
        ToolCall("resolve_dates", {"phrase": "next week"}),
        ToolCall("search_providers"),
        "Here are the times.",
    )
    workflow = await run.workflow()
    run.expect("only_that_provider", sorted(workflow.shortlist), ["ele-02"])
    slots = workflow.shortlist["ele-02"]["slots"]
    hours = sorted({_hour(slot) for slot in slots})
    run.expect_that("hours_offered", hours, bool(hours) and min(hours) >= 10)

    days = _days(workflow.request["start_date"]["value"], workflow.request["end_date"]["value"])
    provider = catalog.CATALOG["ele-02"]
    raw_hours = {_hour(slot) for day in days for slot in catalog.slots_on(provider, day)}
    run.expect("provider_had_earlier_slots", min(raw_hours) < 10)

    await run.set_provider(book_outcomes=["success"])
    response = await run.propose("ele-02", slots[0])
    await run.click(response["approval"]["approval_id"])
    attempt = await run.attempt_closed()
    run.expect("attempt", attempt.status, "booked")
    run.expect("booked_with", attempt.provider_id, "ele-02")


@scenario(
    "E20B",
    "Same provider, but two past providers fit",
    "Core flow",
    "Nothing is saved until the user chooses. Code offers only their real past providers.",
)
async def e20b(run: Eval) -> None:
    await run.booked("ele-02", "electrician", AREA)
    run.session = "second-chat-e20b"
    await run.booked("ele-06", "electrician", AREA)
    await run.advance_provider_clock(60 * 86400)

    run.session = "third-chat-e20b"
    await run.say(
        "The same electrician as last time please",
        ToolCall("use_past_provider", {"service": "electrician"}),
        "Which one: Sharma Electrical or Nair Electrical?",
    )
    calls = [
        event for event in await run.all_events("tool_call") if event.name == "use_past_provider"
    ]
    run.expect("options_returned", len(calls[-1].data["result"]["several_match"]), 2)
    run.expect("nothing_saved", await run.details(), {})

    await run.say(
        "The same one with ele-07",
        ToolCall("use_past_provider", {"service": "electrician", "provider_id": "ele-07"}),
        "That is not one of your past providers.",
    )
    run.expect("invented_provider_refused", "provider_id" in await run.details(), False)

    await run.say(
        "The same Nair one",
        ToolCall("use_past_provider", {"service": "electrician", "provider_id": "ele-06"}),
        "Nair Electrical it is.",
    )
    run.expect("chosen_provider_saved", (await run.details())["provider_id"], "ele-06")
