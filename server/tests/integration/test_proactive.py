"""E24 and E25 on real PostgreSQL, Redis and the mock service. Rules the unit tests cannot prove."""

import asyncio
from time import monotonic

import pytest
from sqlalchemy import select

from app.domains.proactive.models import BookingDisruption
from app.domains.proactive.service import CANCEL_NOTICE, REPLACEMENT_NOTICE
from app.domains.workflow.models import WorkflowState as S
from tests.integration import test_booking_saga
from tests.integration.test_booking_saga import Rig

pytestmark = pytest.mark.integration

migrated_schema = test_booking_saga.migrated_schema
server_url = test_booking_saga.server_url
rig = test_booking_saga.rig


async def booked(rig: Rig) -> dict:
    await rig.scenario(book_outcomes=["success"])
    card = await rig.card()
    await rig.core.approvals.confirm_click(card.approval_id, rig.user)
    await rig.core.saga.run((await rig.attempts())[0].id)
    return (await rig.provider_bookings())[0]


async def provider_cancels(rig: Rig, booking: dict) -> None:
    response = await rig.provider.post(
        "/admin/provider-cancel", json={"booking_id": booking["booking_id"]}
    )
    assert response.status_code == 200, response.text


async def kinds(rig: Rig) -> list[str]:
    return [note.kind for note in await rig.core.notifications.list_after(rig.user, rig.session)]


async def row(rig: Rig) -> BookingDisruption:
    async with rig.core.db.session_scope() as session:
        found = await session.scalar(
            select(BookingDisruption).where(BookingDisruption.user_id == rig.user)
        )
    assert found is not None
    return found


async def test_polls_at_the_same_time_make_one_notice(rig: Rig) -> None:
    booking = await booked(rig)
    await provider_cancels(rig, booking)

    await asyncio.gather(*(rig.core.proactive.poll() for _ in range(5)))
    await rig.core.proactive.poll()

    found = await kinds(rig)
    assert found.count(CANCEL_NOTICE) == 1
    assert found.count(REPLACEMENT_NOTICE) == 1
    snapshot = await rig.core.workflows.snapshot(rig.user, rig.session)
    assert snapshot.state == S.COMPARING
    assert booking["provider_id"] not in snapshot.shortlist
    # Rebooking starts from a new card. The poll made no approval and booked nothing.
    assert (await rig.core.approvals.view(rig.user, rig.session)).card is None
    assert len(await rig.attempts()) == 1


async def test_a_blank_errand_gives_way_under_the_real_unique_index(rig: Rig) -> None:
    """The usual case: "thanks" after a booking opens a blank errand in the same chat."""
    booking = await booked(rig)
    await rig.core.workflows.record_user_message(rig.user, rig.session, "thanks")
    await provider_cancels(rig, booking)

    await rig.core.proactive.poll()

    assert (await row(rig)).reason == "replacements_offered"
    assert (await rig.core.workflows.snapshot(rig.user, rig.session)).state == S.COMPARING


async def test_a_past_booking_gets_no_notice_and_the_reason_is_kept(rig: Rig) -> None:
    booking = await booked(rig)
    await provider_cancels(rig, booking)
    response = await rig.provider.post("/admin/clock/advance", json={"seconds": 60 * 86400})
    assert response.status_code == 200, response.text
    before = await kinds(rig)

    await rig.core.proactive.poll()

    assert await kinds(rig) == before
    assert (await row(rig)).reason == "past_booking"


async def test_a_wake_up_delivers_at_once_over_real_redis(rig: Rig) -> None:
    notifications = rig.core.notifications
    stream = notifications.stream(rig.user, 0)
    waiting = asyncio.create_task(anext(stream))
    await asyncio.sleep(0.5)

    started = monotonic()
    async with rig.core.db.session_scope() as session:
        await notifications.add_in(session, rig.user, rig.session, "news", "hello")
    item = await asyncio.wait_for(waiting, timeout=5)

    assert item is not None and item.text == "hello"
    # Well under the 10 s the reader would otherwise wait.
    assert monotonic() - started < 3
    await stream.aclose()
