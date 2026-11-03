"""Approvals and the booking saga, against the real mock provider, gateway and verifier."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import select, update

from app.container import Core, build_core
from app.core.config import Settings
from app.core.database import Database
from app.domains.bookings.matching import Decision, claims_side_effect, read_decision
from app.domains.bookings.models import Approval, ApprovalStatus, AttemptStatus, BookingAttempt
from app.domains.bookings.repository import BookingRepository
from app.domains.bookings.saga import MAX_BOOK_CALLS
from app.domains.bookings.schemas import ApprovalCard, DecisionStatus
from app.domains.bookings.service import ApprovalNotFound
from app.domains.bookings.verification import token_for
from app.domains.gateway.schemas import Caller, RefusalReason, ToolStatus
from app.domains.gateway.service import CallContext, ToolBudget
from app.domains.tracing.service import RunRecorder
from app.domains.workflow.models import WorkflowState as S
from tests.conftest import FakeClock, RecordingPublisher, make_ready

USER = "u1"
SESSION = "s1"


class CountingTransport(httpx.AsyncBaseTransport):
    """The real provider, with a record of calls and a switch to make a tool unreachable."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.calls: list[str] = []
        self.down: set[str] = set()
        self.hang: set[str] = set()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        tool = request.url.path.rsplit("/", 1)[-1]
        self.calls.append(tool)
        if tool in self.down:
            raise httpx.ConnectError("provider is down")
        if tool in self.hang:
            await asyncio.sleep(30)
        return await self.inner.handle_async_request(request)


@dataclass
class Stack:
    core: Core
    db: Database
    clock: FakeClock
    publisher: RecordingPublisher
    transport: CountingTransport
    provider: httpx.AsyncClient
    jobs_run: int = 0

    async def scenario(self, **config: Any) -> None:
        response = await self.provider.post("/admin/scenario", json=config)
        assert response.status_code == 204, response.text

    async def advance_everything(self, seconds: float) -> None:
        self.clock.advance(seconds)

    async def recorder(self, session: str = SESSION) -> RunRecorder:
        return await self.core.traces.start_run(USER, session)

    def context(self, recorder: RunRecorder | None = None) -> CallContext:
        return CallContext(USER, Caller.MODEL, ToolBudget(12), recorder)

    async def searched(self, session: str = SESSION, **extra: Any) -> list[dict]:
        await make_ready(self.core.workflows, USER, session, **extra)
        recorder = await self.recorder(session)
        args = await self.core.workflows.begin_search(USER, session, recorder)
        result = await self.core.gateway.call("search_providers", args, self.context(recorder))
        assert result.ok, result
        found = await self.core.workflows.finish_search(
            USER, session, result.data["providers"], recorder
        )
        return found["providers"]

    async def card(
        self, provider_suffix: str = "-01", session: str = SESSION, **extra: Any
    ) -> ApprovalCard:
        providers = await self.searched(session, **extra)
        provider = next(item for item in providers if item["id"].endswith(provider_suffix))
        return await self.propose(provider["id"], provider["slots"][0], session)

    async def propose(self, provider_id: str, slot: str, session: str = SESSION) -> ApprovalCard:
        recorder = await self.recorder(session)
        result = await self.core.approvals.propose_booking(
            session, provider_id, slot, self.context(recorder)
        )
        assert result.get("card_shown_to_user"), result
        view = await self.core.approvals.view(USER, session)
        assert view.card is not None
        return view.card

    async def yes(self, card: ApprovalCard) -> Any:
        return await self.core.approvals.confirm_click(card.approval_id, USER)

    async def run_jobs(self) -> None:
        """What the worker does: run every job that has been sent, once."""
        await self.core.jobs.publish_pending()
        while self.jobs_run < len(self.publisher.published):
            self.jobs_run += 1
            await self.core.jobs.execute(self.publisher.published[self.jobs_run - 1])

    async def book(self, **scenario: Any) -> ApprovalCard:
        await self.scenario(**scenario)
        card = await self.card()
        await self.yes(card)
        await self.run_jobs()
        return card

    async def approval(self, approval_id: uuid.UUID) -> Approval:
        async with self.db.session_scope() as session:
            row = await session.get(Approval, approval_id)
        assert row is not None
        return row

    async def attempts(self) -> list[BookingAttempt]:
        async with self.db.session_scope() as session:
            return list(await session.scalars(select(BookingAttempt)))

    async def approvals(self) -> list[Approval]:
        async with self.db.session_scope() as session:
            return list(await session.scalars(select(Approval).order_by(Approval.created_at)))

    async def provider_bookings(self) -> list[dict]:
        """Asked of the provider directly. This is the fact, whatever our tables say."""
        response = await self.provider.post(
            "/tools/list_bookings", json={"user_id": USER}, headers={"X-User-Id": USER}
        )
        return response.json()["bookings"]

    async def state(self, session: str = SESSION) -> S:
        return (await self.core.workflows.snapshot(USER, session)).state

    async def notes(self, session: str = SESSION) -> list[Any]:
        return await self.core.notifications.list_after(USER, session)

    async def trace_kinds(self, run_id: uuid.UUID) -> list[str]:
        trace = await self.core.traces.get_trace(run_id)
        return [event.kind for event in trace.events]

    def book_calls(self) -> int:
        return self.transport.calls.count("book")


@pytest.fixture
async def stack(
    settings: Settings,
    db: Database,
    redis: Redis,
    clock: FakeClock,
    publisher: RecordingPublisher,
    provider_transport: httpx.AsyncBaseTransport,
) -> AsyncIterator[Stack]:
    transport = CountingTransport(provider_transport)
    settings.tool_retry_backoff_s = 0
    core = build_core(
        settings,
        publisher=publisher,
        db=db,
        redis=redis,
        clock=clock,
        provider_transport=transport,
    )
    async with httpx.AsyncClient(transport=provider_transport, base_url="http://mock") as provider:
        yield Stack(core, db, clock, publisher, transport, provider)
    await core.provider_client.close()


