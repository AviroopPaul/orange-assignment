"""hold_slot, book, get_booking_status, cancel and reschedule, line by line against the brief."""

from collections import Counter
from datetime import datetime

import pytest

from mock_provider.outcomes import BookOutcome, decide_book_outcome
from tests.unit.mock_provider.conftest import OTHER_USER, Mock

# hold_slot


async def test_hold_lasts_exactly_ten_minutes_and_has_an_id(mock: Mock) -> None:
    hold = await mock.hold()
    assert hold["hold_id"]
    expires = datetime.fromisoformat(hold["expires_at"].replace("Z", "+00:00"))
    assert (expires - mock.clock.now()).total_seconds() == 600


async def test_quoted_price_matches_search_price_by_default(mock: Mock) -> None:
    hold = await mock.hold("plm-01")
    assert hold["quoted_price"] == hold["search_price"]


async def test_some_providers_quote_more_than_their_search_price(mock: Mock) -> None:
    hold = await mock.hold("plm-02")
    assert hold["quoted_price"] > hold["search_price"]


async def test_quote_difference_is_configurable(mock: Mock) -> None:
    await mock.scenario(quote_deltas={"plm-01": 300, "plm-02": 0})
    raised = await mock.hold("plm-01")
    assert raised["quoted_price"] == raised["search_price"] + 300
    flat = await mock.hold("plm-02")
    assert flat["quoted_price"] == flat["search_price"]


async def test_only_one_person_can_hold_a_slot(mock: Mock) -> None:
    slot = mock.free_slots("plm-01")[0]
    await mock.hold("plm-01", slot)
    second = await mock.tool("hold_slot", user=OTHER_USER, provider_id="plm-01", slot=slot)
    assert second.status_code == 409
    assert second.json()["error"] == "slot_unavailable"


async def test_slot_can_be_held_again_after_expiry(mock: Mock) -> None:
    slot = mock.free_slots("plm-01")[0]
    await mock.hold("plm-01", slot)
    mock.clock.advance(601)
    again = await mock.tool("hold_slot", user=OTHER_USER, provider_id="plm-01", slot=slot)
    assert again.status_code == 200


@pytest.mark.parametrize("slot", ["not-a-time", "2026-11-04T03:17:00+05:30"])
async def test_hold_rejects_a_time_the_provider_does_not_work(mock: Mock, slot: str) -> None:
    response = await mock.tool("hold_slot", provider_id="plm-01", slot=slot)
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_slot"


async def test_hold_rejects_a_past_slot(mock: Mock) -> None:
    slot = mock.free_slots("plm-01")[0]
    mock.clock.advance(30 * 86400)
    response = await mock.tool("hold_slot", provider_id="plm-01", slot=slot)
    assert response.status_code == 422


async def test_hold_needs_to_know_the_caller(mock: Mock) -> None:
    slot = mock.free_slots("plm-01")[0]
    response = await mock.tool("hold_slot", user=None, provider_id="plm-01", slot=slot)
    assert response.status_code == 422


# book: the distribution


def test_book_outcomes_match_the_required_split() -> None:
    samples = 100_000
    counts = Counter(decide_book_outcome(42, f"key-{n}", 1) for n in range(samples))
    assert counts[BookOutcome.SUCCESS] / samples == pytest.approx(0.70, abs=0.01)
    assert counts[BookOutcome.SLOT_TAKEN] / samples == pytest.approx(0.15, abs=0.01)
    timeouts = counts[BookOutcome.TIMEOUT_BOOKED] + counts[BookOutcome.TIMEOUT_NOT_BOOKED]
    assert timeouts / samples == pytest.approx(0.15, abs=0.01)
    assert counts[BookOutcome.TIMEOUT_BOOKED] / timeouts == pytest.approx(0.5, abs=0.02)


def test_book_outcome_depends_only_on_seed_key_and_attempt() -> None:
    forward = [decide_book_outcome(1, f"key-{n}", 1) for n in range(100)]
    backward = [decide_book_outcome(1, f"key-{n}", 1) for n in reversed(range(100))]
    assert forward == list(reversed(backward))


