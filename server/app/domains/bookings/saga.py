"""Carries out one approved booking, cancel or reschedule, in the worker.

Every result ends at the source of truth: `get_booking_status` runs after every `book`, and
`list_bookings` after every `cancel` or `reschedule`, whatever the call returned. Only that answer
can report success. The attempt row commits before each call, so a job that dies or runs twice picks
up from what is known. The provider replays a repeated key, so a repeat cannot act twice.
"""

import asyncio
import logging
import uuid
from datetime import timedelta
from typing import Any

from app.core.clock import Clock
from app.core.database import Database
from app.domains.bookings.models import Approval, AttemptStatus, BookingAction, BookingAttempt
from app.domains.bookings.repository import BookingRepository
from app.domains.bookings.service import (
    ACTIVE_BOOKING,
    BOOK_JOB,
    ApprovalService,
    card_of,
    money,
    slot_label,
)
from app.domains.bookings.verification import change_token_for, token_for
from app.domains.gateway.schemas import Caller, ToolStatus
from app.domains.gateway.service import CallContext, ToolBudget, ToolGateway
from app.domains.jobs.service import JobService
from app.domains.notifications.service import NotificationService
from app.domains.tracing.service import RunRecorder, TraceService
from app.domains.workflow.models import WorkflowState as S
from app.domains.workflow.service import WorkflowService, record_moves, same_time

logger = logging.getLogger(__name__)

# The first call plus two retries, with the same hold and the same key.
MAX_BOOK_CALLS = 3
# The gateway retries a cancel or a reschedule itself, so this counts rounds of those.
MAX_CHANGE_CALLS = 2
SAGA_TOOL_BUDGET = 30
BOOKED_OK = "ok"
# The call may not have reached the provider, so a retry is fair once status says not booked.
_RETRYABLE = frozenset({None, "timeout", "provider_unavailable", "provider_error"})


def _describe(approval: Approval) -> str:
    return f"{approval.provider_name}, {slot_label(approval.slot)}"


def _describe_booking(approval: Approval) -> str:
    """The booking as it was when the user agreed, before any move."""
    return f"{approval.provider_name}, {slot_label(approval.old_slot or approval.slot)}"


def _change_is_done(approval: Approval, booking: dict[str, Any]) -> bool:
    if approval.action == BookingAction.CANCEL:
        return booking["status"] == "cancelled"
    return booking["status"] == ACTIVE_BOOKING and same_time(booking["slot"], approval.slot)


def _shows_as(booking: dict[str, Any] | None) -> str:
    if booking is None:
        return "The provider no longer lists this booking."
    if booking["status"] == ACTIVE_BOOKING:
        return f"The provider still shows it as active, at {slot_label(booking['slot'])}."
    return f"The provider shows it as {booking['status']}."


