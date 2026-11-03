"""Approvals: propose, show a card, and turn the user's yes into one committed attempt.

The card comes from the approval row only. A yes comes only from the raw user message or a click,
and it is read before the model runs. The model can ask, but it can never answer.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import LOCAL_TZ, Clock
from app.core.database import Database
from app.domains.bookings.matching import Decision, read_decision
from app.domains.bookings.models import (
    Approval,
    ApprovalStatus,
    AttemptStatus,
    BookingAction,
    BookingAttempt,
)
from app.domains.bookings.repository import BookingRepository
from app.domains.bookings.schemas import (
    ApprovalCard,
    ApprovalDecision,
    ApprovalRead,
    DecisionStatus,
)
from app.domains.bookings.verification import ApprovedFacts, expiry_text, idempotency_key
from app.domains.gateway.schemas import Caller, ToolResult
from app.domains.gateway.service import CallContext, ToolBudget, ToolGateway
from app.domains.jobs.service import JobService
from app.domains.notifications.service import NotificationService
from app.domains.tracing.service import RunRecorder, TraceService
from app.domains.workflow.models import WorkflowState
from app.domains.workflow.service import (
    ProposalTarget,
    WorkflowService,
    record_moves,
    same_time,
)

logger = logging.getLogger(__name__)

RUNNING_STATES = frozenset({WorkflowState.EXECUTING, WorkflowState.VERIFYING})
BOOK_JOB = "bookings.run_attempt"
# The provider's cancel and reschedule take no hold, so the card itself runs out after this.
CHANGE_CARD_LIFE = timedelta(minutes=10)
# What `list_bookings` calls a booking that can still be changed.
ACTIVE_BOOKING = "confirmed"
# A renewed hold may take one call. A few more would mean something is wrong.
RENEW_TOOL_BUDGET = 3

IN_PROGRESS_REPLY = (
    "Your booking is in progress. It is not confirmed yet. "
    "I will tell you as soon as the provider verifies it."
)
ALREADY_CONFIRMED_REPLY = (
    "You already confirmed this, so only one booking will be made. It is not confirmed yet."
)
NO_LONGER_VALID_REPLY = (
    "That confirmation is no longer valid, so nothing was booked. "
    "The details changed or a newer card replaced it."
)
REJECTED_REPLY = "Okay, I did not book it. Tell me if you want another provider or time."
NEW_CARD_REPLY = (
    "The hold on that slot ran out, so I took a new one. Nothing is booked yet. "
    "Please check the details on this new card and confirm."
)
SLOT_GONE_REPLY = (
    "The hold ran out and that slot is no longer available. Nothing was booked. "
    "Please pick another slot."
)
CHANGE_IN_PROGRESS_REPLY = (
    "I am sending that to the provider. It has not happened yet. "
    "I will tell you as soon as the provider verifies it."
)
CHANGE_ALREADY_CONFIRMED_REPLY = (
    "You already confirmed this, so it will only happen once. It is not confirmed yet."
)
CHANGE_NO_LONGER_VALID_REPLY = (
    "That confirmation is no longer valid, so your booking was not changed. "
    "The details changed or a newer card replaced it."
)
CHANGE_REJECTED_REPLY = "Okay, I left your booking as it is."
CHANGE_CARD_EXPIRED_REPLY = (
    "That card ran out, so your booking was not changed. Tell me if you still want this."
)
CHANGE_DUPLICATE_REPLY = (
    "A change to this booking is already in progress, so I did not start another."
)
NOT_HELD_NOTE = "This new time is not held. If it is taken first, your booking stays as it is."


class ApprovalNotFound(Exception):
    pass


def money(amount: int, currency: str) -> str:
    return f"₹{amount:,}" if currency == "INR" else f"{amount:,} {currency}"


def slot_label(slot: str) -> str:
    return datetime.fromisoformat(slot).astimezone(LOCAL_TZ).strftime("%a %d/%m/%Y, %H:%M")


def price_notes(price: int, list_price: int, max_budget: int | None, currency: str) -> list[str]:
    notes = []
    if price != list_price:
        direction = "higher" if price > list_price else "lower"
        notes.append(
            f"The quote is {direction} than the search price of {money(list_price, currency)}."
        )
    if max_budget is not None and price > max_budget:
        notes.append(f"This is over your budget of {money(max_budget, currency)}.")
    return notes


def _notes_of(approval: Approval) -> list[str]:
    if approval.action == BookingAction.RESCHEDULE:
        return [NOT_HELD_NOTE]
    return price_notes(approval.price, approval.list_price, approval.max_budget, approval.currency)


def card_of(approval: Approval) -> ApprovalCard:
    return ApprovalCard(
        approval_id=approval.id,
        action=approval.action,
        provider_name=approval.provider_name,
        service=approval.service,
        slot=approval.slot,
        slot_label=slot_label(approval.slot),
        old_slot_label=slot_label(approval.old_slot) if approval.old_slot else None,
        price=approval.price,
        currency=approval.currency,
        notes=_notes_of(approval),
        expires_at=approval.expires_at,
        status=approval.status,
    )


@dataclass
class BookingView:
    """What a session's booking looks like right now."""

    card: ApprovalCard | None = None
    attempt_open: bool = False
    attempt_action: str | None = None