def test_seed_changes_the_outcomes() -> None:
    one = [decide_book_outcome(1, f"key-{n}", 1) for n in range(100)]
    two = [decide_book_outcome(2, f"key-{n}", 1) for n in range(100)]
    assert one != two


async def test_unscripted_book_follows_the_seeded_hash(mock: Mock) -> None:
    """Runs the real route with no script, so the seeded path is covered, not only the function."""
    await mock.scenario(seed=42)
    key = next(f"k{n}" for n in range(200) if decide_book_outcome(42, f"k{n}", 1) == "success")
    hold = await mock.hold()
    response = await mock.tool("book", hold_id=hold["hold_id"], idempotency_key=key)
    assert response.status_code == 200


# book: each outcome


async def test_success_creates_a_confirmed_booking(mock: Mock) -> None:
    booking = await mock.booked()
    assert booking["status"] == "confirmed"
    assert booking["booking_id"]

    status = await mock.tool("get_booking_status", idempotency_key="key-1")
    assert status.json()["status"] == "booked"
    assert status.json()["booking_id"] == booking["booking_id"]


async def test_booking_uses_the_quoted_price(mock: Mock) -> None:
    await mock.scenario(book_outcomes=["success"], quote_deltas={"plm-01": 250})
    hold = await mock.hold("plm-01")
    response = await mock.tool("book", hold_id=hold["hold_id"], idempotency_key="k")
    assert response.json()["price"] == hold["search_price"] + 250


async def test_slot_taken_makes_no_booking(mock: Mock) -> None:
    await mock.scenario(book_outcomes=["slot_taken"])
    hold = await mock.hold()
    response = await mock.tool("book", hold_id=hold["hold_id"], idempotency_key="k")
    assert response.status_code == 409
    assert response.json()["error"] == "slot_taken"

    status = await mock.tool("get_booking_status", idempotency_key="k")
    assert status.json()["status"] == "not_booked"
    assert (await mock.tool("list_bookings", user_id="user-1")).json()["bookings"] == []


async def test_slot_taken_is_stable_on_replay(mock: Mock) -> None:
    await mock.scenario(book_outcomes=["slot_taken", "success"])
    hold = await mock.hold()
    await mock.tool("book", hold_id=hold["hold_id"], idempotency_key="k")
    again = await mock.tool("book", hold_id=hold["hold_id"], idempotency_key="k")
    assert again.status_code == 409 and again.json()["error"] == "slot_taken"


async def test_timeout_that_did_book_is_visible_only_through_status(mock: Mock) -> None:
    await mock.scenario(book_outcomes=["timeout_booked"])
    hold = await mock.hold()
    response = await mock.tool("book", hold_id=hold["hold_id"], idempotency_key="k")
    assert response.status_code == 504
    assert response.json()["error"] == "timeout"
    assert "booking_id" not in response.json()

    status = await mock.tool("get_booking_status", idempotency_key="k")
    assert status.json()["status"] == "booked"
    assert len((await mock.tool("list_bookings", user_id="user-1")).json()["bookings"]) == 1


async def test_timeout_that_did_not_book_looks_the_same_to_the_caller(mock: Mock) -> None:
    await mock.scenario(book_outcomes=["timeout_booked", "timeout_not_booked"])
    booked_hold = await mock.hold("plm-01")
    booked = await mock.tool("book", hold_id=booked_hold["hold_id"], idempotency_key="a")
    missed_hold = await mock.hold("ele-01")
    missed = await mock.tool("book", hold_id=missed_hold["hold_id"], idempotency_key="b")

    assert booked.status_code == missed.status_code == 504
    assert booked.json() == missed.json()
    status_a = await mock.tool("get_booking_status", idempotency_key="a")
    status_b = await mock.tool("get_booking_status", idempotency_key="b")
    assert status_a.json()["status"] == "booked"
    assert status_b.json()["status"] == "not_booked"


