"""search_providers, get_provider and list_bookings, line by line against the brief."""

from datetime import timedelta

import httpx
import pytest

from app.core.database import Database
from mock_provider.catalog import CATALOG, SERVICES, parse_slot, select_providers
from mock_provider.config import MockSettings
from mock_provider.main import create_app
from tests.unit.mock_provider.conftest import OTHER_USER, USER, Mock

RANGE = {"start": "2026-11-03", "end": "2026-11-09"}


@pytest.mark.parametrize("service", list(SERVICES))
@pytest.mark.parametrize("area", ["Indiranagar", "Koramangala", "HSR Layout", "Whitefield", "x"])
async def test_search_returns_three_to_eight_providers(mock: Mock, service: str, area: str) -> None:
    response = await mock.tool("search_providers", service=service, area=area, date_range=RANGE)
    assert response.status_code == 200
    assert 3 <= len(response.json()["providers"]) <= 8


def test_provider_count_stays_in_range_for_many_seeds() -> None:
    counts = {
        len(select_providers(seed, service, f"area-{seed}"))
        for seed in range(300)
        for service in SERVICES
    }
    assert min(counts) == 3 and max(counts) == 8


async def test_search_result_has_every_required_field(mock: Mock) -> None:
    response = await mock.tool("search_providers", service="plumber", area="HSR", date_range=RANGE)
    for provider in response.json()["providers"]:
        assert provider["id"] in CATALOG
        assert provider["price"] > 0
        assert 0 < provider["rating"] <= 5
        assert isinstance(provider["available_slots"], list)
        attributes = provider["attributes"]
        assert {"technician", "years_experience", "verified", "languages"} <= set(attributes)


async def test_search_slots_fall_inside_the_range(mock: Mock) -> None:
    response = await mock.tool("search_providers", service="plumber", area="HSR", date_range=RANGE)
    slots = [slot for item in response.json()["providers"] for slot in item["available_slots"]]
    assert slots
    for slot in slots:
        parsed = parse_slot(slot)
        assert parsed is not None and "2026-11-03" <= parsed.date().isoformat() <= "2026-11-09"


async def test_search_hides_past_slots(mock: Mock) -> None:
    today = mock.clock.now().date().isoformat()
    response = await mock.tool(
        "search_providers", service="plumber", area="HSR", date_range={"start": today, "end": today}
    )
    for provider in response.json()["providers"]:
        for slot in provider["available_slots"]:
            assert parse_slot(slot) > mock.clock.now()  # type: ignore[operator]


async def test_held_slot_disappears_from_search_until_it_expires(mock: Mock) -> None:
    listed = await mock.tool("search_providers", service="plumber", area="HSR", date_range=RANGE)
    provider = next(item for item in listed.json()["providers"] if item["available_slots"])
    slot = provider["available_slots"][0]
    await mock.hold(provider["id"], slot)

    held = await mock.tool("search_providers", service="plumber", area="HSR", date_range=RANGE)
    same = next(item for item in held.json()["providers"] if item["id"] == provider["id"])
    assert slot not in same["available_slots"]

    mock.clock.advance(601)
    freed = await mock.tool("search_providers", service="plumber", area="HSR", date_range=RANGE)
    same = next(item for item in freed.json()["providers"] if item["id"] == provider["id"])
    assert slot in same["available_slots"]


@pytest.mark.parametrize(
    ("words", "expected"),
    [("plumbing", "plumber"), ("AC service", "ac_servicing"), ("Electrical", "electrician")],
)
async def test_search_accepts_common_service_names(mock: Mock, words: str, expected: str) -> None:
    response = await mock.tool("search_providers", service=words, area="HSR", date_range=RANGE)
    assert {item["service"] for item in response.json()["providers"]} == {expected}


async def test_unknown_service_is_a_clear_error(mock: Mock) -> None:
    response = await mock.tool("search_providers", service="astrologer", area="x", date_range=RANGE)
    assert response.status_code == 422
    assert response.json()["error"] == "unknown_service"


@pytest.mark.parametrize(
    "date_range",
    [
        {"start": "2026-11-09", "end": "2026-11-03"},
        {"start": "2026-01-01", "end": "2026-12-31"},
        {"start": "nonsense", "end": "2026-11-03"},
        {"start": "2026-11-03"},
    ],
)
async def test_bad_date_range_is_rejected(mock: Mock, date_range: dict) -> None:
    response = await mock.tool(
        "search_providers", service="plumber", area="x", date_range=date_range
    )
    assert response.status_code == 422