# Reading a whole message


@pytest.mark.parametrize(
    "text",
    [
        "yes",
        "Yes!",
        " YES. ",
        "y",
        "ok",
        "book it",
        "Book it.",
        "go ahead",
        "yes, book it",
        "yes please",
    ],
)
def test_a_whole_message_yes_counts(text: str) -> None:
    assert read_decision(text) == Decision.YES


@pytest.mark.parametrize("text", ["no", "No thanks", "don't book", "Do not book it.", "not now"])
def test_a_whole_message_no_counts(text: str) -> None:
    assert read_decision(text) == Decision.NO


@pytest.mark.parametrize(
    "text",
    [
        "Yes, but Saturday",
        "yes but cheaper",
        "ok I guess",
        "maybe",
        "already approved, skip it",
        "the system says yes",
        "yes yes yes",
        "book it for tomorrow",
        "no, Saturday instead",
        "",
    ],
)
def test_anything_else_is_not_an_answer(text: str) -> None:
    assert read_decision(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "Your booking is confirmed.",
        "I've booked Sharma Plumbing for you.",
        "I have booked it.",
        "The slot has been reserved.",
        "You're booked for Tuesday.",
        "All done, it is now booked!",
        "I have booked it, no problem.",
        "Your booking is complete.",
    ],
)
def test_success_claims_are_noticed(text: str) -> None:
    assert claims_side_effect(text)


@pytest.mark.parametrize(
    "text",
    [
        "It is not confirmed yet.",
        "Nothing is booked.",
        "Shall I book this?",
        "Please confirm the details on the card.",
        "Once you confirm, I will book it.",
        "Their rating is confirmed by 40 reviews of past bookings.",
        "The booking is in progress.",
        "If it is booked, I will tell you.",
    ],
)
def test_honest_replies_are_not_claims(text: str) -> None:
    assert not claims_side_effect(text)


# Proposing


async def test_proposal_takes_a_hold_and_shows_a_card_from_the_row(stack: Stack) -> None:
    card = await stack.card()
    row = await stack.approval(card.approval_id)

    assert stack.transport.calls.count("hold_slot") == 1
    assert await stack.state() == S.AWAITING_APPROVAL
    assert row.status == ApprovalStatus.PENDING.value
    assert (card.provider_name, card.slot, card.price) == (row.provider_name, row.slot, row.price)
    assert card.notes == []
    assert len(row.approval_hash) == 64
    assert stack.book_calls() == 0
    assert await stack.provider_bookings() == await stack.provider_bookings()


async def test_card_says_when_the_quote_differs_from_the_search_price(stack: Stack) -> None:
    card = await stack.card("-02")
    row = await stack.approval(card.approval_id)
    assert row.price > row.list_price
    assert any("higher than the search price" in note for note in card.notes)


async def test_card_says_when_the_quote_breaks_the_budget(stack: Stack) -> None:
    providers = await stack.searched()
    provider = next(item for item in providers if item["id"].endswith("-02"))
    budget = provider["price"]
    # Within budget at search time, over it once the hold quotes more.
    await stack.core.workflows.record_user_message(USER, SESSION, f"under {budget}")
    await stack.core.workflows.update_request(USER, SESSION, {"max_budget": budget})
    recorder = await stack.recorder()
    args = await stack.core.workflows.begin_search(USER, SESSION, recorder)
    result = await stack.core.gateway.call("search_providers", args, stack.context(recorder))
    await stack.core.workflows.finish_search(USER, SESSION, result.data["providers"], recorder)

    card = await stack.propose(provider["id"], provider["slots"][0])
    assert any("over your budget" in note for note in card.notes)


async def test_card_holds_no_review_text(stack: Stack) -> None:
    card = await stack.card("-03")
    details = await stack.provider.post(
        "/tools/get_provider",
        json={"provider_id": (await stack.approval(card.approval_id)).provider_id},
        headers={"X-User-Id": USER},
    )
    reviews = [review["text"] for review in details.json()["reviews"]]
    shown = card.model_dump_json()
    assert reviews
    assert not any(review in shown for review in reviews)


@pytest.mark.parametrize(
    ("provider_id", "slot", "error"),
    [
        ("plm-99", "2026-11-03T09:00:00+05:30", "provider_not_in_results"),
        (None, "2030-01-01T09:00:00+05:30", "slot_not_in_results"),
        (None, "whenever", "slot_not_in_results"),
    ],
)
async def test_model_can_only_propose_what_the_search_returned(
    stack: Stack, provider_id: str | None, slot: str, error: str
) -> None:
    providers = await stack.searched()
    result = await stack.core.approvals.propose_booking(
        SESSION, provider_id or providers[0]["id"], slot, stack.context()
    )
    assert result["error"] == error
    assert "hold_slot" not in stack.transport.calls
    assert await stack.approvals() == []


async def test_a_provider_outside_the_budget_cannot_be_proposed(stack: Stack) -> None:
    await make_ready(stack.core.workflows, USER, SESSION, max_budget=1)
    recorder = await stack.recorder()
    args = await stack.core.workflows.begin_search(USER, SESSION, recorder)
    result = await stack.core.gateway.call("search_providers", args, stack.context(recorder))
    raw = result.data["providers"][0]
    await stack.core.workflows.finish_search(USER, SESSION, result.data["providers"], recorder)

    refused = await stack.core.approvals.propose_booking(
        SESSION, raw["id"], raw["available_slots"][0], stack.context()
    )
    assert refused["error"] == "provider_not_in_results"
    assert "hold_slot" not in stack.transport.calls