async def test_retry_after_timeout_not_booked_can_succeed_with_the_same_key(mock: Mock) -> None:
    await mock.scenario(book_outcomes=["timeout_not_booked", "success"])
    hold = await mock.hold()
    first = await mock.tool("book", hold_id=hold["hold_id"], idempotency_key="k")
    assert first.status_code == 504
    second = await mock.tool("book", hold_id=hold["hold_id"], idempotency_key="k")
    assert second.status_code == 200
    assert (await mock.tool("get_booking_status", idempotency_key="k")).json()["status"] == "booked"


async def test_retry_after_timeout_booked_returns_the_same_booking(mock: Mock) -> None:
    await mock.scenario(book_outcomes=["timeout_booked", "success"])
    hold = await mock.hold()
    await mock.tool("book", hold_id=hold["hold_id"], idempotency_key="k")
    retry = await mock.tool("book", hold_id=hold["hold_id"], idempotency_key="k")

    assert retry.status_code == 200
    bookings = (await mock.tool("list_bookings", user_id="user-1")).json()["bookings"]
    assert [item["booking_id"] for item in bookings] == [retry.json()["booking_id"]]


async def test_repeated_key_never_books_twice(mock: Mock) -> None:
    await mock.scenario(book_outcomes=["success"])
    hold = await mock.hold()
    responses = [
        await mock.tool("book", hold_id=hold["hold_id"], idempotency_key="k") for _ in range(5)
    ]
    assert {response.json()["booking_id"] for response in responses} == {
        responses[0].json()["booking_id"]
    }
    assert len((await mock.tool("list_bookings", user_id="user-1")).json()["bookings"]) == 1


async def test_a_used_hold_cannot_book_again_under_a_new_key(mock: Mock) -> None:
    await mock.scenario(book_outcomes=["success", "success"])
    hold = await mock.hold()
    await mock.tool("book", hold_id=hold["hold_id"], idempotency_key="a")
    response = await mock.tool("book", hold_id=hold["hold_id"], idempotency_key="b")
    assert response.status_code == 409
    assert response.json()["error"] == "hold_not_active"


async def test_key_cannot_be_reused_for_a_different_hold(mock: Mock) -> None:
    await mock.scenario(book_outcomes=["success", "success"])
    first = await mock.hold("plm-01")
    second = await mock.hold("ele-01")
    await mock.tool("book", hold_id=first["hold_id"], idempotency_key="k")
    response = await mock.tool("book", hold_id=second["hold_id"], idempotency_key="k")
    assert response.status_code == 409
    assert response.json()["error"] == "idempotency_conflict"


async def test_booked_slot_is_taken_for_everyone(mock: Mock) -> None:
    await mock.scenario(book_outcomes=["success"])
    slot = mock.free_slots("plm-01")[0]
    hold = await mock.hold("plm-01", slot)
    await mock.tool("book", hold_id=hold["hold_id"], idempotency_key="k")
    mock.clock.advance(601)
    second = await mock.tool("hold_slot", user=OTHER_USER, provider_id="plm-01", slot=slot)
    assert second.status_code == 409


# book: hold_expired


async def test_book_at_the_expiry_moment_returns_hold_expired(mock: Mock) -> None:
    await mock.scenario(book_outcomes=["success"])
    hold = await mock.hold()
    mock.clock.advance(600)
    response = await mock.tool("book", hold_id=hold["hold_id"], idempotency_key="k")
    assert response.status_code == 410
    assert response.json()["error"] == "hold_expired"
    assert (await mock.tool("get_booking_status", idempotency_key="k")).json()["status"] == (
        "not_booked"
    )


async def test_book_just_before_expiry_still_works(mock: Mock) -> None:
    await mock.scenario(book_outcomes=["success"])
    hold = await mock.hold()
    mock.clock.advance(599)
    response = await mock.tool("book", hold_id=hold["hold_id"], idempotency_key="k")
    assert response.status_code == 200