class BookingSaga:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        gateway: ToolGateway,
        approvals: ApprovalService,
        workflows: WorkflowService,
        notifications: NotificationService,
        traces: TraceService,
        jobs: JobService,
        lease_s: float,
    ) -> None:
        self._db = db
        self._clock = clock
        self._gateway = gateway
        self._approvals = approvals
        self._workflows = workflows
        self._notifications = notifications
        self._traces = traces
        self._jobs = jobs
        self._lease = timedelta(seconds=lease_s)

    async def run_job(self, payload: dict[str, Any]) -> None:
        await self.run(uuid.UUID(payload["attempt_id"]))

    async def recover(self) -> int:
        """Open attempts with no live lease and no recent job get a new job. Called by the sweep."""
        now = self._clock.now()
        async with self._db.session_scope() as session:
            attempts = await BookingRepository(session).attempts_to_requeue(now, self._lease)
            for attempt in attempts:
                attempt.queued_at = now
                await self._jobs.enqueue_in(session, BOOK_JOB, {"attempt_id": str(attempt.id)})
            return len(attempts)

    async def run(self, attempt_id: uuid.UUID) -> None:
        now = self._clock.now()
        async with self._db.session_scope() as session:
            repository = BookingRepository(session)
            attempt = await repository.claim_attempt(attempt_id, now, self._lease)
            if attempt is None:
                # Finished, or another worker holds it. If that worker dies, the sweep comes back.
                return
            approval = await repository.get_approval(attempt.approval_id)
        assert approval is not None

        recorder = await self._traces.resume_run(attempt.run_id)
        await recorder.event(
            "saga_claimed",
            data={
                "attempt_id": str(attempt.id),
                "claim": attempt.claims,
                "book_calls_so_far": attempt.book_calls,
                "last_result": attempt.last_result,
            },
        )
        context = CallContext(
            attempt.user_id, Caller.SAGA, ToolBudget(SAGA_TOOL_BUDGET), recorder=recorder
        )
        try:
            if approval.action == BookingAction.BOOK:
                await self._drive(attempt, approval, context, recorder)
            else:
                await self._drive_change(attempt, approval, context, recorder)
        except (Exception, asyncio.CancelledError):
            # The job limit or a fault. The next delivery carries on, and the user hears about it.
            await self._say_not_confirmed_safely(attempt, approval, recorder)
            raise
        finally:
            await self._release(attempt)

    async def _say_not_confirmed_safely(
        self, attempt: BookingAttempt, approval: Approval, recorder: RunRecorder
    ) -> None:
        try:
            await self._not_confirmed(attempt, approval, recorder)
        except Exception:
            logger.exception("could not record that attempt %s is unconfirmed", attempt.id)

    async def _release(self, attempt: BookingAttempt) -> None:
        """Let the next delivery start at once. If this fails, the lease simply runs out."""
        try:
            async with self._db.session_scope() as session:
                await BookingRepository(session).update_claimed(
                    attempt.id, attempt.claims, self._clock.now(), lease_until=None
                )
        except Exception:
            logger.exception("could not release attempt %s", attempt.id)

    async def _drive(
        self,
        attempt: BookingAttempt,
        approval: Approval,
        context: CallContext,
        recorder: RunRecorder,
    ) -> None:
        book_calls, last_result = attempt.book_calls, attempt.last_result
        while True:
            if book_calls > 0:
                await self._move(attempt, recorder, S.VERIFYING)
                status = await self._gateway.call(
                    "get_booking_status", {"idempotency_key": attempt.idempotency_key}, context
                )
                if not status.ok:
                    await self._not_confirmed(attempt, approval, recorder)
                    return
                answer = status.data.get("status")
                await recorder.event(
                    "booking_status_checked",
                    data={"status": answer, "after_book_result": last_result},
                )
                if answer == "booked":
                    await self._finish_booked(
                        attempt, approval, str(status.data.get("booking_id")), recorder
                    )
                    return
                if answer != "not_booked":
                    await self._close_failed(attempt, approval, f"status_{answer}", recorder)
                    return
                if last_result == "slot_taken":
                    await self._close_slot_taken(attempt, approval, recorder)
                    return
                if last_result == "hold_expired":
                    await self._ask_again(
                        attempt, approval, AttemptStatus.HOLD_EXPIRED, context, recorder
                    )
                    return
                if last_result not in _RETRYABLE:
                    await self._close_failed(attempt, approval, str(last_result), recorder)
                    return
                if book_calls >= MAX_BOOK_CALLS:
                    await self._close_failed(attempt, approval, "retries_exhausted", recorder)
                    return

            await self._move(attempt, recorder, S.EXECUTING)
            provider = await self._gateway.call(
                "get_provider", {"provider_id": approval.provider_id}, context
            )
            if not provider.ok:
                await self._not_confirmed(attempt, approval, recorder)
                return

            current_price = int(provider.data.get("price", -1))
            context.approval_token = token_for(approval, current_price)
            if not await self._note_book_call(attempt, book_calls + 1):
                return  # This worker lost its lease. The new owner carries on.
            book_calls += 1
            booked = await self._gateway.call(
                "book",
                {"hold_id": approval.hold_id, "idempotency_key": attempt.idempotency_key},
                context,
            )
            context.approval_token = None
            if booked.status == ToolStatus.REFUSED:
                # Nothing was sent, so the count goes back to the truth.
                await self._note_book_call(attempt, book_calls - 1)
                await self._after_refusal(attempt, approval, current_price, context, recorder)
                return
            last_result = BOOKED_OK if booked.ok else booked.error_code
            if not await self._note_result(attempt, last_result):
                return

    async def _drive_change(
        self,
        attempt: BookingAttempt,
        approval: Approval,
        context: CallContext,
        recorder: RunRecorder,
    ) -> None:
        calls, last_result = attempt.book_calls, attempt.last_result
        while True:
            if calls > 0:
                await self._move(attempt, recorder, S.VERIFYING)
            listed = await self._gateway.call(
                "list_bookings", {"user_id": attempt.user_id}, context
            )
            if not listed.ok:
                await self._not_confirmed(attempt, approval, recorder)
                return
            booking = next(
                (
                    item
                    for item in listed.data["bookings"]
                    if item["booking_id"] == approval.booking_id
                ),
                None,
            )
            if calls > 0:
                await recorder.event(
                    "booking_status_checked",
                    data={
                        "status": booking["status"] if booking else None,
                        "slot": booking["slot"] if booking else None,
                        "after_call_result": last_result,
                    },
                )
                if booking is not None and _change_is_done(approval, booking):
                    await self._finish_change(attempt, approval, recorder)
                    return
            if booking is None or booking["status"] != ACTIVE_BOOKING:
                await self._close_change_failed(attempt, approval, booking, "changed", recorder)
                return
            if calls > 0:
                if last_result == "slot_conflict":
                    await self._close_conflict(attempt, approval, booking, recorder)
                    return
                if last_result not in _RETRYABLE:
                    await self._close_change_failed(
                        attempt, approval, booking, str(last_result), recorder
                    )
                    return
                if calls >= MAX_CHANGE_CALLS:
                    await self._close_change_failed(
                        attempt, approval, booking, "retries_exhausted", recorder
                    )
                    return

            await self._move(attempt, recorder, S.EXECUTING)
            context.approval_token = change_token_for(approval, booking)
            if not await self._note_book_call(attempt, calls + 1):
                return  # This worker lost its lease. The new owner carries on.
            calls += 1
            args = {"booking_id": approval.booking_id, "idempotency_key": attempt.idempotency_key}
            if approval.action == BookingAction.RESCHEDULE:
                args["slot"] = approval.slot
            result = await self._gateway.call(approval.action, args, context)
            context.approval_token = None
            if result.status == ToolStatus.REFUSED:
                # Nothing was sent. The booking is no longer what the user agreed to change.
                await self._note_book_call(attempt, calls - 1)
                await recorder.event(
                    "approval_mismatch",
                    data={"approval_id": str(approval.id), "booking": booking},
                )
                await self._close_change_failed(
                    attempt, approval, booking, "approval_mismatch", recorder
                )
                return
            last_result = BOOKED_OK if result.ok else result.error_code
            if not await self._note_result(attempt, last_result):
                return

    async def _after_refusal(
        self,
        attempt: BookingAttempt,
        approval: Approval,
        current_price: int,
        context: CallContext,
        recorder: RunRecorder,
    ) -> None:
        """Nothing was sent. Either the price moved, which needs a fresh yes, or the row changed."""
        await recorder.event(
            "approval_mismatch",
            data={
                "approval_id": str(approval.id),
                "approved_list_price": approval.list_price,
                "current_list_price": current_price,
            },
        )
        if current_price != approval.list_price:
            await self._ask_again(attempt, approval, AttemptStatus.PRICE_CHANGED, context, recorder)
        else:
            await self._close_failed(attempt, approval, "approval_mismatch", recorder)

    async def _move(self, attempt: BookingAttempt, recorder: RunRecorder, *targets: S) -> None:
        async with self._db.session_scope() as session:
            moves = await self._workflows.move_in(session, attempt.workflow_id, *targets)
        await record_moves(recorder, moves)

    async def _note_book_call(self, attempt: BookingAttempt, book_calls: int) -> bool:
        async with self._db.session_scope() as session:
            return await BookingRepository(session).update_claimed(
                attempt.id,
                attempt.claims,
                self._clock.now(),
                book_calls=book_calls,
                last_result=None,
            )

    async def _note_result(self, attempt: BookingAttempt, result: str | None) -> bool:
        async with self._db.session_scope() as session:
            return await BookingRepository(session).update_claimed(
                attempt.id, attempt.claims, self._clock.now(), last_result=result
            )

    async def _not_confirmed(
        self, attempt: BookingAttempt, approval: Approval, recorder: RunRecorder
    ) -> None:
        """The attempt stays open and the sweep tries again. The user hears this once."""
        what = {
            BookingAction.BOOK: f"Your booking with {_describe(approval)}",
            BookingAction.CANCEL: f"Cancelling {_describe(approval)}",
            BookingAction.RESCHEDULE: (
                f"Moving {_describe_booking(approval)} to {slot_label(approval.slot)}"
            ),
        }[BookingAction(approval.action)]
        text = (
            f"{what} is not confirmed yet. "
            "I have not been able to check with the provider yet, and I will keep trying."
        )
        async with self._db.session_scope() as session:
            first = await BookingRepository(session).update_claimed(
                attempt.id,
                attempt.claims,
                self._clock.now(),
                unconfirmed_notified=True,
            )
            if first and not attempt.unconfirmed_notified:
                await self._notifications.add_in(
                    session,
                    attempt.user_id,
                    attempt.session_id,
                    "booking_not_confirmed",
                    text,
                    {"run_id": str(attempt.run_id)},
                )
        attempt.unconfirmed_notified = True
        await recorder.event("booking_not_confirmed", data={"attempt_id": str(attempt.id)})

    async def _finish_booked(
        self, attempt: BookingAttempt, approval: Approval, booking_id: str, recorder: RunRecorder
    ) -> None:
        text = (
            f"Booked and verified with the provider: {_describe(approval)}, "
            f"{money(approval.price, approval.currency)}. Booking ID: {booking_id}."
        )
        moves = []
        async with self._db.session_scope() as session:
            closed = await BookingRepository(session).close_attempt(
                attempt.id, AttemptStatus.BOOKED, self._clock.now(), booking_id=booking_id
            )
            if not closed:
                return
            moves = await self._workflows.move_in(session, attempt.workflow_id, S.VERIFYING, S.DONE)
            await self._notifications.add_in(
                session,
                attempt.user_id,
                attempt.session_id,
                "booking_verified",
                text,
                {"booking_id": booking_id, "run_id": str(attempt.run_id)},
            )
        await record_moves(recorder, moves)
        await recorder.event(
            "booking_verified", data={"attempt_id": str(attempt.id), "booking_id": booking_id}
        )
        await self._traces.settle_run(recorder, "done", text)

    async def _finish_change(
        self, attempt: BookingAttempt, approval: Approval, recorder: RunRecorder
    ) -> None:
        if approval.action == BookingAction.CANCEL:
            status, kind = AttemptStatus.CANCELLED, "booking_cancelled"
            text = f"Cancelled and verified with the provider: {_describe(approval)}."
        else:
            status, kind = AttemptStatus.RESCHEDULED, "booking_rescheduled"
            text = (
                f"Moved and verified with the provider: {approval.provider_name}, now "
                f"{slot_label(approval.slot)}. It was {slot_label(approval.old_slot or '')}."
            )
        text += f" Booking ID: {approval.booking_id}."
        moves = []
        async with self._db.session_scope() as session:
            closed = await BookingRepository(session).close_attempt(
                attempt.id, status, self._clock.now()
            )
            if not closed:
                return
            moves = await self._workflows.close_change_in(session, attempt.workflow_id)
            await self._notifications.add_in(
                session,
                attempt.user_id,
                attempt.session_id,
                kind,
                text,
                {"booking_id": approval.booking_id, "run_id": str(attempt.run_id)},
            )
        await record_moves(recorder, moves)
        await recorder.event(
            kind, data={"attempt_id": str(attempt.id), "booking_id": approval.booking_id}
        )
        await self._traces.settle_run(recorder, "done", text)

    async def _close_conflict(
        self,
        attempt: BookingAttempt,
        approval: Approval,
        booking: dict[str, Any],
        recorder: RunRecorder,
    ) -> None:
        others = await self._workflows.other_slots(
            attempt.workflow_id, attempt.provider_id, attempt.slot
        )
        listed = "; ".join(slot_label(slot) for slot in others)
        offer = f" Other open slots with this provider: {listed}." if listed else ""
        await self._close_to_comparing(
            attempt,
            AttemptStatus.SLOT_CONFLICT,
            "reschedule_conflict",
            f"{slot_label(approval.slot)} is not available with {approval.provider_name}, so your "
            f"booking did not move. It stays at {slot_label(booking['slot'])}.{offer} "
            "Tell me another time and I will ask you to confirm it.",
            recorder,
        )

    async def _close_change_failed(
        self,
        attempt: BookingAttempt,
        approval: Approval,
        booking: dict[str, Any] | None,
        reason: str,
        recorder: RunRecorder,
    ) -> None:
        verb = "cancel" if approval.action == BookingAction.CANCEL else "move"
        if reason == "invalid_slot":
            why = f"{approval.provider_name} does not work at {slot_label(approval.slot)}."
        elif reason in ("changed", "approval_mismatch"):
            why = "It is no longer what you agreed to change, so I did nothing."
        else:
            why = "The provider did not complete it."
        await self._close_to_comparing(
            attempt,
            AttemptStatus.FAILED,
            "change_failed",
            f"I could not {verb} {_describe_booking(approval)}. {why} {_shows_as(booking)}",
            recorder,
            reason,
        )

    async def _close_slot_taken(
        self, attempt: BookingAttempt, approval: Approval, recorder: RunRecorder
    ) -> None:
        others = await self._workflows.other_slots(
            attempt.workflow_id, attempt.provider_id, attempt.slot
        )
        listed = "; ".join(slot_label(slot) for slot in others)
        offer = f" Other open slots with this provider: {listed}." if listed else ""
        await self._close_to_comparing(
            attempt,
            AttemptStatus.SLOT_TAKEN,
            "booking_slot_taken",
            f"Someone else took {_describe(approval)}, so nothing was booked.{offer} "
            "Pick a slot and I will ask you to confirm it.",
            recorder,
        )

    async def _close_failed(
        self, attempt: BookingAttempt, approval: Approval, reason: str, recorder: RunRecorder
    ) -> None:
        await self._close_to_comparing(
            attempt,
            AttemptStatus.FAILED,
            "booking_failed",
            f"I could not complete the booking with {_describe(approval)}. "
            "The provider shows no active booking. Pick another slot, or ask me to try again.",
            recorder,
            reason,
        )

    async def _close_to_comparing(
        self,
        attempt: BookingAttempt,
        status: AttemptStatus,
        kind: str,
        text: str,
        recorder: RunRecorder,
        reason: str | None = None,
    ) -> None:
        moves = []
        async with self._db.session_scope() as session:
            closed = await BookingRepository(session).close_attempt(
                attempt.id, status, self._clock.now(), last_result=reason or status.value
            )
            if not closed:
                return
            moves = await self._workflows.move_in(
                session, attempt.workflow_id, S.VERIFYING, S.COMPARING
            )
            await self._notifications.add_in(
                session,
                attempt.user_id,
                attempt.session_id,
                kind,
                text,
                {"run_id": str(attempt.run_id)},
            )
        await record_moves(recorder, moves)
        await recorder.event(kind, data={"attempt_id": str(attempt.id), "reason": reason})
        await self._traces.settle_run(recorder, "needs_input", text)

    async def _ask_again(
        self,
        attempt: BookingAttempt,
        old: Approval,
        status: AttemptStatus,
        context: CallContext,
        recorder: RunRecorder,
    ) -> None:
        """The old yes no longer applies. Take a new hold, then a fresh approval is the only path.

        The hold comes first. If this worker dies before the commit, the attempt is still open, and
        the next one asks for the slot again, which replaces this hold.
        """
        hold = await self._gateway.call(
            "hold_slot", {"provider_id": old.provider_id, "slot": old.slot}, context
        )
        if hold.status == ToolStatus.TIMEOUT or hold.ambiguous:
            await self._not_confirmed(attempt, old, recorder)
            return
        if not hold.ok:
            await self._close_slot_taken(attempt, old, recorder)
            return

        approval = self._approvals.approval_from_hold(
            old.user_id,
            old.session_id,
            old.workflow_id,
            attempt.run_id,
            old.provider_name,
            old.service,
            old.max_budget,
            hold,
        )
        card = card_of(approval)
        if status == AttemptStatus.PRICE_CHANGED:
            kind = "booking_price_changed"
            text = (
                f"The price for {_describe(old)} changed from "
                f"{money(old.price, old.currency)} to {money(card.price, card.currency)}. "
                "Nothing was booked. Please check the new card and confirm."
            )
        else:
            kind = "booking_hold_expired"
            text = (
                f"The hold on {_describe(old)} ran out before the booking went through, so nothing "
                f"was booked. I took a new hold at {money(card.price, card.currency)}. "
                "Please check the new card and confirm."
            )

        moves = []
        async with self._db.session_scope() as session:
            repository = BookingRepository(session)
            if not await repository.close_attempt(
                attempt.id, status, self._clock.now(), last_result=status.value
            ):
                return
            moves = await self._workflows.move_in(
                session, attempt.workflow_id, S.VERIFYING, S.COMPARING
            )
            moves += await self._approvals.write_approval_in(session, approval)
            await self._notifications.add_in(
                session,
                attempt.user_id,
                attempt.session_id,
                kind,
                text,
                {"approval": card.model_dump(mode="json"), "run_id": str(attempt.run_id)},
            )
        await record_moves(recorder, moves)
        await recorder.event(
            kind, data={"attempt_id": str(attempt.id), "new_approval_id": str(approval.id)}
        )
        await self._traces.settle_run(recorder, "awaiting_approval", text)