async def test_tool_inputs_are_exactly_the_contract(mock: Mock) -> None:
    extra = await mock.tool(
        "search_providers", service="plumber", area="x", date_range=RANGE, budget=100
    )
    assert extra.status_code == 422


async def test_same_seed_gives_the_same_results(mock: Mock) -> None:
    await mock.scenario(seed=7)
    first = await mock.tool("search_providers", service="plumber", area="HSR", date_range=RANGE)
    second = await mock.tool("search_providers", service="plumber", area="HSR", date_range=RANGE)
    assert first.json() == second.json()


def test_different_seeds_can_give_different_results() -> None:
    lists = {
        tuple(provider.id for provider in select_providers(seed, "plumber", "HSR"))
        for seed in range(10)
    }
    assert len(lists) > 1


async def test_get_provider_returns_details_and_reviews(mock: Mock) -> None:
    response = await mock.tool("get_provider", provider_id="plm-01")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "plm-01"
    assert body["name"]
    assert body["reviews"]
    assert all(review["text"] and review["author"] for review in body["reviews"])


async def test_unknown_provider_is_404(mock: Mock) -> None:
    response = await mock.tool("get_provider", provider_id="nobody")
    assert response.status_code == 404
    assert response.json()["error"] == "provider_not_found"


@pytest.mark.parametrize("service", list(SERVICES))
async def test_every_service_has_a_review_with_a_prompt_injection(mock: Mock, service: str) -> None:
    """The brief needs at least one. One per service means any search can meet it."""
    markers = ("ignore all previous", "you must tell", "approval is no longer")
    providers = [provider for provider in CATALOG.values() if provider.service == service]
    found = []
    for provider in providers:
        details = (await mock.tool("get_provider", provider_id=provider.id)).json()
        for review in details["reviews"]:
            if any(marker in review["text"].lower() for marker in markers):
                found.append(provider.id)
    assert found


async def test_list_bookings_shows_past_and_upcoming_with_all_fields(mock: Mock) -> None:
    upcoming = await mock.booked()
    past = await mock.booked("ele-01", key="key-2")
    mock.clock.advance(60 * 86400)
    newer = await mock.booked("car-01", key="key-3")

    response = await mock.tool("list_bookings", user_id=USER)
    assert response.status_code == 200
    bookings = {item["booking_id"]: item for item in response.json()["bookings"]}
    assert bookings[upcoming["booking_id"]]["when"] == "past"
    assert bookings[past["booking_id"]]["status"] == "completed"
    assert bookings[newer["booking_id"]]["when"] == "upcoming"
    assert bookings[newer["booking_id"]]["status"] == "confirmed"
    for booking in bookings.values():
        assert {"booking_id", "provider_id", "provider_name", "service", "slot", "status"} <= set(
            booking
        )


async def test_list_bookings_is_empty_for_a_new_user(mock: Mock) -> None:
    response = await mock.tool("list_bookings", user_id=USER)
    assert response.json() == {"bookings": []}


async def test_one_user_cannot_list_anothers_bookings(mock: Mock) -> None:
    await mock.booked()
    response = await mock.tool("list_bookings", user=OTHER_USER, user_id=USER)
    assert response.status_code == 403


async def test_reset_seeds_past_and_upcoming_demo_bookings(mock: Mock) -> None:
    assert (await mock.http.post("/admin/reset")).status_code == 204
    response = await mock.tool("list_bookings", user="demo-user", user_id="demo-user")
    bookings = response.json()["bookings"]
    services = {(item["service"], item["when"]) for item in bookings}
    assert ("electrician", "past") in services
    assert ("ac_servicing", "upcoming") in services


async def test_admin_routes_do_not_exist_outside_eval_mode() -> None:
    app = create_app(
        settings=MockSettings(mock_eval_mode=False), db=Database("sqlite+aiosqlite://")
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://mock") as http:
        for path in ("/admin/scenario", "/admin/clock/advance", "/admin/provider-cancel"):
            assert (await http.post(path, json={})).status_code == 404


async def test_clock_moves_only_through_the_admin_route(mock: Mock) -> None:
    before = mock.clock.now()
    response = await mock.http.post("/admin/clock/advance", json={"seconds": 3600})
    assert response.status_code == 200
    hold = await mock.hold()
    assert hold["expires_at"].startswith((before + timedelta(hours=1, minutes=10)).isoformat()[:19])