async def test_expired_hold_beats_any_scripted_outcome(mock: Mock) -> None:
    await mock.scenario(book_outcomes=["timeout_booked"])
    hold = await mock.hold()
    mock.clock.advance(601)
    response = await mock.tool("book", hold_id=hold["hold_id"], idempotency_key="k")
    assert response.status_code == 410


async def test_replay_after_expiry_still_returns_the_booking(mock: Mock) -> None:
    await mock.scenario(book_outcomes=["success"])
    hold = await mock.hold()
    first = await mock.tool("book", hold_id=hold["hold_id"], idempotency_key="k")
    mock.clock.advance(3600)
    replay = await mock.tool("book", hold_id=hold["hold_id"], idempotency_key="k")
    assert replay.status_code == 200
    assert replay.json()["booking_id"] == first.json()["booking_id"]


async def test_another_users_hold_cannot_be_booked(mock: Mock) -> None:
    hold = await mock.hold()
    response = await mock.tool(
        "book", user=OTHER_USER, hold_id=hold["hold_id"], idempotency_key="k"
    )
    assert response.status_code == 404


# get_booking_status


@pytest.mark.parametrize("body", [{}, {"hold_id": "h", "idempotency_key": "k"}])
async def test_status_needs_exactly_one_lookup(mock: Mock, body: dict) -> None:
    assert (await mock.tool("get_booking_status", **body)).status_code == 422


async def test_status_by_hold_and_by_key_agree(mock: Mock) -> None:
    await mock.scenario(book_outcomes=["success"])
    hold = await mock.hold()
    await mock.tool("book", hold_id=hold["hold_id"], idempotency_key="k")
    by_hold = (await mock.tool("get_booking_status", hold_id=hold["hold_id"])).json()
    by_key = (await mock.tool("get_booking_status", idempotency_key="k")).json()
    assert by_hold["status"] == by_key["status"] == "booked"
    assert by_hold["booking_id"] == by_key["booking_id"]


async def test_never_seen_key_means_not_booked(mock: Mock) -> None:
    """A crash before the call leaves a key the provider never saw. That is a definite answer."""
    response = await mock.tool("get_booking_status", idempotency_key="never-sent")
    assert response.status_code == 200
    assert response.json()["status"] == "not_booked"


async def test_held_but_unbooked_hold_is_not_booked(mock: Mock) -> None:
    hold = await mock.hold()
    status = await mock.tool("get_booking_status", hold_id=hold["hold_id"])
    assert status.json()["status"] == "not_booked"


async def test_unknown_hold_is_404(mock: Mock) -> None:
    assert (await mock.tool("get_booking_status", hold_id="nope")).status_code == 404


async def test_another_users_booking_status_is_hidden(mock: Mock) -> None:
    await mock.booked()
    by_key = await mock.tool("get_booking_status", user=OTHER_USER, idempotency_key="key-1")
    assert by_key.status_code == 404


# cancel


async def test_first_cancel_fails_with_503_and_the_retry_succeeds(mock: Mock) -> None:
    booking = await mock.booked()
    first = await mock.tool("cancel", booking_id=booking["booking_id"], idempotency_key="c")
    assert first.status_code == 503
    assert first.json()["error"] == "transient_error"

    still = await mock.tool("get_booking_status", idempotency_key="key-1")
    assert still.json()["status"] == "booked"

    second = await mock.tool("cancel", booking_id=booking["booking_id"], idempotency_key="c")
    assert second.status_code == 200
    assert second.json()["status"] == "cancelled"
    after = await mock.tool("get_booking_status", idempotency_key="key-1")
    assert after.json()["status"] == "cancelled"


async def test_every_booking_gets_its_own_first_failure(mock: Mock) -> None:
    one = await mock.booked("plm-01", "k1")
    two = await mock.booked("ele-01", "k2")
    for booking in (one, two):
        response = await mock.tool("cancel", booking_id=booking["booking_id"], idempotency_key="x")
        assert response.status_code == 503


