"""The turn controller: runs one user turn inside hard limits and always ends with a clear reply."""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from math import ceil

from sqlalchemy.exc import SQLAlchemyError

from app.core.locks import TurnLockTimeout, TurnLockUnavailable
from app.domains.bookings.schemas import ApprovalCard, ApprovalDecision
from app.domains.bookings.service import ApprovalService
from app.domains.chat.ports import (
    AgentRuntime,
    ContextUnavailable,
    ModelCallLimitExceeded,
    ModelRateLimited,
    ModelUnavailable,
    SessionConflict,
    ToolCallLimitExceeded,
    TurnLock,
)
from app.domains.chat.schemas import ChatRequest, ChatResponse, FailureReason, Outcome
from app.domains.memory.schemas import ChatMemoryNotes
from app.domains.memory.service import ASSISTANT, USER, MemoryService
from app.domains.tracing.service import RunRecorder, TraceService
from app.domains.workflow.schemas import RequestSnapshot
from app.domains.workflow.service import WorkflowService

logger = logging.getLogger(__name__)

# True for this message alone. A booking already running is reported instead, in `_failure_reply`.
NO_CHANGE = "This message did not make or change any booking."
BOOKING_RUNNING = "Your earlier booking request is still in progress, and it is not confirmed yet."
CARD_REMINDER = "Please check the details on the card and confirm, or tell me what to change."
_FIELD_NAMES = {
    "service": "the service you need",
    "area": "your area",
    "start_date": "the dates",
    "end_date": "the dates",
}

FAILURE_REPLIES: dict[FailureReason, tuple[Outcome, str]] = {
    FailureReason.MODEL_NOT_CONFIGURED: (
        Outcome.TOOL_FAILED,
        "I can't reply yet because no Gemini API key is set. "
        "Set GOOGLE_API_KEY and restart the service.",
    ),
    FailureReason.MODEL_UNAVAILABLE: (
        Outcome.TOOL_FAILED,
        f"The AI model is not responding right now. {NO_CHANGE} Please try again shortly.",
    ),
    FailureReason.MODEL_RATE_LIMITED: (
        Outcome.LIMIT_REACHED,
        f"The AI model is busy because we hit its usage limit. {NO_CHANGE} "
        "Please try again shortly.",
    ),
    FailureReason.MODEL_CALL_LIMIT: (
        Outcome.LIMIT_REACHED,
        f"I reached my step limit for this message. {NO_CHANGE} "
        "Please try again, or simplify the request.",
    ),
    FailureReason.TOOL_CALL_LIMIT: (
        Outcome.LIMIT_REACHED,
        f"I reached my limit of tool calls for this message. {NO_CHANGE} "
        "Please try again, or simplify the request.",
    ),
    FailureReason.RUN_DEADLINE: (
        Outcome.LIMIT_REACHED,
        f"This took longer than my time limit, so I stopped. {NO_CHANGE} Please try again.",
    ),
    FailureReason.EMPTY_MODEL_REPLY: (
        Outcome.TOOL_FAILED,
        f"I didn't get a usable reply from the AI model. {NO_CHANGE} Please try again.",
    ),
    FailureReason.SESSION_BUSY: (
        Outcome.LIMIT_REACHED,
        "I'm still working on your previous message. Please wait, then send this one again.",
    ),
    FailureReason.SESSION_CONFLICT: (
        Outcome.TOOL_FAILED,
        "This chat changed while I was working on your message. Please send it again.",
    ),
    FailureReason.LOCK_UNAVAILABLE: (
        Outcome.TOOL_FAILED,
        "A part of the service is down, so I can't safely process messages. "
        f"{NO_CHANGE} Please try again shortly.",
    ),
    FailureReason.CONTEXT_UNAVAILABLE: (
        Outcome.TOOL_FAILED,
        f"I couldn't safely prepare this request for the AI model. {NO_CHANGE} Please try again.",
    ),
    FailureReason.UNEXPECTED_ERROR: (
        Outcome.TOOL_FAILED,
        f"Something went wrong on my side. {NO_CHANGE} Please try again.",
    ),
}


@dataclass
class _TurnNotes:
    """Whether the user's message reached the chat history, so its reply belongs there too."""

    recorded: bool = False