class ApprovalService:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        gateway: ToolGateway,
        workflows: WorkflowService,
        jobs: JobService,
        traces: TraceService,
        notifications: NotificationService,
    ) -> None:
        self._db = db
        self._clock = clock
        self._gateway = gateway
        self._workflows = workflows
        self._jobs = jobs
        self._traces = traces
        self._notifications = notifications

    # Asking

    async def propose_booking(
        self,
        session_id: str,
        provider_id: str,
        slot: str,
        tool_context: CallContext,
    ) -> dict[str, Any]:
        """Take a hold and write the approval. The reply tells the model that the user decides."""
        user_id = tool_context.user_id
        recorder = tool_context.recorder
        target = await self._workflows.proposal_target(user_id, session_id, provider_id, slot)
        if not isinstance(target, ProposalTarget):
            return dict(target)

        # Another chat may be booking this slot. A new hold would take it out from under that.
        async with self._db.session_scope() as session:
            running = await BookingRepository(session).open_attempt_for_slot(
                user_id, provider_id, target.slot
            )
        if running is not None:
            return {
                "error": "booking_in_progress",
                "message": "This slot is already being booked for this user. Tell them to wait.",
            }

        # The model may propose twice. A second hold would replace the one on the card.
        view = await self.view(user_id, session_id, recorder)
        if view.card is not None:
            async with self._db.session_scope() as session:
                current = await BookingRepository(session).get_approval(view.card.approval_id)
            if current and (current.provider_id, current.slot) == (provider_id, target.slot):
                return self._for_model(view.card)

        hold = await self._gateway.call(
            "hold_slot",
            {"provider_id": provider_id, "slot": target.slot},
            CallContext(user_id, Caller.PROPOSAL, tool_context.budget, recorder),
        )
        if not hold.ok:
            return {
                "error": hold.error_code or "hold_failed",
                "message": f"{hold.message} Nothing was held. Offer the user another slot.",
            }

        run_id = recorder.run_id if recorder else uuid.uuid4()
        try:
            async with self._db.session_scope() as session:
                approval = self.approval_from_hold(
                    user_id,
                    session_id,
                    target.workflow_id,
                    run_id,
                    target.provider_name,
                    target.service,
                    target.max_budget,
                    hold,
                )
                moves = await self.write_approval_in(session, approval)
        except IntegrityError:
            return {
                "error": "approval_conflict",
                "message": "Another confirmation is open. Ask the user to answer that one first.",
            }
        if recorder is not None:
            await record_moves(recorder, moves)
            await self._trace_created(recorder, approval)
        return self._for_model(card_of(approval))

    async def propose_cancel(
        self, session_id: str, booking_id: str, tool_context: CallContext
    ) -> dict[str, Any]:
        return await self._propose_change(
            session_id, BookingAction.CANCEL, booking_id, None, tool_context
        )

    async def propose_reschedule(
        self, session_id: str, booking_id: str, day: str, at: str, tool_context: CallContext
    ) -> dict[str, Any]:
        """Code builds the slot from the user's own words, so the model never works out a date."""
        chosen = await self._workflows.read_day_and_time(tool_context.user_id, session_id, day, at)
        if isinstance(chosen, dict):
            return chosen
        slot = datetime.combine(chosen, time.fromisoformat(at), LOCAL_TZ)
        if slot <= self._clock.now():
            return {"error": "time_has_passed", "message": "That time has passed. Ask for another."}
        return await self._propose_change(
            session_id, BookingAction.RESCHEDULE, booking_id, slot.isoformat(), tool_context
        )

    async def _propose_change(
        self,
        session_id: str,
        action: BookingAction,
        booking_id: str,
        new_slot: str | None,
        tool_context: CallContext,
    ) -> dict[str, Any]:
        """Every detail on the card comes from the provider, none of it from the model."""
        user_id = tool_context.user_id
        recorder = tool_context.recorder
        snapshot = await self._workflows.snapshot(user_id, session_id)
        if snapshot.workflow_id is None or snapshot.state in RUNNING_STATES:
            return {
                "error": "booking_in_progress",
                "message": "A booking request is in progress. Tell the user to wait for it.",
            }

        context = CallContext(user_id, Caller.PROPOSAL, tool_context.budget, recorder)
        listed = await self._gateway.call("list_bookings", {"user_id": user_id}, context)
        if not listed.ok:
            return {
                "error": listed.error_code or "provider_unavailable",
                "message": "The provider's bookings could not be read. Nothing changed.",
            }
        booking = next(
            (item for item in listed.data["bookings"] if item["booking_id"] == booking_id), None
        )
        if booking is None:
            return {
                "error": "booking_not_found",
                "message": "That booking is not on this user's list. Use an ID from list_bookings.",
            }
        if booking["status"] != ACTIVE_BOOKING:
            return {
                "error": "booking_not_active",
                "message": f"This booking is {booking['status']}, so it cannot be changed.",
            }
        if new_slot is not None and same_time(new_slot, booking["slot"]):
            return {
                "error": "same_slot",
                "message": "The booking is already at that time. Ask which time they want.",
            }

        async with self._db.session_scope() as session:
            running = await BookingRepository(session).open_attempt_for_booking(booking_id)
        if running is not None:
            return {
                "error": "booking_in_progress",
                "message": "A change to this booking is in progress. Tell the user to wait.",
            }

        # The model may propose twice. The same question must not make a new card.
        view = await self.view(user_id, session_id, recorder)
        target = new_slot or booking["slot"]
        if view.card is not None and view.card.action == action:
            async with self._db.session_scope() as session:
                current = await BookingRepository(session).get_approval(view.card.approval_id)
            if current and current.booking_id == booking_id and same_time(current.slot, target):
                return self._for_model(view.card)

        provider = await self._gateway.call(
            "get_provider", {"provider_id": booking["provider_id"]}, context
        )
        if not provider.ok:
            return {
                "error": provider.error_code or "provider_unavailable",
                "message": "The provider's details could not be read. Nothing changed.",
            }

        approval = self._change_approval(
            action,
            user_id,
            session_id,
            snapshot.workflow_id,
            recorder.run_id if recorder else uuid.uuid4(),
            booking,
            target,
            str(provider.data["currency"]),
        )
        try:
            async with self._db.session_scope() as session:
                moves = await self.write_approval_in(session, approval)
        except IntegrityError:
            return {
                "error": "approval_conflict",
                "message": "Another confirmation is open. Ask the user to answer that one first.",
            }
        if recorder is not None:
            await record_moves(recorder, moves)
            await self._trace_created(recorder, approval)
        return self._for_model(card_of(approval))

    def _change_approval(
        self,
        action: BookingAction,
        user_id: str,
        session_id: str,
        workflow_id: uuid.UUID,
        run_id: uuid.UUID,
        booking: dict[str, Any],
        slot: str,
        currency: str,
    ) -> Approval:
        now = self._clock.now()
        expires_at = now + CHANGE_CARD_LIFE
        old_slot = str(booking["slot"]) if action == BookingAction.RESCHEDULE else None
        facts = ApprovedFacts(
            action=action.value,
            user_id=user_id,
            provider_id=str(booking["provider_id"]),
            provider_name=str(booking["provider_name"]),
            slot=slot,
            price=int(booking["price"]),
            list_price=int(booking["price"]),
            currency=currency,
            hold_id=None,
            expires_at=expiry_text(expires_at),
            booking_id=str(booking["booking_id"]),
            old_slot=old_slot,
        )
        return Approval(
            id=uuid.uuid4(),
            action=facts.action,
            user_id=user_id,
            session_id=session_id,
            workflow_id=workflow_id,
            run_id=run_id,
            provider_id=facts.provider_id,
            provider_name=facts.provider_name,
            service=str(booking["service"]),
            slot=slot,
            old_slot=old_slot,
            booking_id=facts.booking_id,
            price=facts.price,
            list_price=facts.list_price,
            currency=currency,
            max_budget=None,
            hold_id=None,
            expires_at=expires_at,
            approval_hash=facts.hash(),
            status=ApprovalStatus.PENDING.value,
            created_at=now,
        )

    def _for_model(self, card: ApprovalCard) -> dict[str, Any]:
        if card.action != BookingAction.BOOK:
            verb = "cancelled" if card.action == BookingAction.CANCEL else "moved"
            return {
                "card_shown_to_user": True,
                "provider": card.provider_name,
                "slot": card.slot_label,
                "moving_from": card.old_slot_label,
                "price": money(card.price, card.currency),
                "notes": card.notes,
                "next_step": "The user now sees a card with these details and a Confirm button. "
                f"Say the booking has not been {verb} yet, mention any notes and ask them to "
                "confirm. You cannot confirm for them.",
            }
        return {
            "card_shown_to_user": True,
            "provider": card.provider_name,
            "slot": card.slot_label,
            "price": money(card.price, card.currency),
            "notes": card.notes,
            "next_step": "The user now sees a card with these details and a Confirm button. "
            "Say the slot is held, not booked. Mention any notes and ask them to confirm. "
            "You cannot confirm for them.",
        }

    def approval_from_hold(
        self,
        user_id: str,
        session_id: str,
        workflow_id: uuid.UUID,
        run_id: uuid.UUID,
        provider_name: str,
        service: str,
        max_budget: int | None,
        hold: ToolResult,
    ) -> Approval:
        data = hold.data
        expires_at = datetime.fromisoformat(str(data["expires_at"]).replace("Z", "+00:00"))
        facts = ApprovedFacts(
            action=BookingAction.BOOK.value,
            user_id=user_id,
            provider_id=str(data["provider_id"]),
            provider_name=provider_name,
            slot=str(data["slot"]),
            price=int(data["quoted_price"]),
            list_price=int(data["search_price"]),
            currency=str(data["currency"]),
            hold_id=str(data["hold_id"]),
            expires_at=expiry_text(expires_at),
        )
        return Approval(
            id=uuid.uuid4(),
            action=facts.action,
            user_id=user_id,
            session_id=session_id,
            workflow_id=workflow_id,
            run_id=run_id,
            provider_id=facts.provider_id,
            provider_name=provider_name,
            service=service,
            slot=facts.slot,
            price=facts.price,
            list_price=facts.list_price,
            currency=facts.currency,
            max_budget=max_budget,
            hold_id=facts.hold_id,
            expires_at=expires_at,
            approval_hash=facts.hash(),
            status=ApprovalStatus.PENDING.value,
            created_at=self._clock.now(),
        )

    async def write_approval_in(
        self, session: AsyncSession, approval: Approval
    ) -> list[tuple[WorkflowState, WorkflowState]]:
        """One open question per workflow. A newer card always replaces an older one."""
        repository = BookingRepository(session)
        await repository.void_pending_for_workflow(
            approval.workflow_id, "superseded", self._clock.now()
        )
        await repository.add(approval)
        return await self._workflows.move_in(
            session, approval.workflow_id, WorkflowState.AWAITING_APPROVAL
        )

    async def _trace_created(self, recorder: RunRecorder, approval: Approval) -> None:
        await recorder.event(
            "approval_created",
            name=approval.action,
            data={
                "approval_id": str(approval.id),
                "provider_id": approval.provider_id,
                "slot": approval.slot,
                "price": approval.price,
                "list_price": approval.list_price,
                "hold_id": approval.hold_id,
                "booking_id": approval.booking_id,
                "old_slot": approval.old_slot,
                "expires_at": approval.expires_at.isoformat(),
                "hash": approval.approval_hash,
            },
        )

    # What is open right now

    async def view(
        self, user_id: str, session_id: str, recorder: RunRecorder | None = None
    ) -> BookingView:
        """Closes a card that no longer applies, so it can never be answered or shown again."""
        now = self._clock.now()
        event: tuple[str, str] | None = None
        moves: list[tuple[WorkflowState, WorkflowState]] = []
        async with self._db.session_scope() as session:
            repository = BookingRepository(session)
            attempt = await repository.open_attempt(user_id, session_id)
            pending = await repository.pending_approval(user_id, session_id)
            card = None
            if pending is not None:
                state = await self._workflows.state_in(session, pending.workflow_id)
                # Read again now the lock is held. A click may have consumed it in the meantime.
                pending = await repository.get_approval(pending.id)
                assert pending is not None
                if pending.status != ApprovalStatus.PENDING.value:
                    pass
                elif state != WorkflowState.AWAITING_APPROVAL:
                    await repository.close_approval(
                        pending.id, ApprovalStatus.VOIDED, "request_changed", now
                    )
                    event = ("approval_voided", "request_changed")
                elif pending.expires_at <= now:
                    await repository.close_approval(
                        pending.id, ApprovalStatus.EXPIRED, "hold_expired", now
                    )
                    moves = await self._workflows.move_in(
                        session, pending.workflow_id, WorkflowState.COMPARING
                    )
                    event = ("approval_expired", "hold_expired")
                else:
                    card = card_of(pending)
        if recorder is not None:
            await record_moves(recorder, moves)
            if event is not None and pending is not None:
                await recorder.event(
                    event[0], data={"approval_id": str(pending.id), "reason": event[1]}
                )
        return BookingView(
            card=card,
            attempt_open=attempt is not None,
            attempt_action=attempt.action if attempt else None,
        )

    async def verified_actions(self, user_id: str, session_id: str) -> set[str]:
        async with self._db.session_scope() as session:
            return await BookingRepository(session).verified_actions(user_id, session_id)

    async def unfinished_actions(self, user_id: str, session_id: str) -> set[str]:
        """What is still waiting for a yes or a check, so nobody may say it has happened."""
        view = await self.view(user_id, session_id)
        unfinished = {view.attempt_action} if view.attempt_action else set()
        if view.card is not None:
            unfinished.add(view.card.action)
        return unfinished

    async def read(self, approval_id: uuid.UUID, user_id: str) -> ApprovalRead:
        async with self._db.session_scope() as session:
            repository = BookingRepository(session)
            approval = await repository.get_approval(approval_id)
            if approval is None or approval.user_id != user_id:
                raise ApprovalNotFound(str(approval_id))
            attempt = await repository.attempt_for_approval(approval_id)
        return ApprovalRead(
            card=card_of(approval),
            attempt_status=attempt.status if attempt else None,
            booking_id=attempt.booking_id if attempt else None,
        )

    async def prompt_note(self, user_id: str, session_id: str) -> str:
        """The model's view of a booking. Workers never write to its session, so it reads this."""
        view = await self.view(user_id, session_id)
        lines = []
        if view.card is not None:
            card = view.card
            what = {
                BookingAction.BOOK: "Nothing is booked.",
                BookingAction.CANCEL: "The booking is NOT cancelled.",
                BookingAction.RESCHEDULE: "The booking has NOT moved.",
            }[BookingAction(card.action)]
            lines.append(
                f"A card to {card.action} is on screen: {card.provider_name}, {card.slot_label}, "
                f"{money(card.price, card.currency)}. Only the user's own click or reply can "
                f"approve it. {what}"
            )
        if view.attempt_open:
            lines.append(
                f"A {view.attempt_action} request is in progress and NOT confirmed yet. "
                "Never say it has happened. Say the check is still running."
            )
        latest = await self._notifications.latest_texts(user_id, session_id, limit=2)
        lines.extend(f"Latest update, written by the system: {text}" for text in latest)
        return "\n".join(lines) or "No booking activity in this chat."

    async def latest_updates(self, user_id: str, session_id: str, limit: int) -> list[str]:
        """What happened to bookings in this chat, in the system's own words."""
        return await self._notifications.latest_texts(user_id, session_id, limit)

    # Answering

    async def decide_from_message(
        self, user_id: str, session_id: str, message: str, recorder: RunRecorder
    ) -> ApprovalDecision | None:
        """A whole-message yes or no, read before the model runs. None sends it to the model."""
        if read_decision(message) is None:
            return None
        async with self._db.session_scope() as session:
            repository = BookingRepository(session)
            pending = await repository.pending_approval(user_id, session_id)
            attempt = await repository.open_attempt(user_id, session_id)

        if pending is None:
            if attempt is not None and read_decision(message, attempt.action) == Decision.YES:
                return self._already_confirmed(attempt.action)
            return None
        # "Book it" must not approve a cancel, so the words are read against this card's action.
        decision = read_decision(message, pending.action)
        if decision is None:
            return None
        await recorder.event(
            "approval_answer",
            data={"answer": decision.value, "approval_id": str(pending.id), "source": "message"},
        )
        if decision == Decision.YES:
            return await self.confirm(pending.id, user_id, recorder)
        return await self.reject(pending.id, user_id, recorder)

    async def confirm(
        self, approval_id: uuid.UUID, user_id: str, recorder: RunRecorder
    ) -> ApprovalDecision:
        """One transaction: consume the approval, write the attempt, queue the job."""
        now = self._clock.now()
        try:
            async with self._db.session_scope() as session:
                repository = BookingRepository(session)
                approval = await repository.get_approval(approval_id)
                if approval is None or approval.user_id != user_id:
                    raise ApprovalNotFound(str(approval_id))
                state = await self._workflows.state_in(session, approval.workflow_id)
                # Read again now the lock is held. Another click may have won while this one waited.
                approval = await repository.get_approval(approval_id)
                assert approval is not None
                if approval.status == ApprovalStatus.CONSUMED.value:
                    return self._already_confirmed(approval.action)
                if approval.status != ApprovalStatus.PENDING.value:
                    return self._no_longer_valid(approval.action)

                stale = state != WorkflowState.AWAITING_APPROVAL
                expired = approval.expires_at <= now
                attempt, moves = None, []
                if stale:
                    await repository.close_approval(
                        approval_id, ApprovalStatus.VOIDED, "request_changed", now
                    )
                elif not expired:
                    if not await repository.consume(approval_id, now):
                        return self._already_confirmed(approval.action)
                    moves = await self._workflows.move_in(
                        session, approval.workflow_id, WorkflowState.EXECUTING
                    )
                    attempt = self._new_attempt(approval, recorder.run_id, now)
                    await repository.add(attempt)
                    await self._jobs.enqueue_in(session, BOOK_JOB, {"attempt_id": str(attempt.id)})
        except IntegrityError:
            # This slot already has a booking under way, so this approval must not start another.
            return await self._void_duplicate(approval_id, recorder)

        if stale:
            await recorder.event(
                "approval_voided",
                data={"approval_id": str(approval_id), "reason": "request_changed"},
            )
            return self._no_longer_valid(approval.action)
        if attempt is None:
            if approval.action != BookingAction.BOOK:
                return await self._close_expired_change(approval, recorder)
            return await self._renew_expired(approval, recorder)

        await record_moves(recorder, moves)
        await recorder.event(
            "approval_consumed",
            data={
                "approval_id": str(approval_id),
                "attempt_id": str(attempt.id),
                "idempotency_key": attempt.idempotency_key,
            },
        )
        reply = IN_PROGRESS_REPLY
        if approval.action != BookingAction.BOOK:
            reply = CHANGE_IN_PROGRESS_REPLY
        return ApprovalDecision(status=DecisionStatus.IN_PROGRESS, outcome="verifying", reply=reply)

    def _new_attempt(self, approval: Approval, run_id: uuid.UUID, now: datetime) -> BookingAttempt:
        return BookingAttempt(
            id=uuid.uuid4(),
            approval_id=approval.id,
            action=approval.action,
            user_id=approval.user_id,
            session_id=approval.session_id,
            workflow_id=approval.workflow_id,
            run_id=run_id,
            provider_id=approval.provider_id,
            slot=approval.slot,
            hold_id=approval.hold_id,
            booking_id=approval.booking_id,
            idempotency_key=idempotency_key(approval.action, approval.id),
            status=AttemptStatus.OPEN.value,
            queued_at=now,
            claims=0,
            book_calls=0,
            unconfirmed_notified=False,
            created_at=now,
            updated_at=now,
        )

    def _already_confirmed(self, action: str) -> ApprovalDecision:
        return ApprovalDecision(
            status=DecisionStatus.ALREADY_CONFIRMED,
            outcome="verifying",
            reply=ALREADY_CONFIRMED_REPLY
            if action == BookingAction.BOOK
            else CHANGE_ALREADY_CONFIRMED_REPLY,
        )

    def _no_longer_valid(self, action: str) -> ApprovalDecision:
        return ApprovalDecision(
            status=DecisionStatus.NO_LONGER_VALID,
            outcome="needs_input",
            reply=NO_LONGER_VALID_REPLY
            if action == BookingAction.BOOK
            else CHANGE_NO_LONGER_VALID_REPLY,
        )

    async def _close_expired_change(
        self, approval: Approval, recorder: RunRecorder
    ) -> ApprovalDecision:
        """There is no hold to renew, and an old yes must not change a booking."""
        async with self._db.session_scope() as session:
            await BookingRepository(session).close_approval(
                approval.id, ApprovalStatus.EXPIRED, "card_expired", self._clock.now()
            )
            moves = await self._workflows.move_in(
                session, approval.workflow_id, WorkflowState.COMPARING
            )
        await record_moves(recorder, moves)
        await recorder.event(
            "approval_expired", data={"approval_id": str(approval.id), "reason": "card_expired"}
        )
        return ApprovalDecision(
            status=DecisionStatus.NO_LONGER_VALID,
            outcome="needs_input",
            reply=CHANGE_CARD_EXPIRED_REPLY,
        )

    async def _void_duplicate(
        self, approval_id: uuid.UUID, recorder: RunRecorder
    ) -> ApprovalDecision:
        now = self._clock.now()
        async with self._db.session_scope() as session:
            repository = BookingRepository(session)
            approval = await repository.get_approval(approval_id)
            await repository.close_approval(
                approval_id, ApprovalStatus.VOIDED, "duplicate_booking", now
            )
            moves = []
            if approval is not None:
                moves = await self._workflows.move_in(
                    session, approval.workflow_id, WorkflowState.COMPARING
                )
        await record_moves(recorder, moves)
        await recorder.event(
            "approval_voided", data={"approval_id": str(approval_id), "reason": "duplicate_booking"}
        )
        return ApprovalDecision(
            status=DecisionStatus.NO_LONGER_VALID,
            outcome="needs_input",
            reply="A booking for this slot is already in progress, so I did not start another."
            if approval is None or approval.action == BookingAction.BOOK
            else CHANGE_DUPLICATE_REPLY,
        )

    async def _renew_expired(self, old: Approval, recorder: RunRecorder) -> ApprovalDecision:
        """An old yes must not book, even at the same price. Take a new hold and ask again."""
        hold = await self._gateway.call(
            "hold_slot",
            {"provider_id": old.provider_id, "slot": old.slot},
            CallContext(old.user_id, Caller.PROPOSAL, ToolBudget(RENEW_TOOL_BUDGET), recorder),
        )
        now = self._clock.now()
        approval = None
        async with self._db.session_scope() as session:
            repository = BookingRepository(session)
            await repository.close_approval(old.id, ApprovalStatus.EXPIRED, "hold_expired", now)
            if hold.ok:
                approval = self.approval_from_hold(
                    old.user_id,
                    old.session_id,
                    old.workflow_id,
                    recorder.run_id,
                    old.provider_name,
                    old.service,
                    old.max_budget,
                    hold,
                )
                moves = await self.write_approval_in(session, approval)
            else:
                moves = await self._workflows.move_in(
                    session, old.workflow_id, WorkflowState.COMPARING
                )
        await record_moves(recorder, moves)
        await recorder.event(
            "approval_expired", data={"approval_id": str(old.id), "reason": "hold_expired"}
        )
        if approval is None:
            return ApprovalDecision(
                status=DecisionStatus.SLOT_GONE, outcome="needs_input", reply=SLOT_GONE_REPLY
            )
        await self._trace_created(recorder, approval)
        return ApprovalDecision(
            status=DecisionStatus.NEW_CARD,
            outcome="awaiting_approval",
            reply=NEW_CARD_REPLY,
            approval=card_of(approval),
        )

    async def reject(
        self, approval_id: uuid.UUID, user_id: str, recorder: RunRecorder
    ) -> ApprovalDecision:
        now = self._clock.now()
        moves: list[tuple[WorkflowState, WorkflowState]] = []
        async with self._db.session_scope() as session:
            repository = BookingRepository(session)
            approval = await repository.get_approval(approval_id)
            if approval is None or approval.user_id != user_id:
                raise ApprovalNotFound(str(approval_id))
            if approval.status == ApprovalStatus.CONSUMED.value:
                return self._already_confirmed(approval.action)
            if not await repository.close_approval(
                approval_id, ApprovalStatus.REJECTED, "user_said_no", now
            ):
                return self._no_longer_valid(approval.action)
            state = await self._workflows.state_in(session, approval.workflow_id)
            if state == WorkflowState.AWAITING_APPROVAL:
                moves = await self._workflows.move_in(
                    session, approval.workflow_id, WorkflowState.COMPARING
                )
        await record_moves(recorder, moves)
        await recorder.event("approval_rejected", data={"approval_id": str(approval_id)})
        return ApprovalDecision(
            status=DecisionStatus.REJECTED,
            outcome="needs_input",
            reply=REJECTED_REPLY
            if approval.action == BookingAction.BOOK
            else CHANGE_REJECTED_REPLY,
        )

    # Clicks

    async def confirm_click(self, approval_id: uuid.UUID, user_id: str) -> ApprovalDecision:
        return await self._click(approval_id, user_id, "yes")

    async def reject_click(self, approval_id: uuid.UUID, user_id: str) -> ApprovalDecision:
        return await self._click(approval_id, user_id, "no")

    async def _click(self, approval_id: uuid.UUID, user_id: str, answer: str) -> ApprovalDecision:
        """A click is its own run. It needs no model and no turn lock, only PostgreSQL."""
        async with self._db.session_scope() as session:
            approval = await BookingRepository(session).get_approval(approval_id)
        if approval is None or approval.user_id != user_id:
            raise ApprovalNotFound(str(approval_id))

        recorder = await self._traces.start_run(user_id, approval.session_id)
        await recorder.event(
            "approval_answer",
            data={"answer": answer, "approval_id": str(approval_id), "source": "click"},
        )
        if answer == "yes":
            decision = await self.confirm(approval_id, user_id, recorder)
        else:
            decision = await self.reject(approval_id, user_id, recorder)
        await self._traces.finish_run(recorder, decision.outcome, None, decision.reply)
        await self.publish()
        return decision.model_copy(
            update={"run_id": recorder.run_id, "session_id": approval.session_id}
        )

    async def publish(self) -> None:
        """Send queued jobs now. If Redis is down they wait in PostgreSQL for the next sweep."""
        try:
            await self._jobs.publish_pending()
        except Exception:
            logger.exception("could not publish after an approval; the sweep will")
