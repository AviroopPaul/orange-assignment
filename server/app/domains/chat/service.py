"""The turn controller: runs one user turn inside hard limits and always ends with a clear reply."""

import asyncio
import logging
import uuid
from math import ceil

from app.core.locks import TurnLockTimeout, TurnLockUnavailable
from app.domains.chat.ports import (
    AgentRuntime,
    ModelCallLimitExceeded,
    ModelRateLimited,
    ModelUnavailable,
    SessionConflict,
    ToolCallLimitExceeded,
    TurnLock,
)
from app.domains.chat.schemas import ChatRequest, ChatResponse, FailureReason, Outcome
from app.domains.tracing.service import RunRecorder, TraceService
from app.domains.workflow.dates import date_question, is_asked_alone
from app.domains.workflow.schemas import RequestSnapshot
from app.domains.workflow.service import WorkflowService

logger = logging.getLogger(__name__)

FAILURE_REPLIES: dict[FailureReason, tuple[Outcome, str]] = {
    FailureReason.MODEL_NOT_CONFIGURED: (
        Outcome.TOOL_FAILED,
        "I can't reply yet because no Gemini API key is set. "
        "Set GOOGLE_API_KEY and restart the service.",
    ),
    FailureReason.MODEL_UNAVAILABLE: (
        Outcome.TOOL_FAILED,
        "The AI model is not responding right now. Nothing was changed. Please try again shortly.",
    ),
    FailureReason.MODEL_RATE_LIMITED: (
        Outcome.LIMIT_REACHED,
        "The AI model is busy because we hit its usage limit. Nothing was changed. "
        "Please try again shortly.",
    ),
    FailureReason.MODEL_CALL_LIMIT: (
        Outcome.LIMIT_REACHED,
        "I reached my step limit for this message. Nothing was changed. "
        "Please try again, or simplify the request.",
    ),
    FailureReason.TOOL_CALL_LIMIT: (
        Outcome.LIMIT_REACHED,
        "I reached my limit of tool calls for this message. Nothing was changed. "
        "Please try again, or simplify the request.",
    ),
    FailureReason.RUN_DEADLINE: (
        Outcome.LIMIT_REACHED,
        "This took longer than my time limit, so I stopped. Nothing was changed. Please try again.",
    ),
    FailureReason.EMPTY_MODEL_REPLY: (
        Outcome.TOOL_FAILED,
        "I didn't get a usable reply from the AI model. Nothing was changed. Please try again.",
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
        "Nothing was changed. Please try again shortly.",
    ),
    FailureReason.UNEXPECTED_ERROR: (
        Outcome.TOOL_FAILED,
        "Something went wrong on my side. Nothing was changed. Please try again.",
    ),
}


class ChatService:
    def __init__(
        self,
        runtime: AgentRuntime,
        turn_lock: TurnLock,
        traces: TraceService,
        workflows: WorkflowService,
        run_deadline_s: float,
        model_is_configured: bool,
    ) -> None:
        self._runtime = runtime
        self._turn_lock = turn_lock
        self._traces = traces
        self._workflows = workflows
        self._run_deadline_s = run_deadline_s
        self._model_is_configured = model_is_configured

    async def handle_message(self, request: ChatRequest) -> ChatResponse:
        session_id = request.session_id or uuid.uuid4().hex
        recorder = await self._traces.start_run(request.user_id, session_id)

        reply, failure, retry_after_s = await self._run(request, session_id, recorder)
        if failure is not None:
            outcome, reply = FAILURE_REPLIES[failure]
            if retry_after_s is not None:
                reply = reply.replace("shortly", f"in about {retry_after_s} seconds")
        else:
            snapshot = await self._workflows.snapshot(request.user_id, session_id)
            outcome = Outcome.NEEDS_INPUT if snapshot.missing_required else Outcome.DONE
            reply = self._settle_date_question(reply, snapshot)
            if snapshot.pending_dates:
                await self._workflows.note_date_question(
                    request.user_id,
                    session_id,
                    is_asked_alone(reply, snapshot.pending_dates["shown"]),
                )

        await self._traces.finish_run(
            recorder, outcome.value, failure.value if failure else None, reply
        )
        return ChatResponse(
            run_id=recorder.run_id,
            session_id=session_id,
            outcome=outcome,
            reason=failure,
            retry_after_s=retry_after_s,
            reply=reply,
        )

    def _settle_date_question(self, reply: str, snapshot: RequestSnapshot) -> str:
        """The user must see the exact dates and be asked about them, whatever the model wrote."""
        pending = snapshot.pending_dates
        if pending is None:
            return reply
        shown, question = pending["shown"], date_question(pending["label"])
        if is_asked_alone(reply, shown):
            return reply
        if pending.get("unclear"):
            # Their answer was not clear, so this time the dates are the only thing asked.
            return question
        if all(day in reply for day in shown) and "?" in reply:
            return reply
        return f"{reply}\n\n{question}"

    async def _run(
        self, request: ChatRequest, session_id: str, recorder: RunRecorder
    ) -> tuple[str, FailureReason | None, int | None]:
        if not self._model_is_configured:
            return "", FailureReason.MODEL_NOT_CONFIGURED, None

        user_id = request.user_id
        try:
            async with self._turn_lock.hold(f"{user_id}:{session_id}"):
                await self._workflows.record_user_message(
                    user_id, session_id, request.message, recorder
                )
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
        except Exception as error:
            logger.exception("unexpected error in run %s", recorder.run_id)
            await recorder.event("unexpected_error", data={"error": repr(error)[:500]})
            return "", FailureReason.UNEXPECTED_ERROR, None

        if not reply.strip():
            return "", FailureReason.EMPTY_MODEL_REPLY, None
        return reply, None, None