class ChatService:
    def __init__(
        self,
        runtime: AgentRuntime,
        turn_lock: TurnLock,
        traces: TraceService,
        workflows: WorkflowService,
        bookings: ApprovalService,
        run_deadline_s: float,
        model_is_configured: bool,
        memory: MemoryService,
    ) -> None:
        self._runtime = runtime
        self._turn_lock = turn_lock
        self._traces = traces
        self._workflows = workflows
        self._bookings = bookings
        self._run_deadline_s = run_deadline_s
        self._model_is_configured = model_is_configured
        self._memory = memory

    async def handle_message(self, request: ChatRequest) -> ChatResponse:
        session_id = request.session_id or uuid.uuid4().hex
        recorder = await self._traces.start_run(request.user_id, session_id)

        card: ApprovalCard | None = None
        notes = _TurnNotes()
        result = await self._run(request, session_id, recorder, notes)
        if isinstance(result, ApprovalDecision):
            reply, failure, retry_after_s = result.reply, None, None
            outcome, card = Outcome(result.outcome), result.approval
        else:
            reply, failure, retry_after_s = result
            if failure == FailureReason.EMPTY_MODEL_REPLY:
                recovered = await self._reply_from_state(request.user_id, session_id, recorder)
                if recovered is not None:
                    reply, failure = recovered, None
            if failure is not None:
                outcome, reply, card = await self._failure_reply(
                    failure, retry_after_s, request.user_id, session_id, recorder
                )
            else:
                snapshot = await self._workflows.snapshot(request.user_id, session_id)
                outcome = Outcome.NEEDS_INPUT if snapshot.missing_required else Outcome.DONE
                reply = self._with_dates_shown(reply, snapshot)
                if snapshot.dates_to_show:
                    await self._workflows.note_dates_shown(
                        request.user_id, session_id, snapshot.dates_to_show["label"]
                    )
                view = await self._bookings.view(request.user_id, session_id, recorder)
                card = view.card
                if card is not None:
                    outcome = Outcome.AWAITING_APPROVAL
                elif view.attempt_open:
                    outcome = Outcome.VERIFYING

        await self._traces.finish_run(
            recorder, outcome.value, failure.value if failure else None, reply
        )
        memory = await self._finish_turn_memory(request.user_id, session_id, reply, notes)
        # Last, so the saga starts writing to this run's trace only after the run is finished.
        await self._bookings.publish()
        return ChatResponse(
            run_id=recorder.run_id,
            session_id=session_id,
            outcome=outcome,
            reason=failure,
            retry_after_s=retry_after_s,
            reply=reply,
            approval=card,
            memory=memory,
        )

    async def _finish_turn_memory(
        self, user_id: str, session_id: str, reply: str, notes: _TurnNotes
    ) -> ChatMemoryNotes:
        """A reply is already decided, so a failure here must not change it."""
        try:
            if notes.recorded:
                await self._memory.record_turn(user_id, session_id, ASSISTANT, reply)
            await self.settle_memory_changes(user_id)
            return await self._memory.take_chat_notes(user_id, session_id)
        except (SQLAlchemyError, OSError):
            logger.exception("could not store the reply or finish a memory change")
            return ChatMemoryNotes()

    async def settle_memory_changes(self, user_id: str) -> None:
        """Clean the last copies of removed text. A session in use is deleted on its next turn."""
        await self._memory.finish_scrub(user_id)
        for session_id in await self._memory.rotations_due(user_id):
            try:
                async with self._turn_lock.hold(f"{user_id}:{session_id}"):
                    await self._rotate(user_id, session_id)
            except (TurnLockTimeout, TurnLockUnavailable):
                logger.warning("session %s stays marked for deletion", session_id)

    async def _rotate(self, user_id: str, session_id: str) -> None:
        await self._runtime.rotate_session(user_id, session_id)
        await self._memory.clear_rotation(user_id, session_id)

    async def _failure_reply(
        self,
        failure: FailureReason,
        retry_after_s: int | None,
        user_id: str,
        session_id: str,
        recorder: RunRecorder,
    ) -> tuple[Outcome, str, ApprovalCard | None]:
        """A failed turn must not lose an open card, or deny a booking that is really running."""
        outcome, reply = FAILURE_REPLIES[failure]
        if retry_after_s is not None:
            reply = reply.replace("shortly", f"in about {retry_after_s} seconds")
        try:
            view = await self._bookings.view(user_id, session_id, recorder)
        except (SQLAlchemyError, OSError):
            logger.warning("could not read booking state for a failed turn")
            return outcome, reply, None
        if view.attempt_open:
            reply = reply.replace(NO_CHANGE, BOOKING_RUNNING)
        return outcome, reply, view.card

    async def _reply_from_state(
        self, user_id: str, session_id: str, recorder: RunRecorder
    ) -> str | None:
        """The model's tools ran, but it wrote nothing. Code knows what to ask next."""
        snapshot = await self._workflows.snapshot(user_id, session_id)
        view = await self._bookings.view(user_id, session_id, recorder)
        reply = None
        if view.card is not None:
            reply = CARD_REMINDER
        elif view.attempt_open:
            reply = BOOKING_RUNNING
        elif snapshot.details and snapshot.missing_required:
            needed = dict.fromkeys(_FIELD_NAMES[name] for name in snapshot.missing_required)
            reply = f"Thanks, I saved that. I still need {' and '.join(needed)}."
        if reply is not None:
            await recorder.event("empty_reply_recovered", data={"reply": reply})
        return reply

    def _with_dates_shown(self, reply: str, snapshot: RequestSnapshot) -> str:
        """Dates are saved with no question, so the user must see what was understood."""
        saved = snapshot.dates_to_show
        if saved is None or all(day in reply for day in saved["shown"]):
            return reply
        return f"Dates: {saved['label']}.\n\n{reply}"

    async def _run(
        self, request: ChatRequest, session_id: str, recorder: RunRecorder, notes: _TurnNotes
    ) -> tuple[str, FailureReason | None, int | None] | ApprovalDecision:
        if not self._model_is_configured:
            return "", FailureReason.MODEL_NOT_CONFIGURED, None

        user_id = request.user_id
        try:
            async with self._turn_lock.hold(f"{user_id}:{session_id}"):
                if session_id in await self._memory.rotations_due(user_id):
                    await self._rotate(user_id, session_id)
                await self._workflows.record_user_message(
                    user_id, session_id, request.message, recorder
                )
                await self._memory.record_turn(user_id, session_id, USER, request.message)
                notes.recorded = True
                # Read before the model runs, so nothing the model writes can count as approval.
                decision = await self._bookings.decide_from_message(
                    user_id, session_id, request.message, recorder
                )
                if decision is not None:
                    return decision
                answered = await self._memory.answer_from_message(
                    user_id, session_id, request.message, recorder
                )
                if answered is not None:
                    return answered, None, None
                try:
                    async with asyncio.timeout(self._run_deadline_s):
                        reply = await self._runtime.run_turn(
                            user_id, session_id, request.message, recorder
                        )
                except (TimeoutError, ModelCallLimitExceeded, ToolCallLimitExceeded):
                    await self._workflows.mark_limit_reached(user_id, session_id, recorder)
                    raise
        except TurnLockTimeout:
            return "", FailureReason.SESSION_BUSY, None
        except TurnLockUnavailable:
            return "", FailureReason.LOCK_UNAVAILABLE, None
        except TimeoutError:
            return "", FailureReason.RUN_DEADLINE, None
        except ModelCallLimitExceeded:
            return "", FailureReason.MODEL_CALL_LIMIT, None
        except ToolCallLimitExceeded:
            return "", FailureReason.TOOL_CALL_LIMIT, None
        except ModelRateLimited as error:
            retry_after_s = None if error.retry_after_s is None else ceil(error.retry_after_s)
            logger.warning("model rate limited, retry after %s s", retry_after_s)
            await recorder.event("model_rate_limited", data={"retry_after_s": retry_after_s})
            return "", FailureReason.MODEL_RATE_LIMITED, retry_after_s
        except ModelUnavailable as error:
            logger.warning("model unavailable: %s", error)
            await recorder.event("model_unavailable", data={"error": str(error)[:500]})
            return "", FailureReason.MODEL_UNAVAILABLE, None
        except SessionConflict:
            return "", FailureReason.SESSION_CONFLICT, None
        except ContextUnavailable as error:
            logger.warning("context unavailable: %s", error)
            await recorder.event("context_unavailable", data={"error": str(error)[:500]})
            return "", FailureReason.CONTEXT_UNAVAILABLE, None
        except Exception as error:
            logger.exception("unexpected error in run %s", recorder.run_id)
            await recorder.event("unexpected_error", data={"error": repr(error)[:500]})
            return "", FailureReason.UNEXPECTED_ERROR, None

        if not reply.strip():
            return "", FailureReason.EMPTY_MODEL_REPLY, None
        return reply, None, None
