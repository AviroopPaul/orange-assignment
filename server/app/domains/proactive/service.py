"""Finds bookings the provider cancelled, tells the user, and offers replacements.

Runs in the worker, so it never touches an ADK session. The cancel is reported first, in the same
transaction as its row. Replacements come in a second step, so a provider that is down cannot delay
the news. A row left in `detected` is picked up by the next poll, which makes a dead worker safe.

It never holds a slot, because a hold lasts only 10 minutes and the user may be away. Rebooking goes
through the normal card, so it needs a fresh approval.
"""

import asyncio
import logging
from datetime import date
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.core.clock import LOCAL_TZ, Clock
from app.core.database import Database
from app.core.locks import RedisTurnLock, TurnLockTimeout, TurnLockUnavailable
from app.domains.bookings.models import BookingAttempt
from app.domains.bookings.service import ApprovalService, money, slot_label
from app.domains.gateway.schemas import Caller
from app.domains.gateway.service import CallContext, ToolBudget, ToolGateway
from app.domains.notifications.service import NotificationService
from app.domains.proactive.models import BookingDisruption, DisruptionStatus
from app.domains.proactive.policy import (
    MAX_DAYS_SHOWN,
    Reason,
    best_options,
    decide,
    replacement_search,
)
from app.domains.proactive.repository import ProactiveRepository
from app.domains.tracing.service import RunRecorder, TraceService
from app.domains.workflow.filters import apply_hard_limits, build_shortlist
from app.domains.workflow.request import CHOSEN_PROVIDER
from app.domains.workflow.service import WorkflowService, record_moves

logger = logging.getLogger(__name__)

CANCEL_NOTICE = "provider_cancelled"
REPLACEMENT_NOTICE = "replacement_options"
MAX_SEARCH_FAILURES = 3
ROWS_PER_POLL = 20
POLL_TOOL_BUDGET = 1
# The gateway retries reads itself, and each retry spends from this.
SEARCH_TOOL_BUDGET = 5
SETTLED_BY = "proactive"
NOTHING_DONE = "Nothing is held or booked."


def _describe_option(option: dict[str, Any]) -> str:
    days = ", ".join(option["open_days"][:MAX_DAYS_SHOWN])
    return (
        f"{option['name']}, {money(option['price'], option['currency'])}, "
        f"rated {option['rating']}, open {days}"
    )


def options_text(options: list[dict[str, Any]]) -> str:
    listed = "\n".join(f"{i}. {_describe_option(found)}" for i, found in enumerate(options, 1))
    return (
        f"These replacements fit your earlier request:\n{listed}\n{NOTHING_DONE} "
        "Tell me which one you want and the day, and I will show you a card to confirm."
    )


_NO_OPTIONS_TEXT = {
    Reason.NO_REPLACEMENT_FITS: "I found no replacement that fits your earlier request. "
    "Tell me what I can change, such as the days, the time or the budget.",
    Reason.NO_PREFERENCES: "I do not have your area and days for this booking. "
    "Tell me both if you want a replacement.",
    Reason.DATES_PASSED: "The days in your earlier request have passed. "
    "Tell me which days work now if you want a replacement.",
    Reason.SEARCH_FAILED: "I could not reach the provider to look for a replacement. "
    "Ask me again in a little while.",
}
_ERRAND_IN_PROGRESS_TEXT = (
    "You have another request open in this chat, so I left it as it is. "
    "Ask me when you want a replacement."
)