async def test_repeated_cancel_key_replays_the_result(mock: Mock) -> None:
    booking = await mock.booked()
    await mock.tool("cancel", booking_id=booking["booking_id"], idempotency_key="c")
    done = await mock.tool("cancel", booking_id=booking["booking_id"], idempotency_key="c")
    again = await mock.tool("cancel", booking_id=booking["booking_id"], idempotency_key="c")
    assert again.status_code == 200
    assert again.json() == done.json()


async def test_cancel_under_a_new_key_after_cancelling_is_refused(mock: Mock) -> None:
    booking = await mock.booked()
    for _ in range(2):
        await mock.tool("cancel", booking_id=booking["booking_id"], idempotency_key="c")
    response = await mock.tool("cancel", booking_id=booking["booking_id"], idempotency_key="d")
    assert response.status_code == 409
    assert response.json()["error"] == "booking_not_active"


async def test_cancel_frees_the_slot(mock: Mock) -> None:
    await mock.scenario(book_outcomes=["success"])
    slot = mock.free_slots("plm-01")[0]
    hold = await mock.hold("plm-01", slot)
    booked = await mock.tool("book", hold_id=hold["hold_id"], idempotency_key="k")
    for _ in range(2):
        await mock.tool("cancel", booking_id=booked.json()["booking_id"], idempotency_key="c")
    again = await mock.tool("hold_slot", user=OTHER_USER, provider_id="plm-01", slot=slot)
    assert again.status_code == 200


async def test_another_users_booking_cannot_be_cancelled(mock: Mock) -> None:
    booking = await mock.booked()
    response = await mock.tool(
        "cancel", user=OTHER_USER, booking_id=booking["booking_id"], idempotency_key="c"
    )
    assert response.status_code == 404


async def test_cancel_unknown_booking_is_404(mock: Mock) -> None:
    response = await mock.tool("cancel", booking_id="nope", idempotency_key="c")
    assert response.status_code == 404


# reschedule


async def test_reschedule_moves_the_booking_and_frees_the_old_slot(mock: Mock) -> None:
    booking = await mock.booked()
    old_slot = booking["slot"]
    new_slot = next(slot for slot in mock.free_slots("plm-01", 4) if slot != old_slot)

    response = await mock.tool(
        "reschedule", booking_id=booking["booking_id"], slot=new_slot, idempotency_key="r"
    )
    assert response.status_code == 200
    assert response.json()["slot"] == new_slot
    assert response.json()["booking_id"] == booking["booking_id"]

    free = await mock.tool("hold_slot", user=OTHER_USER, provider_id="plm-01", slot=old_slot)
    assert free.status_code == 200
    taken = await mock.tool("hold_slot", user=OTHER_USER, provider_id="plm-01", slot=new_slot)
    assert taken.status_code == 409


async def test_configured_slot_returns_slot_conflict(mock: Mock) -> None:
    booking = await mock.booked()
    conflicted = next(s for s in mock.free_slots("plm-01", 4) if s != booking["slot"])
    await mock.scenario(slot_conflicts=[conflicted])

    response = await mock.tool(
        "reschedule", booking_id=booking["booking_id"], slot=conflicted, idempotency_key="r"
    )
    assert response.status_code == 409
    assert response.json()["error"] == "slot_conflict"

    unchanged = await mock.tool("list_bookings", user_id="user-1")
    assert unchanged.json()["bookings"][0]["slot"] == booking["slot"]


async def test_default_config_conflicts_at_midday(mock: Mock) -> None:
    from mock_provider.catalog import CATALOG, parse_slot, slots_on

    booking = await mock.booked()
    day = mock.clock.now().date()
    midday = None
    from datetime import timedelta

    while midday is None:
        day += timedelta(days=1)
        for slot in slots_on(CATALOG["plm-01"], day):
            if parse_slot(slot).hour == 12:  # type: ignore[union-attr]
                midday = slot
    response = await mock.tool(
        "reschedule", booking_id=booking["booking_id"], slot=midday, idempotency_key="r"
    )
    assert response.json()["error"] == "slot_conflict"


