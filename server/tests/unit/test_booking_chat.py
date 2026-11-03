"""The whole path through a chat turn: real ADK loop, scripted model, real provider and saga."""

from collections.abc import AsyncIterator, Callable

import httpx
import pytest
from fastapi import FastAPI
from google.adk.sessions import InMemorySessionService
from redis.asyncio import Redis
from sqlalchemy import select

from app.container import ApiContainer, build_api_container, build_core
from app.core.config import Settings
from app.core.database import Database
from app.domains.agent.callbacks import FALSE_CLAIM_REPLY
from app.domains.agent.scripted import ScriptedLlm, ToolCall
from app.domains.bookings.models import Approval, ApprovalStatus, AttemptStatus, BookingAttempt
from app.domains.chat.schemas import ChatRequest, ChatResponse, FailureReason, Outcome
from app.domains.chat.service import BOOKING_RUNNING, CARD_REMINDER, NO_CHANGE
from app.main import create_app
from tests.conftest import FakeClock, RecordingPublisher, make_ready

USER = "u1"
SESSION = "s1"
SEARCH = ToolCall("search_providers")


class Flow:
    def __init__(
        self, container: ApiContainer, model: ScriptedLlm, publisher: RecordingPublisher
    ) -> None:
        self.container = container
        self.model = model
        self.publisher = publisher
        self.jobs_run = 0

    @property
    def core(self):
        return self.container.core

    async def say(self, text: str, *script: object) -> ChatResponse:
        self.model.script[:] = list(script)
        self.model.requests.clear()
        return await self.container.chat.handle_message(
            ChatRequest(user_id=USER, session_id=SESSION, message=text)
        )

    async def ready_slot(self) -> tuple[str, str]:
        """Details saved and dates confirmed, then a real search to choose from."""
        await make_ready(self.core.workflows, USER, SESSION)
        await self.say("find me one", SEARCH, "Here are some options.")
        async with self.core.db.session_scope() as session:
            from app.domains.workflow.repository import WorkflowRepository

            workflow = await WorkflowRepository(session).get_active(USER, SESSION)
        provider_id = next(item for item in workflow.shortlist if item.endswith("-01"))
        return provider_id, workflow.shortlist[provider_id]["slots"][0]

    async def show_card(self) -> ChatResponse:
        provider_id, slot = await self.ready_slot()
        return await self.say(
            "book the first one",
            ToolCall("propose_booking", {"provider_id": provider_id, "slot": slot}),
            "Please confirm the details on the card.",
        )

    async def run_jobs(self) -> None:
        await self.core.jobs.publish_pending()
        while self.jobs_run < len(self.publisher.published):
            self.jobs_run += 1
            await self.core.jobs.execute(self.publisher.published[self.jobs_run - 1])

    async def approvals(self) -> list[Approval]:
        async with self.core.db.session_scope() as session:
            return list(await session.scalars(select(Approval)))

    async def attempts(self) -> list[BookingAttempt]:
        async with self.core.db.session_scope() as session:
            return list(await session.scalars(select(BookingAttempt)))


@pytest.fixture
async def flow(
    settings: Settings,
    db: Database,
    redis: Redis,
    clock: FakeClock,
    publisher: RecordingPublisher,
    provider_transport: httpx.AsyncBaseTransport,
) -> AsyncIterator[Flow]:
    settings.tool_retry_backoff_s = 0
    core = build_core(
        settings,
        publisher=publisher,
        db=db,
        redis=redis,
        clock=clock,
        provider_transport=provider_transport,
    )
    model = ScriptedLlm(model="gemini-3.5-flash-lite", script=[])
    container = build_api_container(core, model=model, session_service=InMemorySessionService())
    yield Flow(container, model, publisher)
    await core.provider_client.close()