async def test_proposing_before_a_search_is_refused(stack: Stack) -> None:
    await make_ready(stack.core.workflows, USER, SESSION)
    result = await stack.core.approvals.propose_booking(
        SESSION, "plm-01", "2026-11-03T09:00:00+05:30", stack.context()
    )
    assert result["error"] == "search_first"
    assert "hold_slot" not in stack.transport.calls


async def test_proposing_the_same_slot_twice_keeps_one_card_and_one_hold(stack: Stack) -> None:
    card = await stack.card()
    row = await stack.approval(card.approval_id)
    again = await stack.propose(row.provider_id, row.slot)

    assert again.approval_id == card.approval_id
    assert stack.transport.calls.count("hold_slot") == 1
    assert len(await stack.approvals()) == 1


async def test_a_new_slot_voids_the_old_card_and_the_old_yes_does_nothing(stack: Stack) -> None:
    providers = await stack.searched()
    provider = next(item for item in providers if item["id"].endswith("-01"))
    first = await stack.propose(provider["id"], provider["slots"][0])
    second = await stack.propose(provider["id"], provider["slots"][1])

    assert (await stack.approval(first.approval_id)).status == ApprovalStatus.VOIDED.value
    decision = await stack.yes(first)
    assert decision.status == DecisionStatus.NO_LONGER_VALID
    assert await stack.attempts() == []

    assert (await stack.yes(second)).status == DecisionStatus.IN_PROGRESS


async def test_hold_failure_is_told_to_the_model_with_no_card(stack: Stack) -> None:
    providers = await stack.searched()
    stack.transport.down.add("hold_slot")
    result = await stack.core.approvals.propose_booking(
        SESSION, providers[0]["id"], providers[0]["slots"][0], stack.context()
    )
    assert result["error"] == "provider_unavailable"
    assert await stack.approvals() == []
    assert await stack.state() == S.COMPARING


# Approving


async def test_yes_commits_the_approval_attempt_and_job_together(stack: Stack) -> None:
    card = await stack.card()
    decision = await stack.yes(card)

    assert decision.status == DecisionStatus.IN_PROGRESS
    assert decision.outcome == "verifying"
    assert "not confirmed yet" in decision.reply
    assert (await stack.approval(card.approval_id)).status == ApprovalStatus.CONSUMED.value
    attempts = await stack.attempts()
    assert len(attempts) == 1
    assert attempts[0].idempotency_key == f"book:{card.approval_id}"
    assert attempts[0].status == AttemptStatus.OPEN.value
    assert await stack.state() == S.EXECUTING
    assert len(stack.publisher.published) == 1
    assert stack.book_calls() == 0


