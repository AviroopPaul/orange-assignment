"""The turn controller: runs one user turn inside hard limits and always ends with a clear reply."""

import asyncio
import logging
import uuid

from app.core.locks import TurnLockTimeout, TurnLockUnavailable
from app.domains.chat.ports import (
    AgentRuntime,
    ModelCallLimitExceeded,
    ModelUnavailable,
    SessionConflict,
    ToolCallLimitExceeded,
    TurnLock,
)
from app.domains.chat.schemas import ChatRequest, ChatResponse, FailureReason, Outcome
from app.domains.tracing.service import RunRecorder, TraceService

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
        run_deadline_s: float,
        model_is_configured: bool,
    ) -> None:
        self._runtime = runtime
        self._turn_lock = turn_lock
        self._traces = traces
        self._run_deadline_s = run_deadline_s
        self._model_is_configured = model_is_configured

    async def handle_message(self, request: ChatRequest) -> ChatResponse:
        session_id = request.session_id or uuid.uuid4().hex
        recorder = await self._traces.start_run(request.user_id, session_id)

        reply, failure = await self._run(request, session_id, recorder)
        outcome = Outcome.DONE
        if failure is not None:
            outcome, reply = FAILURE_REPLIES[failure]

        await self._traces.finish_run(
            recorder, outcome.value, failure.value if failure else None, reply
        )
        return ChatResponse(
            run_id=recorder.run_id,
            session_id=session_id,
            outcome=outcome,
            reason=failure,
            reply=reply,
        )

    async def _run(
        self, request: ChatRequest, session_id: str, recorder: RunRecorder
    ) -> tuple[str, FailureReason | None]:
        if not self._model_is_configured:
            return "", FailureReason.MODEL_NOT_CONFIGURED

        try:
            async with self._turn_lock.hold(f"{request.user_id}:{session_id}"):
                async with asyncio.timeout(self._run_deadline_s):
                    reply = await self._runtime.run_turn(
                        request.user_id, session_id, request.message, recorder
                    )
        except TurnLockTimeout:
            return "", FailureReason.SESSION_BUSY
        except TurnLockUnavailable:
            return "", FailureReason.LOCK_UNAVAILABLE
        except TimeoutError:
            return "", FailureReason.RUN_DEADLINE
        except ModelCallLimitExceeded:
            return "", FailureReason.MODEL_CALL_LIMIT
        except ToolCallLimitExceeded:
            return "", FailureReason.TOOL_CALL_LIMIT
        except ModelUnavailable as error:
            logger.warning("model unavailable: %s", error)
            await recorder.event("model_unavailable", data={"error": str(error)[:500]})
            return "", FailureReason.MODEL_UNAVAILABLE
        except SessionConflict:
            return "", FailureReason.SESSION_CONFLICT
        except Exception as error:
            logger.exception("unexpected error in run %s", recorder.run_id)
            await recorder.event("unexpected_error", data={"error": repr(error)[:500]})
            return "", FailureReason.UNEXPECTED_ERROR

        if not reply.strip():
            return "", FailureReason.EMPTY_MODEL_REPLY
        return reply, None