@pytest.fixture
def provider_scenario(provider_app: FastAPI) -> Callable:
    async def set_scenario(**config: object) -> None:
        transport = httpx.ASGITransport(app=provider_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://mock") as http:
            assert (await http.post("/admin/scenario", json=config)).status_code == 204

    return set_scenario


async def test_card_comes_back_beside_the_reply(flow: Flow) -> None:
    response = await flow.show_card()

    assert response.outcome == Outcome.AWAITING_APPROVAL
    assert response.approval is not None
    row = (await flow.approvals())[0]
    assert response.approval.approval_id == row.id
    assert response.approval.price == row.price
    assert row.status == ApprovalStatus.PENDING.value


async def test_a_typed_yes_books_without_the_model(flow: Flow, provider_scenario: Callable) -> None:
    await provider_scenario(book_outcomes=["success"])
    await flow.show_card()

    response = await flow.say("Yes", "The model must not run.")
    assert flow.model.requests == []
    assert response.outcome == Outcome.VERIFYING
    assert "not confirmed yet" in response.reply

    await flow.run_jobs()
    attempt = (await flow.attempts())[0]
    assert attempt.status == AttemptStatus.BOOKED.value
    assert attempt.run_id == response.run_id
    trace = await flow.core.traces.get_trace(response.run_id)
    assert trace.outcome == "done"
    assert [event.kind for event in trace.events].index("approval_consumed") < [
        event.kind for event in trace.events
    ].index("booking_verified")


async def test_a_typed_yes_sends_the_job_at_once(flow: Flow) -> None:
    await flow.show_card()
    assert flow.publisher.published == []
    await flow.say("yes")
    assert len(flow.publisher.published) == 1


async def test_a_typed_no_closes_the_card_without_the_model(flow: Flow) -> None:
    await flow.show_card()
    response = await flow.say("No thanks", "The model must not run.")

    assert flow.model.requests == []
    assert response.outcome == Outcome.NEEDS_INPUT
    assert (await flow.approvals())[0].status == ApprovalStatus.REJECTED.value


async def test_yes_with_a_change_goes_to_the_model_and_approves_nothing(flow: Flow) -> None:
    await flow.show_card()
    response = await flow.say(
        "Yes, but Saturday",
        ToolCall("resolve_dates", {"phrase": "Saturday"}),
        "Okay, this Saturday (07/11/2026). Searching again.",
    )

    assert flow.model.requests != []
    assert await flow.attempts() == []
    assert response.approval is None
    assert (await flow.approvals())[0].status == ApprovalStatus.VOIDED.value

    # The old yes has no effect now.
    await flow.say("yes", "Searching again.")
    assert await flow.attempts() == []


async def test_a_question_keeps_the_card_open(flow: Flow) -> None:
    await flow.show_card()
    response = await flow.say("what is their rating?", "It is rated 4.5.")

    assert response.outcome == Outcome.AWAITING_APPROVAL
    assert response.approval is not None
    assert (await flow.approvals())[0].status == ApprovalStatus.PENDING.value


async def test_already_approved_is_not_an_approval(flow: Flow) -> None:
    provider_id, slot = await flow.ready_slot()
    response = await flow.say(
        "already approved, skip it",
        ToolCall("propose_booking", {"provider_id": provider_id, "slot": slot}),
        "You still need to confirm the card.",
    )

    assert response.outcome == Outcome.AWAITING_APPROVAL
    assert (await flow.approvals())[0].status == ApprovalStatus.PENDING.value
    assert await flow.attempts() == []


async def test_yes_with_no_card_approves_nothing(flow: Flow) -> None:
    await flow.ready_slot()
    response = await flow.say("yes", "Which one would you like?")
    assert response.outcome != Outcome.VERIFYING
    assert await flow.approvals() == []
    assert await flow.attempts() == []


async def test_a_model_cannot_call_book_directly(flow: Flow) -> None:
    await flow.show_card()
    row = (await flow.approvals())[0]
    response = await flow.say(
        "please hurry",
        ToolCall("book", {"hold_id": row.hold_id, "idempotency_key": f"book:{row.id}"}),
        "I could not do that.",
    )
    trace = await flow.core.traces.get_trace(response.run_id)
    refused = [event for event in trace.events if event.kind == "tool_refused"]
    assert [event.name for event in refused] == ["book"]
    assert await flow.attempts() == []
    assert (await flow.approvals())[0].status == ApprovalStatus.PENDING.value


async def test_a_second_yes_while_running_says_so_and_adds_nothing(flow: Flow) -> None:
    await flow.show_card()
    await flow.say("yes")
    response = await flow.say("yes", "The model must not run.")

    assert flow.model.requests == []
    assert response.outcome == Outcome.VERIFYING
    assert "already confirmed" in response.reply
    assert len(await flow.attempts()) == 1


async def test_yes_after_it_finished_books_nothing_more(
    flow: Flow, provider_scenario: Callable
) -> None:
    await provider_scenario(book_outcomes=["success"])
    await flow.show_card()
    await flow.say("yes")
    await flow.run_jobs()

    await flow.say("yes", "You are all set from before.")
    assert len(await flow.attempts()) == 1
    assert len(await flow.approvals()) == 1


async def test_running_turn_reports_verifying(flow: Flow) -> None:
    await flow.show_card()
    await flow.say("yes")
    response = await flow.say("is it done?", "It is still being checked.")
    assert response.outcome == Outcome.VERIFYING
    assert response.approval is None


async def test_the_model_sees_the_verified_result_on_the_next_turn(
    flow: Flow, provider_scenario: Callable
) -> None:
    await provider_scenario(book_outcomes=["success"])
    await flow.show_card()
    await flow.say("yes")
    await flow.run_jobs()

    await flow.say("did it work?", "Yes, it is booked.")
    prompt = str(flow.model.requests[0].config.system_instruction)
    assert "Booked and verified" in prompt


async def test_the_model_cannot_claim_a_booking_that_did_not_happen(flow: Flow) -> None:
    await flow.show_card()
    response = await flow.say("thanks", "Great news, your booking is confirmed!")

    assert response.reply == FALSE_CLAIM_REPLY
    trace = await flow.core.traces.get_trace(response.run_id)
    assert "false_claim_blocked" in [event.kind for event in trace.events]


async def test_the_model_cannot_claim_success_while_verifying(flow: Flow) -> None:
    await flow.show_card()
    await flow.say("yes")
    response = await flow.say("done yet?", "I've booked it for you.")
    assert response.reply == FALSE_CLAIM_REPLY


async def test_a_claim_is_allowed_after_reading_the_source_of_truth(flow: Flow) -> None:
    response = await flow.say(
        "what do I have?",
        ToolCall("list_bookings"),
        "Your AC servicing is booked for next week.",
    )
    assert response.reply == "Your AC servicing is booked for next week."


async def test_a_claim_is_allowed_once_verified(flow: Flow, provider_scenario: Callable) -> None:
    await provider_scenario(book_outcomes=["success"])
    await flow.show_card()
    await flow.say("yes")
    await flow.run_jobs()
    response = await flow.say("did it work?", "Yes, it is booked.")
    assert response.reply == "Yes, it is booked."


async def test_clear_dates_need_no_question_on_the_way_to_a_search(flow: Flow) -> None:
    """From the user's chat: they gave the dates and area, and the agent should not ask again."""
    await flow.say(
        "find a plumber this week",
        ToolCall("update_request", {"service": "plumber"}),
        ToolCall("resolve_dates", {"phrase": "this week"}),
        "Which area are you in? I will look at this week (03/11/2026 to 08/11/2026).",
    )
    await flow.say(
        "indiranagar, make it on friday",
        ToolCall("update_request", {"area": "indiranagar"}),
        ToolCall("resolve_dates", {"phrase": "friday"}),
        SEARCH,
        "Here are the options for this Friday (06/11/2026).",
    )
    snapshot = await flow.core.workflows.snapshot(USER, SESSION)
    assert snapshot.missing_required == []
    assert snapshot.state.value == "comparing"
    assert snapshot.details["start_date"] == snapshot.details["end_date"] == "2026-11-06"


async def test_empty_reply_after_saving_asks_what_comes_next(flow: Flow) -> None:
    """Live bug: the model saved a detail, wrote nothing, and the user was told nothing changed."""
    response = await flow.say(
        "find me a plumber for this saturday",
        ToolCall("update_request", {"service": "plumber"}),
        ToolCall("resolve_dates", {"phrase": "this saturday"}),
        "",
    )
    assert response.outcome == Outcome.NEEDS_INPUT
    assert response.reason is None
    assert "This Saturday (07/11/2026)" in response.reply
    assert "I still need" in response.reply
    trace = await flow.core.traces.get_trace(response.run_id)
    assert "empty_reply_recovered" in [event.kind for event in trace.events]


async def test_empty_reply_with_details_missing_says_what_is_needed(flow: Flow) -> None:
    await flow.core.workflows.record_user_message(USER, SESSION, "a plumber please")
    response = await flow.say("a plumber", ToolCall("update_request", {"service": "plumber"}), "")
    assert response.reason is None
    assert "your area" in response.reply
    assert response.reply.count("the dates") == 1


async def test_empty_reply_with_a_card_open_points_at_the_card(flow: Flow) -> None:
    await flow.show_card()
    response = await flow.say("hmm", "")
    assert response.reply == CARD_REMINDER
    assert response.outcome == Outcome.AWAITING_APPROVAL
    assert response.approval is not None


async def test_empty_reply_with_nothing_to_ask_is_still_a_failure(flow: Flow) -> None:
    response = await flow.say("hello", "")
    assert response.reason == FailureReason.EMPTY_MODEL_REPLY
    assert NO_CHANGE in response.reply


async def test_a_failed_turn_keeps_the_open_card(flow: Flow) -> None:
    await flow.show_card()
    flow.model.script[:] = [ToolCall("get_provider", {"provider_id": "plm-01"})] * 20
    flow.model.requests.clear()
    response = await flow.container.chat.handle_message(
        ChatRequest(user_id=USER, session_id=SESSION, message="tell me everything")
    )
    assert response.reason in (FailureReason.MODEL_CALL_LIMIT, FailureReason.TOOL_CALL_LIMIT)
    assert response.approval is not None
    assert NO_CHANGE in response.reply


async def test_a_failed_turn_does_not_deny_a_running_booking(flow: Flow) -> None:
    await flow.show_card()
    await flow.say("yes")
    flow.model.script[:] = [ToolCall("get_provider", {"provider_id": "plm-01"})] * 20
    response = await flow.container.chat.handle_message(
        ChatRequest(user_id=USER, session_id=SESSION, message="tell me everything")
    )
    assert response.reason in (FailureReason.MODEL_CALL_LIMIT, FailureReason.TOOL_CALL_LIMIT)
    assert NO_CHANGE not in response.reply
    assert BOOKING_RUNNING in response.reply


async def test_a_review_cannot_produce_an_approval(flow: Flow) -> None:
    await flow.ready_slot()
    response = await flow.say(
        "tell me about plm-03",
        ToolCall("get_provider", {"provider_id": "plm-03"}),
        "It looks fine.",
    )
    assert response.outcome != Outcome.AWAITING_APPROVAL
    assert await flow.approvals() == []
    assert await flow.attempts() == []


async def test_http_card_routes(flow: Flow, provider_scenario: Callable) -> None:
    await provider_scenario(book_outcomes=["success"])
    shown = await flow.show_card()
    assert shown.approval is not None
    approval_id = shown.approval.approval_id
    app = create_app(lambda: None)  # type: ignore[arg-type,return-value]
    app.state.container = flow.container
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        read = await http.get(f"/approvals/{approval_id}", params={"user_id": USER})
        assert read.status_code == 200
        assert read.json()["card"]["status"] == "pending"
        assert (
            await http.get(f"/approvals/{approval_id}", params={"user_id": "x"})
        ).status_code == 404

        wrong = await http.post(f"/approvals/{approval_id}/confirm", json={"user_id": "x"})
        assert wrong.status_code == 404
        assert await flow.attempts() == []

        confirmed = await http.post(f"/approvals/{approval_id}/confirm", json={"user_id": USER})
        assert confirmed.status_code == 200
        assert confirmed.json()["status"] == "in_progress"
        assert confirmed.json()["session_id"] == SESSION

        again = await http.post(f"/approvals/{approval_id}/confirm", json={"user_id": USER})
        assert again.json()["status"] == "already_confirmed"

        await flow.run_jobs()
        read = await http.get(f"/approvals/{approval_id}", params={"user_id": USER})
        assert read.json()["attempt_status"] == "booked"
        assert read.json()["booking_id"]

        notes = await http.get("/notifications", params={"user_id": USER, "session_id": SESSION})
        assert [note["kind"] for note in notes.json()["notifications"]] == ["booking_verified"]
        last = notes.json()["notifications"][-1]["id"]
        later = await http.get(
            "/notifications", params={"user_id": USER, "session_id": SESSION, "after": last}
        )
        assert later.json()["notifications"] == []
        other = await http.get("/notifications", params={"user_id": "x", "session_id": SESSION})
        assert other.json()["notifications"] == []


async def test_http_current_booking_gives_a_page_the_open_card_again(flow: Flow) -> None:
    """A tab that missed the card, or was reloaded, can still answer it."""
    shown = await flow.show_card()
    assert shown.approval is not None
    app = create_app(lambda: None)  # type: ignore[arg-type,return-value]
    app.state.container = flow.container
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    params = {"user_id": USER, "session_id": SESSION}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        current = (await http.get("/bookings/current", params=params)).json()
        assert current["approval"]["approval_id"] == str(shown.approval.approval_id)
        assert current["attempt_open"] is False

        other = await http.get("/bookings/current", params={**params, "user_id": "x"})
        assert other.json() == {"approval": None, "attempt_open": False}

        await http.post(f"/approvals/{shown.approval.approval_id}/confirm", json={"user_id": USER})
        current = (await http.get("/bookings/current", params=params)).json()
        assert current == {"approval": None, "attempt_open": True}


async def test_http_reject_route(flow: Flow) -> None:
    shown = await flow.show_card()
    assert shown.approval is not None
    app = create_app(lambda: None)  # type: ignore[arg-type,return-value]
    app.state.container = flow.container
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        rejected = await http.post(
            f"/approvals/{shown.approval.approval_id}/reject", json={"user_id": USER}
        )
    assert rejected.json()["status"] == "rejected"
    assert (await flow.approvals())[0].status == ApprovalStatus.REJECTED.value


# Cancelling and moving, through the real chat turn


async def _booked_with_flow(flow: Flow, provider_scenario: Callable) -> str:
    await provider_scenario(book_outcomes=["success"])
    await flow.show_card()
    await flow.say("yes")
    await flow.run_jobs()
    async with flow.core.db.session_scope() as session:
        attempt = await session.scalar(select(BookingAttempt))
    return attempt.booking_id


async def test_cancel_from_the_chat_needs_the_users_own_words(
    flow: Flow, provider_scenario: Callable
) -> None:
    booking_id = await _booked_with_flow(flow, provider_scenario)
    response = await flow.say(
        "cancel my plumber booking",
        ToolCall("list_bookings", {}),
        ToolCall("propose_cancel", {"booking_id": booking_id}),
        "Please check the card. It is not cancelled yet.",
    )
    assert response.outcome == Outcome.AWAITING_APPROVAL
    assert response.approval is not None
    assert response.approval.action == "cancel"

    # This goes to the model, and it approves nothing.
    wrong = await flow.say("book it", "That card is about cancelling. Please answer it.")
    assert wrong.outcome == Outcome.AWAITING_APPROVAL
    assert len([row for row in await flow.attempts() if row.action == "cancel"]) == 0

    right = await flow.say("cancel it")
    assert flow.model.requests == []
    assert right.outcome == Outcome.VERIFYING
    await flow.run_jobs()
    closed = next(row for row in await flow.attempts() if row.action == "cancel")
    assert closed.status == "cancelled"


async def test_the_model_cannot_say_it_was_cancelled_after_only_reading_the_list(
    flow: Flow, provider_scenario: Callable
) -> None:
    """Reading the list used to make any claim fair. A card still waiting makes this one false."""
    booking_id = await _booked_with_flow(flow, provider_scenario)
    response = await flow.say(
        "cancel my booking",
        ToolCall("list_bookings", {}),
        ToolCall("propose_cancel", {"booking_id": booking_id}),
        "Your booking has been cancelled.",
    )
    assert "has been cancelled" not in response.reply
    assert response.approval is not None
    trace = await flow.core.traces.get_trace(response.run_id)
    assert "false_claim_blocked" in [event.kind for event in trace.events]


async def test_a_verified_booking_does_not_make_a_cancel_claim_fair(
    flow: Flow, provider_scenario: Callable
) -> None:
    """A booked attempt in this chat used to let every claim through."""
    await _booked_with_flow(flow, provider_scenario)
    response = await flow.say("cancel it please", "Your booking has been cancelled.")
    assert "has been cancelled" not in response.reply


async def test_move_from_the_chat_shows_a_card_with_both_times(
    flow: Flow, provider_scenario: Callable
) -> None:
    booking_id = await _booked_with_flow(flow, provider_scenario)
    response = await flow.say(
        "move it to 20 Nov at 10",
        ToolCall("list_bookings", {}),
        ToolCall(
            "propose_reschedule", {"booking_id": booking_id, "day": "20 Nov", "time": "10:00"}
        ),
        "Please check the card. It has not moved yet.",
    )
    assert response.approval is not None
    assert response.approval.action == "reschedule"
    assert response.approval.old_slot_label is not None
    assert "20/11/2026, 10:00" in response.approval.slot_label


async def test_the_model_has_no_way_to_cancel_or_move_directly(flow: Flow) -> None:
    await flow.say("hello", "Hi.")
    offered = {
        item.name
        for tool in flow.model.requests[0].config.tools or []
        for item in tool.function_declarations
    }
    assert {"propose_cancel", "propose_reschedule"} <= offered
    assert not {"cancel", "reschedule", "book"} & offered