async def test_if_the_job_cannot_be_written_the_approval_stays_open(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    card = await stack.card()

    async def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("outbox write failed")

    monkeypatch.setattr(stack.core.jobs, "enqueue_in", broken)
    with pytest.raises(RuntimeError):
        await stack.yes(card)
    monkeypatch.undo()

    assert (await stack.approval(card.approval_id)).status == ApprovalStatus.PENDING.value
    assert await stack.attempts() == []
    assert await stack.state() == S.AWAITING_APPROVAL
    assert (await stack.yes(card)).status == DecisionStatus.IN_PROGRESS


async def test_redis_down_at_approval_keeps_the_job_for_the_sweep(stack: Stack) -> None:
    card = await stack.card()
    stack.publisher.broken = True
    decision = await stack.yes(card)
    assert decision.status == DecisionStatus.IN_PROGRESS
    assert stack.publisher.published == []

    stack.publisher.broken = False
    await stack.core.jobs.sweep()
    await stack.run_jobs()
    assert (await stack.attempts())[0].status != AttemptStatus.OPEN.value


async def test_repeated_confirmation_makes_one_attempt_and_one_booking(stack: Stack) -> None:
    await stack.scenario(book_outcomes=["success"])
    card = await stack.card()
    answers = [await stack.yes(card) for _ in range(5)]

    assert [answer.status for answer in answers] == [DecisionStatus.IN_PROGRESS] + [
        DecisionStatus.ALREADY_CONFIRMED
    ] * 4
    assert len(await stack.attempts()) == 1
    await stack.run_jobs()
    assert stack.book_calls() == 1
    assert len(await stack.provider_bookings()) == 1


async def test_someone_elses_approval_does_not_exist(stack: Stack) -> None:
    card = await stack.card()
    with pytest.raises(ApprovalNotFound):
        await stack.core.approvals.confirm_click(card.approval_id, "intruder")
    with pytest.raises(ApprovalNotFound):
        await stack.core.approvals.reject_click(card.approval_id, "intruder")
    with pytest.raises(ApprovalNotFound):
        await stack.core.approvals.read(card.approval_id, "intruder")
    assert (await stack.approval(card.approval_id)).status == ApprovalStatus.PENDING.value


async def test_no_closes_the_card_and_keeps_the_errand(stack: Stack) -> None:
    card = await stack.card()
    decision = await stack.core.approvals.reject_click(card.approval_id, USER)

    assert decision.status == DecisionStatus.REJECTED
    assert (await stack.approval(card.approval_id)).status == ApprovalStatus.REJECTED.value
    assert await stack.state() == S.COMPARING
    assert (await stack.yes(card)).status == DecisionStatus.NO_LONGER_VALID
    assert await stack.attempts() == []


async def test_the_user_can_pick_the_same_slot_again_after_saying_no(stack: Stack) -> None:
    card = await stack.card()
    row = await stack.approval(card.approval_id)
    await stack.core.approvals.reject_click(card.approval_id, USER)

    fresh = await stack.propose(row.provider_id, row.slot)
    assert fresh.approval_id != card.approval_id
    assert (await stack.approval(fresh.approval_id)).hold_id != row.hold_id


async def test_changing_a_detail_voids_the_open_card(stack: Stack) -> None:
    card = await stack.card()
    await stack.core.workflows.record_user_message(USER, SESSION, "under 900 please")
    await stack.core.workflows.update_request(USER, SESSION, {"max_budget": 900})

    assert await stack.state() == S.INTAKE
    decision = await stack.yes(card)
    assert decision.status == DecisionStatus.NO_LONGER_VALID
    assert (await stack.approval(card.approval_id)).status == ApprovalStatus.VOIDED.value
    assert await stack.attempts() == []


async def test_changing_the_dates_voids_the_open_card(stack: Stack) -> None:
    card = await stack.card()
    await stack.core.workflows.record_user_message(USER, SESSION, "actually Saturday")
    await stack.core.workflows.resolve_dates(USER, SESSION, "Saturday")

    view = await stack.core.approvals.view(USER, SESSION)
    assert view.card is None
    assert (await stack.approval(card.approval_id)).status == ApprovalStatus.VOIDED.value


async def test_a_card_that_ran_out_gets_a_new_hold_and_needs_a_new_yes(stack: Stack) -> None:
    card = await stack.card()
    old = await stack.approval(card.approval_id)
    stack.clock.advance(601)

    decision = await stack.yes(card)
    assert decision.status == DecisionStatus.NEW_CARD
    assert decision.approval is not None
    assert decision.approval.approval_id != card.approval_id
    assert (await stack.approval(card.approval_id)).status == ApprovalStatus.EXPIRED.value
    assert (await stack.approval(decision.approval.approval_id)).hold_id != old.hold_id
    assert await stack.attempts() == []
    assert await stack.state() == S.AWAITING_APPROVAL


async def test_a_card_that_ran_out_and_lost_its_slot_says_so(stack: Stack) -> None:
    card = await stack.card()
    stack.clock.advance(601)
    stack.transport.down.add("hold_slot")

    decision = await stack.yes(card)
    assert decision.status == DecisionStatus.SLOT_GONE
    assert decision.approval is None
    assert await stack.state() == S.COMPARING


async def test_an_ignored_card_closes_when_it_runs_out(stack: Stack) -> None:
    card = await stack.card()
    stack.clock.advance(601)
    view = await stack.core.approvals.view(USER, SESSION)
    assert view.card is None
    assert (await stack.approval(card.approval_id)).status == ApprovalStatus.EXPIRED.value
    assert await stack.state() == S.COMPARING


# The gateway's check


async def _saga_book(stack: Stack, card: ApprovalCard, token: str | None) -> Any:
    row = await stack.approval(card.approval_id)
    return await stack.core.gateway.call(
        "book",
        {"hold_id": row.hold_id, "idempotency_key": f"book:{card.approval_id}"},
        CallContext(USER, Caller.SAGA, ToolBudget(5), approval_token=token),
    )


@pytest.mark.parametrize(
    "tamper",
    [
        {"price": 1},
        {"list_price": 1},
        {"slot": "2030-01-01T09:00:00+05:30"},
        {"provider_id": "plm-99"},
        {"provider_name": "Someone Else"},
        {"hold_id": "hold-other"},
    ],
    ids=lambda change: next(iter(change)),
)
async def test_any_edit_to_an_approved_row_is_refused(stack: Stack, tamper: dict) -> None:
    card = await stack.card()
    await stack.yes(card)
    row = await stack.approval(card.approval_id)
    token = token_for(row, row.list_price)
    async with stack.db.session_scope() as session:
        await session.execute(update(Approval).where(Approval.id == row.id).values(**tamper))

    if "hold_id" in tamper:
        args = {"hold_id": tamper["hold_id"], "idempotency_key": f"book:{card.approval_id}"}
    else:
        args = {"hold_id": row.hold_id, "idempotency_key": f"book:{card.approval_id}"}
    result = await stack.core.gateway.call(
        "book", args, CallContext(USER, Caller.SAGA, ToolBudget(5), approval_token=token)
    )
    assert result.error_code == RefusalReason.APPROVAL_REQUIRED.value
    assert stack.book_calls() == 0


async def test_the_gateway_refuses_a_price_that_moved(stack: Stack) -> None:
    card = await stack.card()
    await stack.yes(card)
    row = await stack.approval(card.approval_id)
    result = await _saga_book(stack, card, token_for(row, row.list_price + 100))
    assert result.status == ToolStatus.REFUSED
    assert stack.book_calls() == 0


async def test_the_real_token_is_accepted(stack: Stack) -> None:
    await stack.scenario(book_outcomes=["success"])
    card = await stack.card()
    await stack.yes(card)
    row = await stack.approval(card.approval_id)
    result = await _saga_book(stack, card, token_for(row, row.list_price))
    assert result.ok
    assert stack.book_calls() == 1


@pytest.mark.parametrize("token", [None, "", "garbage", f"{uuid.uuid4()}:abc"])
async def test_a_missing_or_made_up_token_is_refused(stack: Stack, token: str | None) -> None:
    card = await stack.card()
    await stack.yes(card)
    result = await _saga_book(stack, card, token)
    assert result.error_code == RefusalReason.APPROVAL_REQUIRED.value
    assert stack.book_calls() == 0


async def test_an_approval_nobody_answered_cannot_book(stack: Stack) -> None:
    card = await stack.card()
    row = await stack.approval(card.approval_id)
    result = await _saga_book(stack, card, token_for(row, row.list_price))
    assert result.error_code == RefusalReason.APPROVAL_REQUIRED.value
    assert stack.book_calls() == 0


async def test_the_token_cannot_be_used_with_another_key(stack: Stack) -> None:
    card = await stack.card()
    await stack.yes(card)
    row = await stack.approval(card.approval_id)
    result = await stack.core.gateway.call(
        "book",
        {"hold_id": row.hold_id, "idempotency_key": "book:another"},
        CallContext(
            USER, Caller.SAGA, ToolBudget(5), approval_token=token_for(row, row.list_price)
        ),
    )
    assert result.error_code == RefusalReason.APPROVAL_REQUIRED.value


async def test_the_token_cannot_be_used_with_another_hold(stack: Stack) -> None:
    card = await stack.card()
    await stack.yes(card)
    row = await stack.approval(card.approval_id)
    result = await stack.core.gateway.call(
        "book",
        {"hold_id": "hold-other", "idempotency_key": f"book:{card.approval_id}"},
        CallContext(
            USER, Caller.SAGA, ToolBudget(5), approval_token=token_for(row, row.list_price)
        ),
    )
    assert result.error_code == RefusalReason.APPROVAL_REQUIRED.value
    assert stack.book_calls() == 0


async def test_an_attempt_whose_approval_is_not_consumed_cannot_book(stack: Stack) -> None:
    card = await stack.card()
    await stack.yes(card)
    row = await stack.approval(card.approval_id)
    async with stack.db.session_scope() as session:
        await session.execute(
            update(Approval)
            .where(Approval.id == row.id)
            .values(status=ApprovalStatus.REJECTED.value)
        )
    result = await _saga_book(stack, card, token_for(row, row.list_price))
    assert result.error_code == RefusalReason.APPROVAL_REQUIRED.value
    assert stack.book_calls() == 0


async def test_consuming_is_conditional_on_pending_and_on_time(stack: Stack) -> None:
    """The guard on its own, without the checks in front of it that also catch these cases."""
    card = await stack.card()
    async with stack.db.session_scope() as session:
        repository = BookingRepository(session)
        late = card.expires_at + timedelta(seconds=1)
        assert not await repository.consume(card.approval_id, late)
        assert await repository.consume(card.approval_id, stack.clock.now())
        assert not await repository.consume(card.approval_id, stack.clock.now())


async def test_the_token_cannot_be_used_by_another_user(stack: Stack) -> None:
    card = await stack.card()
    await stack.yes(card)
    row = await stack.approval(card.approval_id)
    result = await stack.core.gateway.call(
        "book",
        {"hold_id": row.hold_id, "idempotency_key": f"book:{card.approval_id}"},
        CallContext(
            "u2", Caller.SAGA, ToolBudget(5), approval_token=token_for(row, row.list_price)
        ),
    )
    assert result.error_code == RefusalReason.APPROVAL_REQUIRED.value


async def test_a_finished_attempt_cannot_book_again(stack: Stack) -> None:
    card = await stack.book(book_outcomes=["success"])
    row = await stack.approval(card.approval_id)
    calls = stack.book_calls()
    result = await _saga_book(stack, card, token_for(row, row.list_price))
    assert result.error_code == RefusalReason.APPROVAL_REQUIRED.value
    assert stack.book_calls() == calls


async def test_the_model_cannot_book_even_with_a_real_token(stack: Stack) -> None:
    card = await stack.card()
    await stack.yes(card)
    row = await stack.approval(card.approval_id)
    result = await stack.core.gateway.call(
        "book",
        {"hold_id": row.hold_id, "idempotency_key": f"book:{card.approval_id}"},
        CallContext(
            USER, Caller.MODEL, ToolBudget(5), approval_token=token_for(row, row.list_price)
        ),
    )
    assert result.error_code == RefusalReason.NOT_ALLOWED.value
    assert stack.book_calls() == 0


# The saga: each result in the blueprint's table


async def test_success_is_verified_and_then_reported(stack: Stack) -> None:
    card = await stack.book(book_outcomes=["success"])

    attempt = (await stack.attempts())[0]
    bookings = await stack.provider_bookings()
    assert attempt.status == AttemptStatus.BOOKED.value
    assert attempt.booking_id in {booking["booking_id"] for booking in bookings}
    assert stack.transport.calls.index("book") < stack.transport.calls.index("get_booking_status")
    # The errand is finished, so the session starts a new one.
    assert await stack.state() == S.INTAKE

    notes = await stack.notes()
    assert [note.kind for note in notes] == ["booking_verified"]
    assert attempt.booking_id in notes[0].text
    assert card.provider_name in notes[0].text


async def test_no_success_text_exists_before_the_status_check(stack: Stack) -> None:
    await stack.scenario(book_outcomes=["success"])
    card = await stack.card()
    await stack.yes(card)
    stack.transport.down.add("get_booking_status")
    await stack.run_jobs()

    assert stack.book_calls() == 1
    assert [note.kind for note in await stack.notes()] == ["booking_not_confirmed"]
    assert (await stack.attempts())[0].status == AttemptStatus.OPEN.value


async def test_timeout_after_booking_does_not_retry_and_reports_success(stack: Stack) -> None:
    await stack.book(book_outcomes=["timeout_booked"])

    assert stack.book_calls() == 1
    attempt = (await stack.attempts())[0]
    assert attempt.status == AttemptStatus.BOOKED.value
    assert attempt.last_result == "timeout"
    assert [note.kind for note in await stack.notes()] == ["booking_verified"]
    assert len(await stack.provider_bookings()) >= 1


async def test_the_trace_shows_the_check_between_the_timeout_and_the_success(stack: Stack) -> None:
    await stack.book(book_outcomes=["timeout_booked"])
    attempt = (await stack.attempts())[0]
    trace = await stack.core.traces.get_trace(attempt.run_id)
    steps = [
        (event.kind, event.name)
        for event in trace.events
        if event.kind in ("tool_call", "state_transition", "booking_verified")
    ]
    assert steps == [
        ("state_transition", "executing"),
        ("tool_call", "get_provider"),
        ("tool_call", "book"),
        ("state_transition", "verifying"),
        ("tool_call", "get_booking_status"),
        ("state_transition", "done"),
        ("booking_verified", None),
    ]
    assert trace.outcome == "done"
    book = next(event for event in trace.events if event.name == "book")
    assert book.data["status"] == "timeout"
    assert book.data["ambiguous"] is True


async def test_timeout_not_booked_retries_with_the_same_key_then_books(stack: Stack) -> None:
    await stack.book(book_outcomes=["timeout_not_booked", "success"])

    assert stack.book_calls() == 2
    assert (await stack.attempts())[0].status == AttemptStatus.BOOKED.value
    assert len({note.kind for note in await stack.notes()}) == 1


async def test_two_retries_at_most_then_a_clear_failure(stack: Stack) -> None:
    await stack.book(book_outcomes=["timeout_not_booked"] * 5)

    assert stack.book_calls() == MAX_BOOK_CALLS == 3
    attempt = (await stack.attempts())[0]
    assert attempt.status == AttemptStatus.FAILED.value
    notes = await stack.notes()
    assert [note.kind for note in notes] == ["booking_failed"]
    assert "no active booking" in notes[0].text
    assert await stack.state() == S.COMPARING
    assert len(await stack.attempts()) == 1


async def test_slot_taken_closes_it_and_offers_other_slots_with_a_fresh_yes(stack: Stack) -> None:
    card = await stack.book(book_outcomes=["slot_taken", "success"])

    assert stack.book_calls() == 1
    attempt = (await stack.attempts())[0]
    assert attempt.status == AttemptStatus.SLOT_TAKEN.value
    assert await stack.state() == S.COMPARING
    notes = await stack.notes()
    assert [note.kind for note in notes] == ["booking_slot_taken"]
    assert "nothing was booked" in notes[0].text
    assert "Other open slots" in notes[0].text

    assert (await stack.yes(card)).status == DecisionStatus.ALREADY_CONFIRMED
    assert stack.book_calls() == 1


async def test_hold_expired_takes_a_new_hold_and_asks_again(stack: Stack) -> None:
    await stack.scenario(book_outcomes=["success"])
    card = await stack.card()
    old = await stack.approval(card.approval_id)
    await stack.yes(card)
    # The provider's clock runs past the hold, as if the worker was slow to start.
    await stack.provider.post("/admin/clock/advance", json={"seconds": 601})
    await stack.run_jobs()

    attempt = (await stack.attempts())[0]
    assert attempt.status == AttemptStatus.HOLD_EXPIRED.value
    assert await stack.state() == S.AWAITING_APPROVAL
    approvals = await stack.approvals()
    assert len(approvals) == 2
    fresh = next(item for item in approvals if item.id != old.id)
    assert fresh.status == ApprovalStatus.PENDING.value
    assert fresh.hold_id != old.hold_id
    notes = await stack.notes()
    assert [note.kind for note in notes] == ["booking_hold_expired"]
    assert notes[0].data["approval"]["approval_id"] == str(fresh.id)
    # Same price, and it still needs its own yes.
    assert fresh.price == old.price
    assert (await stack.attempts())[0].approval_id == old.id


async def test_price_change_after_approval_books_nothing_and_shows_the_new_price(
    stack: Stack,
) -> None:
    await stack.scenario(book_outcomes=["success"])
    card = await stack.card()
    old = await stack.approval(card.approval_id)
    await stack.yes(card)
    await stack.scenario(
        provider_prices={old.provider_id: old.price + 500}, book_outcomes=["success"]
    )
    await stack.run_jobs()

    assert stack.book_calls() == 0
    attempt = (await stack.attempts())[0]
    assert attempt.status == AttemptStatus.PRICE_CHANGED.value
    assert attempt.book_calls == 0
    notes = await stack.notes()
    assert [note.kind for note in notes] == ["booking_price_changed"]
    card_data = notes[0].data["approval"]
    assert card_data["price"] == old.price + 500
    assert await stack.state() == S.AWAITING_APPROVAL

    # The new yes books at the new price, once.
    fresh = await stack.core.approvals.view(USER, SESSION)
    assert fresh.card is not None
    assert (await stack.yes(fresh.card)).status == DecisionStatus.IN_PROGRESS
    await stack.run_jobs()
    assert stack.book_calls() == 1
    booked = [item for item in await stack.provider_bookings() if item["price"] == old.price + 500]
    assert len(booked) == 1


async def test_an_edited_row_books_nothing_and_says_so(stack: Stack) -> None:
    card = await stack.card()
    await stack.yes(card)
    async with stack.db.session_scope() as session:
        await session.execute(
            update(Approval).where(Approval.id == card.approval_id).values(price=1)
        )
    await stack.run_jobs()

    assert stack.book_calls() == 0
    assert (await stack.attempts())[0].status == AttemptStatus.FAILED.value
    assert [note.kind for note in await stack.notes()] == ["booking_failed"]


async def test_status_unavailable_stays_open_and_says_so_once(stack: Stack) -> None:
    await stack.scenario(book_outcomes=["timeout_booked"])
    card = await stack.card()
    await stack.yes(card)
    stack.transport.down.add("get_booking_status")
    await stack.run_jobs()

    attempt = (await stack.attempts())[0]
    assert attempt.status == AttemptStatus.OPEN.value
    assert await stack.state() == S.VERIFYING
    assert [note.kind for note in await stack.notes()] == ["booking_not_confirmed"]
    assert "not confirmed yet" in (await stack.notes())[0].text

    # The sweep comes back. Still down: no second message and no second book.
    stack.clock.advance(61)
    await stack.core.jobs.sweep()
    await stack.run_jobs()
    assert stack.book_calls() == 1
    assert len(await stack.notes()) == 1

    # The provider returns, and the answer is reached without another book call.
    stack.transport.down.clear()
    stack.clock.advance(61)
    await stack.core.jobs.sweep()
    await stack.run_jobs()
    assert stack.book_calls() == 1
    assert (await stack.attempts())[0].status == AttemptStatus.BOOKED.value
    assert (await stack.notes())[-1].kind == "booking_verified"
    assert len(await stack.provider_bookings()) >= 1
    assert card.approval_id


async def test_a_booking_the_provider_already_dropped_is_not_reported_as_booked(
    stack: Stack,
) -> None:
    await stack.scenario(book_outcomes=["timeout_booked"])
    card = await stack.card()
    await stack.yes(card)
    stack.transport.down.add("get_booking_status")
    await stack.run_jobs()
    bookings = await stack.provider_bookings()
    dropped = next(item for item in bookings if item["provider_name"] == card.provider_name)
    await stack.provider.post("/admin/provider-cancel", json={"booking_id": dropped["booking_id"]})

    stack.transport.down.clear()
    stack.clock.advance(61)
    await stack.core.jobs.sweep()
    await stack.run_jobs()

    assert (await stack.attempts())[0].status == AttemptStatus.FAILED.value
    kinds = [note.kind for note in await stack.notes()]
    assert "booking_verified" not in kinds
    assert kinds[-1] == "booking_failed"
    # The status answer settles it, so `book` is not tried again.
    assert stack.book_calls() == 1


async def test_provider_read_failing_before_book_sends_no_book(stack: Stack) -> None:
    card = await stack.card()
    await stack.yes(card)
    stack.transport.down.add("get_provider")
    await stack.run_jobs()

    assert stack.book_calls() == 0
    assert (await stack.attempts())[0].status == AttemptStatus.OPEN.value
    assert (await stack.approval(card.approval_id)).status == ApprovalStatus.CONSUMED.value


# Restarts and repeats


async def test_a_worker_that_dies_after_book_is_resolved_by_status(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    await stack.scenario(book_outcomes=["success"])
    card = await stack.card()
    await stack.yes(card)

    async def die(*args: object, **kwargs: object) -> None:
        raise RuntimeError("worker killed")

    monkeypatch.setattr(stack.core.saga, "_note_result", die)
    await stack.run_jobs()
    monkeypatch.undo()
    assert stack.book_calls() == 1
    assert (await stack.attempts())[0].book_calls == 1
    assert (await stack.attempts())[0].status == AttemptStatus.OPEN.value

    await stack.core.saga.run((await stack.attempts())[0].id)
    assert stack.book_calls() == 1
    assert (await stack.attempts())[0].status == AttemptStatus.BOOKED.value
    assert card.approval_id


async def test_a_worker_that_dies_before_book_still_books_once(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    await stack.scenario(book_outcomes=["success"])
    card = await stack.card()
    await stack.yes(card)
    original = stack.core.saga._note_book_call

    async def die_after_commit(*args: object, **kwargs: object) -> bool:
        await original(*args, **kwargs)  # type: ignore[arg-type]
        raise RuntimeError("worker killed")

    monkeypatch.setattr(stack.core.saga, "_note_book_call", die_after_commit)
    await stack.run_jobs()
    monkeypatch.undo()
    assert stack.book_calls() == 0
    assert (await stack.attempts())[0].book_calls == 1

    await stack.core.saga.run((await stack.attempts())[0].id)
    assert stack.book_calls() == 1
    assert (await stack.attempts())[0].status == AttemptStatus.BOOKED.value
    assert card.approval_id


async def test_a_second_delivery_finds_the_attempt_claimed(stack: Stack) -> None:
    card = await stack.card()
    await stack.yes(card)
    attempt = (await stack.attempts())[0]
    async with stack.db.session_scope() as session:
        await session.execute(
            update(BookingAttempt)
            .where(BookingAttempt.id == attempt.id)
            .values(lease_until=stack.clock.now().replace(year=2030))
        )
    await stack.core.saga.run(attempt.id)
    assert stack.book_calls() == 0
    assert card.approval_id


async def test_a_finished_attempt_ignores_another_delivery(stack: Stack) -> None:
    await stack.book(book_outcomes=["success"])
    calls = stack.book_calls()
    await stack.core.saga.run((await stack.attempts())[0].id)
    assert stack.book_calls() == calls
    assert len(await stack.notes()) == 1


async def test_two_workers_at_once_book_once_and_report_once(stack: Stack) -> None:
    await stack.scenario(book_outcomes=["success"])
    card = await stack.card()
    await stack.yes(card)
    attempt = (await stack.attempts())[0]
    await asyncio.gather(stack.core.saga.run(attempt.id), stack.core.saga.run(attempt.id))

    bookings = await stack.provider_bookings()
    assert sum(item["provider_id"] == attempt.provider_id for item in bookings) >= 1
    assert len(await stack.notes()) == 1
    assert card.approval_id


async def test_a_slow_worker_arriving_after_the_answer_changes_nothing(stack: Stack) -> None:
    card = await stack.book(book_outcomes=["success"])
    attempt = (await stack.attempts())[0]
    row = await stack.approval(card.approval_id)
    recorder = await stack.core.traces.resume_run(attempt.run_id)

    await stack.core.saga._finish_booked(attempt, row, "bkg-other", recorder)
    await stack.core.saga._close_slot_taken(attempt, row, recorder)

    after = (await stack.attempts())[0]
    assert after.status == AttemptStatus.BOOKED.value
    assert after.booking_id == attempt.booking_id
    assert [note.kind for note in await stack.notes()] == ["booking_verified"]


async def test_a_fault_in_the_saga_tells_the_user_and_comes_back(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    await stack.scenario(book_outcomes=["success"])
    card = await stack.card()
    await stack.yes(card)

    async def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("a bug")

    monkeypatch.setattr(stack.core.saga, "_note_result", broken)
    await stack.run_jobs()
    monkeypatch.undo()
    assert [note.kind for note in await stack.notes()] == ["booking_not_confirmed"]

    await stack.core.jobs.sweep()
    await stack.run_jobs()
    assert (await stack.notes())[-1].kind == "booking_verified"
    assert len(await stack.notes()) == 2
    assert card.approval_id


async def test_a_worker_that_lost_its_lease_writes_nothing(stack: Stack) -> None:
    card = await stack.card()
    await stack.yes(card)
    attempt = (await stack.attempts())[0]
    async with stack.db.session_scope() as session:
        await session.execute(
            update(BookingAttempt).where(BookingAttempt.id == attempt.id).values(claims=7)
        )
    assert not await stack.core.saga._note_book_call(attempt, 1)
    assert card.approval_id


async def test_job_time_limit_during_book_stays_not_confirmed_then_resolves(
    stack: Stack, settings: Settings
) -> None:
    await stack.scenario(book_outcomes=["success"])
    card = await stack.card()
    await stack.yes(card)
    stack.core.jobs._time_limit_s = 0.05
    stack.transport.hang.add("book")
    await stack.run_jobs()

    attempt = (await stack.attempts())[0]
    assert attempt.status == AttemptStatus.OPEN.value
    assert attempt.book_calls == 1
    assert attempt.lease_until is None
    assert [note.kind for note in await stack.notes()] == ["booking_not_confirmed"]

    stack.transport.hang.clear()
    stack.core.jobs._time_limit_s = 45
    await stack.core.jobs.sweep()
    await stack.run_jobs()
    assert (await stack.attempts())[0].status == AttemptStatus.BOOKED.value
    assert card.approval_id


async def test_sweep_requeues_after_the_lease_and_not_before(stack: Stack) -> None:
    card = await stack.card()
    await stack.yes(card)
    published = len(stack.publisher.published)
    assert await stack.core.saga.recover() == 0

    stack.clock.advance(61)
    assert await stack.core.saga.recover() == 1
    assert await stack.core.saga.recover() == 0
    await stack.core.jobs.publish_pending()
    assert len(stack.publisher.published) == published + 1
    assert card.approval_id


async def test_sweep_leaves_a_live_lease_alone(stack: Stack) -> None:
    await stack.card()
    await stack.yes((await stack.core.approvals.view(USER, SESSION)).card)
    attempt = (await stack.attempts())[0]
    stack.clock.advance(61)
    async with stack.db.session_scope() as session:
        await session.execute(
            update(BookingAttempt)
            .where(BookingAttempt.id == attempt.id)
            .values(lease_until=stack.clock.now().replace(year=2030))
        )
    assert await stack.core.saga.recover() == 0


async def test_sweep_ignores_finished_attempts(stack: Stack) -> None:
    await stack.book(book_outcomes=["success"])
    stack.clock.advance(600)
    assert await stack.core.saga.recover() == 0


async def test_one_active_booking_per_user_provider_and_slot(stack: Stack) -> None:
    """Two chats reach the same slot: the database stops the second attempt."""
    providers = await stack.searched("a")
    await stack.searched("b")
    provider = next(item for item in providers if item["id"].endswith("-01"))
    first = await stack.propose(provider["id"], provider["slots"][0], session="a")
    second = await stack.propose(provider["id"], provider["slots"][0], session="b")
    await stack.yes(first)

    decision = await stack.yes(second)

    assert decision.status == DecisionStatus.NO_LONGER_VALID
    assert "already in progress" in decision.reply
    assert len(await stack.attempts()) == 1
    assert (await stack.approval(second.approval_id)).closed_reason == "duplicate_booking"


async def test_a_slot_being_booked_in_another_chat_cannot_be_proposed(stack: Stack) -> None:
    providers = await stack.searched("a")
    await stack.searched("b")
    provider = next(item for item in providers if item["id"].endswith("-01"))
    await stack.yes(await stack.propose(provider["id"], provider["slots"][0], session="a"))
    holds = stack.transport.calls.count("hold_slot")

    refused = await stack.core.approvals.propose_booking(
        "b", provider["id"], provider["slots"][0], stack.context()
    )
    assert refused["error"] == "booking_in_progress"
    assert stack.transport.calls.count("hold_slot") == holds


# What the model is told


async def test_prompt_note_says_not_confirmed_while_running(stack: Stack) -> None:
    card = await stack.card()
    note = await stack.core.approvals.prompt_note(USER, SESSION)
    assert card.provider_name in note
    assert "Nothing is booked" in note

    await stack.yes(card)
    note = await stack.core.approvals.prompt_note(USER, SESSION)
    assert "NOT confirmed yet" in note


async def test_prompt_note_carries_the_verified_result(stack: Stack) -> None:
    await stack.book(book_outcomes=["success"])
    note = await stack.core.approvals.prompt_note(USER, SESSION)
    assert "verified" in note
    assert "NOT confirmed" not in note


async def test_while_booking_the_model_cannot_change_the_request(stack: Stack) -> None:
    card = await stack.card()
    await stack.yes(card)
    workflows = stack.core.workflows
    assert (await workflows.update_request(USER, SESSION, {"area": "HSR"}))["error"] == (
        "booking_in_progress"
    )
    assert (await workflows.resolve_dates(USER, SESSION, "Saturday"))["error"] == (
        "booking_in_progress"
    )
    refused = await stack.core.approvals.propose_booking(
        SESSION, "plm-01", card.slot, stack.context()
    )
    assert refused["error"] == "booking_in_progress"