async def test_slot_held_by_someone_else_is_a_conflict(mock: Mock) -> None:
    booking = await mock.booked()
    held = next(s for s in mock.free_slots("plm-01", 4) if s != booking["slot"])
    await mock.tool("hold_slot", user=OTHER_USER, provider_id="plm-01", slot=held)
    response = await mock.tool(
        "reschedule", booking_id=booking["booking_id"], slot=held, idempotency_key="r"
    )
    assert response.status_code == 409
    assert response.json()["error"] == "slot_conflict"


async def test_repeated_reschedule_key_changes_nothing_more(mock: Mock) -> None:
    booking = await mock.booked()
    slots = [s for s in mock.free_slots("plm-01", 5) if s != booking["slot"]]
    first = await mock.tool(
        "reschedule", booking_id=booking["booking_id"], slot=slots[0], idempotency_key="r"
    )
    replay = await mock.tool(
        "reschedule", booking_id=booking["booking_id"], slot=slots[1], idempotency_key="r"
    )
    assert replay.json() == first.json()
    assert replay.json()["slot"] == slots[0]


async def test_reschedule_rejects_a_time_the_provider_does_not_work(mock: Mock) -> None:
    booking = await mock.booked()
    response = await mock.tool(
        "reschedule",
        booking_id=booking["booking_id"],
        slot="2026-11-04T03:17:00+05:30",
        idempotency_key="r",
    )
    assert response.status_code == 422


async def test_cancelled_booking_cannot_be_rescheduled(mock: Mock) -> None:
    booking = await mock.booked()
    for _ in range(2):
        await mock.tool("cancel", booking_id=booking["booking_id"], idempotency_key="c")
    slot = next(s for s in mock.free_slots("plm-01", 4) if s != booking["slot"])
    response = await mock.tool(
        "reschedule", booking_id=booking["booking_id"], slot=slot, idempotency_key="r"
    )
    assert response.status_code == 409
    assert response.json()["error"] == "booking_not_active"


async def test_key_cannot_cross_from_cancel_to_reschedule(mock: Mock) -> None:
    booking = await mock.booked()
    for _ in range(2):
        await mock.tool("cancel", booking_id=booking["booking_id"], idempotency_key="same")
    slot = mock.free_slots("plm-01", 4)[-1]
    response = await mock.tool(
        "reschedule", booking_id=booking["booking_id"], slot=slot, idempotency_key="same"
    )
    assert response.json()["error"] == "idempotency_conflict"


async def test_another_users_booking_cannot_be_rescheduled(mock: Mock) -> None:
    booking = await mock.booked()
    slot = mock.free_slots("plm-01", 4)[-1]
    response = await mock.tool(
        "reschedule",
        user=OTHER_USER,
        booking_id=booking["booking_id"],
        slot=slot,
        idempotency_key="r",
    )
    assert response.status_code == 404


# The simulated event


async def test_provider_cancel_shows_up_in_list_and_status(mock: Mock) -> None:
    booking = await mock.booked()
    response = await mock.http.post(
        "/admin/provider-cancel", json={"booking_id": booking["booking_id"]}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled_by_provider"

    listed = await mock.tool("list_bookings", user_id="user-1")
    assert listed.json()["bookings"][0]["status"] == "cancelled_by_provider"
    status = await mock.tool("get_booking_status", idempotency_key="key-1")
    assert status.json()["status"] == "cancelled_by_provider"


async def test_provider_cancel_frees_the_slot(mock: Mock) -> None:
    booking = await mock.booked()
    await mock.http.post("/admin/provider-cancel", json={"booking_id": booking["booking_id"]})
    again = await mock.tool(
        "hold_slot", user=OTHER_USER, provider_id=booking["provider_id"], slot=booking["slot"]
    )
    assert again.status_code == 200
