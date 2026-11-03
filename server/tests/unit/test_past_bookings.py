"""F8: "the same electrician as last time" is resolved by code, from the user's own bookings."""

from app.domains.workflow import request as details
from app.domains.workflow.filters import apply_hard_limits, build_shortlist
from app.domains.workflow.models import WorkflowState as S
from app.domains.workflow.past_bookings import past_providers, refers_to_past
from app.domains.workflow.service import WorkflowService
from tests.conftest import make_ready

USER, SESSION = "u1", "s1"
SAID = "Book the same electrician as last time, but not before 10 a.m."


def booking(provider: str, slot: str, service: str = "electrician", **extra: object) -> dict:
    return {
        "booking_id": f"bkg-{provider}",
        "provider_id": provider,
        "provider_name": f"Shop {provider}",
        "service": service,
        "slot": slot,
        "status": "completed",
        "when": "past",
        **extra,
    }


ONE = [booking("ele-02", "2026-10-01T10:00:00+05:30")]
TWO = [*ONE, booking("ele-05", "2026-10-20T10:00:00+05:30")]


def provider(id_: str, slots: list[str]) -> dict:
    return {
        "id": id_,
        "name": id_,
        "service": "electrician",
        "price": 500,
        "available_slots": slots,
    }


def test_only_finished_visits_of_that_service_count() -> None:
    bookings = [
        *ONE,
        booking("ele-03", "2026-10-02T10:00:00+05:30", status="cancelled"),
        booking("ele-04", "2026-10-03T10:00:00+05:30", status="cancelled_by_provider"),
        booking("ele-06", "2030-10-03T10:00:00+05:30", status="confirmed", when="upcoming"),
        booking("plm-01", "2026-10-04T10:00:00+05:30", service="plumber"),
    ]
    assert [item["provider_id"] for item in past_providers(bookings, "Electrician")] == ["ele-02"]


def test_two_visits_to_one_provider_are_one_match_with_the_latest_day() -> None:
    bookings = [*ONE, booking("ele-02", "2026-10-09T10:00:00+05:30")]
    found = past_providers(bookings, "electrician")
    assert len(found) == 1
    assert found[0]["last_visit"].startswith("2026-10-09")


def test_past_words_are_needed_and_not_before_is_not_one() -> None:
    assert refers_to_past(SAID)
    assert refers_to_past("my usual plumber please")
    assert not refers_to_past("an electrician, not before 10 a.m.")


async def test_one_match_saves_provider_and_service_from_history(workflow: WorkflowService) -> None:
    await workflow.record_user_message(USER, SESSION, SAID)

    result = await workflow.use_past_provider(USER, SESSION, "electrician", None, ONE)

    assert result["provider"]["provider_id"] == "ele-02"
    assert "area" in result["missing_fields"]
    snapshot = await workflow.snapshot(USER, SESSION)
    assert snapshot.details["provider_id"] == "ele-02"
    assert snapshot.details["service"] == "electrician"


async def test_several_matches_save_nothing_until_the_user_chooses(
    workflow: WorkflowService,
) -> None:
    await workflow.record_user_message(USER, SESSION, SAID)

    result = await workflow.use_past_provider(USER, SESSION, "electrician", None, TWO)

    assert [item["provider_id"] for item in result["several_match"]] == ["ele-05", "ele-02"]
    assert (await workflow.snapshot(USER, SESSION)).details == {}

    chosen = await workflow.use_past_provider(USER, SESSION, "electrician", "ele-02", TWO)
    assert chosen["provider"]["provider_id"] == "ele-02"
    assert (await workflow.snapshot(USER, SESSION)).details["provider_id"] == "ele-02"


async def test_no_match_or_an_invented_provider_saves_nothing(workflow: WorkflowService) -> None:
    await workflow.record_user_message(USER, SESSION, SAID)

    assert (await workflow.use_past_provider(USER, SESSION, "plumber", None, ONE))["error"] == (
        "no_past_booking"
    )
    invented = await workflow.use_past_provider(USER, SESSION, "electrician", "ele-07", ONE)
    assert invented["error"] == "provider_not_in_past_bookings"
    assert (await workflow.snapshot(USER, SESSION)).details == {}


async def test_the_model_cannot_use_it_unless_the_user_asked(workflow: WorkflowService) -> None:
    await workflow.record_user_message(USER, SESSION, "I need an electrician")

    result = await workflow.use_past_provider(USER, SESSION, "electrician", None, ONE)

    assert result["error"] == "past_provider_not_requested"
    assert (await workflow.snapshot(USER, SESSION)).details == {}


def test_search_keeps_to_the_chosen_provider_and_the_time_limit() -> None:
    request = {
        details.CHOSEN_PROVIDER: {"value": "ele-02"},
        "earliest_time": {"value": "10:00"},
    }
    found = [
        provider("ele-02", ["2026-11-10T09:00:00+05:30", "2026-11-10T11:00:00+05:30"]),
        provider("ele-03", ["2026-11-10T11:00:00+05:30"]),
    ]

    shortlist = build_shortlist(found, request)
    shown = apply_hard_limits(found, request)

    assert list(shortlist) == ["ele-02"]
    assert shortlist["ele-02"]["slots"] == ["2026-11-10T11:00:00+05:30"]
    assert [item["id"] for item in shown["providers"]] == ["ele-02"]
    assert shown["removed"]["not_the_past_provider"] == 1


def test_a_provider_that_is_not_open_is_said_plainly() -> None:
    request = {details.CHOSEN_PROVIDER: {"value": "ele-02"}}
    shown = apply_hard_limits([provider("ele-03", ["2026-11-10T11:00:00+05:30"])], request)
    assert shown["providers"] == []
    assert "past_provider_not_open" in shown


async def test_changing_the_service_or_clearing_drops_the_provider(
    workflow: WorkflowService,
) -> None:
    await make_ready(workflow, USER, SESSION, service="electrician")
    await workflow.record_user_message(USER, SESSION, SAID)
    await workflow.use_past_provider(USER, SESSION, "electrician", None, ONE)

    await workflow.update_request(USER, SESSION, {"service": "plumber"})
    assert "provider_id" not in (await workflow.snapshot(USER, SESSION)).details

    await workflow.update_request(USER, SESSION, {"service": "electrician"})
    await workflow.use_past_provider(USER, SESSION, "electrician", None, ONE)
    await workflow.update_request(USER, SESSION, {}, ["provider_id"])
    assert "provider_id" not in (await workflow.snapshot(USER, SESSION)).details


async def test_a_new_provider_after_a_search_needs_a_new_search(workflow: WorkflowService) -> None:
    await make_ready(workflow, USER, SESSION, service="electrician")
    await workflow.record_user_message(USER, SESSION, SAID)
    await workflow.begin_search(USER, SESSION)
    await workflow.finish_search(USER, SESSION, [provider("ele-03", ["2026-11-10T11:00:00+05:30"])])

    await workflow.use_past_provider(USER, SESSION, "electrician", None, ONE)

    assert (await workflow.snapshot(USER, SESSION)).state == S.INTAKE