class ProactiveService:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        gateway: ToolGateway,
        bookings: ApprovalService,
        workflows: WorkflowService,
        notifications: NotificationService,
        traces: TraceService,
        turn_lock: RedisTurnLock,
        time_limit_s: float,
    ) -> None:
        self._db = db
        self._clock = clock
        self._gateway = gateway
        self._bookings = bookings
        self._workflows = workflows
        self._notifications = notifications
        self._traces = traces
        self._turn_lock = turn_lock
        self._time_limit_s = time_limit_s

    def _today(self) -> date:
        return self._clock.now().astimezone(LOCAL_TZ).date()

    async def poll(self) -> None:
        """Called on a schedule. Anything not finished here is simply seen again by the next one."""
        try:
            async with asyncio.timeout(self._time_limit_s):
                for user_id in await self._bookings.watched_users():
                    await self._check_user(user_id)
                await self._offer_replacements()
        except TimeoutError:
            logger.warning("the provider poll ran out of time")

    # Step 1: compare the provider's list with our records, and report what changed.

    async def _check_user(self, user_id: str) -> None:
        context = CallContext(user_id, Caller.BACKGROUND, ToolBudget(POLL_TOOL_BUDGET))
        listed = await self._gateway.call("list_bookings", {"user_id": user_id}, context)
        if not listed.ok:
            logger.warning("provider poll for %s failed: %s", user_id, listed.status)
            return
        # The provider's list decides, and a repeated entry counts once.
        bookings = {found["booking_id"]: found for found in listed.data.get("bookings", [])}
        on_record = await self._bookings.bookings_on_record(user_id)
        async with self._db.session_scope() as session:
            recorded = await ProactiveRepository(session).recorded_booking_ids(user_id)

        repeats = []
        for booking_id, booking in bookings.items():
            attempt = on_record.get(booking_id)
            decision = decide(
                booking,
                on_record=attempt is not None,
                already_recorded=booking_id in recorded,
                change_running=await self._bookings.change_is_running(booking_id),
            )
            if decision.reason == Reason.DUPLICATE:
                repeats.append(booking_id)
            elif attempt is not None and decision.notify:
                await self._report_cancel(booking, attempt)
            elif attempt is not None and decision.record:
                await self._record_silently(booking, attempt, decision.reason)
        if repeats:
            async with self._db.session_scope() as session:
                await ProactiveRepository(session).note_repeats(repeats)

    def _row_for(
        self,
        booking: dict[str, Any],
        attempt: BookingAttempt,
        status: DisruptionStatus,
        reason: Reason,
    ) -> BookingDisruption:
        now = self._clock.now()
        return BookingDisruption(
            booking_id=booking["booking_id"],
            user_id=attempt.user_id,
            session_id=attempt.session_id,
            workflow_id=attempt.workflow_id,
            provider_id=booking["provider_id"],
            status=status.value,
            reason=reason.value,
            created_at=now,
            updated_at=now,
        )

    async def _record_silently(
        self, booking: dict[str, Any], attempt: BookingAttempt, reason: Reason
    ) -> None:
        try:
            async with self._db.session_scope() as session:
                await ProactiveRepository(session).add(
                    self._row_for(booking, attempt, DisruptionStatus.IGNORED, reason)
                )
        except IntegrityError:
            return
        logger.info("no notice for booking %s: %s", booking["booking_id"], reason.value)

    async def _report_cancel(self, booking: dict[str, Any], attempt: BookingAttempt) -> None:
        recorder = await self._traces.start_run(attempt.user_id, attempt.session_id)
        row = self._row_for(booking, attempt, DisruptionStatus.DETECTED, Reason.PROVIDER_CANCELLED)
        row.run_id = recorder.run_id
        label = slot_label(booking["slot"])
        text = (
            f"{booking['provider_name']} cancelled your {booking['service']} booking for {label}. "
            "Nothing has been rebooked. I am looking for replacements."
        )
        try:
            async with self._db.session_scope() as session:
                await ProactiveRepository(session).add(row)
                closed_cards, moves = await self._bookings.close_cards_for_booking_in(
                    session, booking["booking_id"]
                )
                await self._notifications.add_in(
                    session,
                    attempt.user_id,
                    attempt.session_id,
                    CANCEL_NOTICE,
                    text,
                    {
                        "run_id": str(recorder.run_id),
                        "proactive": True,
                        "booking_id": booking["booking_id"],
                        "closed_cards": closed_cards,
                    },
                )
        except IntegrityError:
            # Another poll got here first and told the user.
            await self._traces.settle_run(
                recorder, Reason.DUPLICATE.value, "A second poll saw the same cancel.", SETTLED_BY
            )
            return
        await record_moves(recorder, moves)
        await recorder.event(
            "provider_cancel_detected",
            name=Reason.PROVIDER_CANCELLED.value,
            data={
                "source": "list_bookings",
                "booking_id": booking["booking_id"],
                "provider_id": booking["provider_id"],
                "slot": booking["slot"],
                "status": booking["status"],
                "when": booking["when"],
                "notified": True,
                "closed_cards": closed_cards,
            },
        )

    # Step 2: look for replacements within the user's limits. Nothing here holds a slot.

    async def _offer_replacements(self) -> None:
        async with self._db.session_scope() as session:
            rows = await ProactiveRepository(session).detected(ROWS_PER_POLL)
        for row in rows:
            await self._offer(row)

    async def _offer(self, row: BookingDisruption) -> None:
        assert row.run_id is not None
        recorder = await self._traces.resume_run(row.run_id)
        request = await self._workflows.request_of(row.workflow_id)
        search = replacement_search(request, self._today())
        if isinstance(search, Reason):
            # With no saved request there is no errand to reopen.
            await self._close(row, recorder, search, {}, reopen=search != Reason.NO_PREFERENCES)
            return

        context = CallContext(
            row.user_id, Caller.BACKGROUND, ToolBudget(SEARCH_TOOL_BUDGET), recorder=recorder
        )
        found = await self._gateway.call("search_providers", search, context)
        if not found.ok:
            await self._search_failed(row, recorder)
            return

        # The provider that just cancelled is not offered back to the user.
        providers = [
            provider
            for provider in found.data.get("providers", [])
            if provider.get("id") != row.provider_id
        ]
        # Keeping to a past provider makes no sense once that provider has cancelled.
        request = {key: value for key, value in request.items() if key != CHOSEN_PROVIDER}
        options = best_options(apply_hard_limits(providers, request)["providers"])
        await self._close(
            row,
            recorder,
            Reason.REPLACEMENTS_OFFERED if options else Reason.NO_REPLACEMENT_FITS,
            build_shortlist(providers, request),
            reopen=True,
            options=options,
        )

    async def _search_failed(self, row: BookingDisruption, recorder: RunRecorder) -> None:
        async with self._db.session_scope() as session:
            fresh = await ProactiveRepository(session).get_locked(row.id)
            if fresh is None:
                return
            fresh.search_failures += 1
            failures = fresh.search_failures
        if failures >= MAX_SEARCH_FAILURES:
            await self._close(row, recorder, Reason.SEARCH_FAILED, {}, reopen=True)

    async def _close(
        self,
        row: BookingDisruption,
        recorder: RunRecorder,
        reason: Reason,
        shortlist: dict[str, Any],
        reopen: bool,
        options: list[dict[str, Any]] | None = None,
    ) -> None:
        """A chat turn may be reading this errand. If one is running, the next poll comes back."""
        moves = []
        try:
            async with self._turn_lock.hold(f"{row.user_id}:{row.session_id}", wait_s=0):
                async with self._db.session_scope() as session:
                    fresh = await ProactiveRepository(session).get_locked(row.id)
                    if fresh is None or fresh.status != DisruptionStatus.DETECTED.value:
                        return
                    if reopen:
                        reopened = await self._workflows.reopen_in(
                            session, row.workflow_id, row.user_id, row.session_id, shortlist
                        )
                        if reopened is None:
                            reason = Reason.ERRAND_IN_PROGRESS
                        moves = reopened or []
                    fresh.status = DisruptionStatus.CLOSED.value
                    fresh.reason = reason.value
                    fresh.updated_at = self._clock.now()
                    text = self._closing_text(reason, options or [])
                    await self._notifications.add_in(
                        session,
                        row.user_id,
                        row.session_id,
                        REPLACEMENT_NOTICE,
                        text,
                        {"run_id": str(recorder.run_id), "proactive": True},
                    )
        except (TurnLockTimeout, TurnLockUnavailable):
            return
        await record_moves(recorder, moves)
        await recorder.event(
            "replacement_decision",
            name=reason.value,
            data={
                "offered": [found["id"] for found in options or []],
                "excluded_provider": row.provider_id,
                "holds_placed": 0,
            },
        )
        await self._traces.settle_run(recorder, "needs_input", text, SETTLED_BY)

    def _closing_text(self, reason: Reason, options: list[dict[str, Any]]) -> str:
        if reason == Reason.ERRAND_IN_PROGRESS:
            return _ERRAND_IN_PROGRESS_TEXT
        if reason == Reason.REPLACEMENTS_OFFERED:
            return options_text(options)
        return _NO_OPTIONS_TEXT[reason]
