import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

IDENTIFIER_PATTERN = r"^[A-Za-z0-9_.\-]{1,64}$"


class Outcome(StrEnum):
    """How a run ended. Every exit path maps to exactly one of these."""

    DONE = "done"
    NEEDS_INPUT = "needs_input"
    AWAITING_APPROVAL = "awaiting_approval"
    VERIFYING = "verifying"
    LIMIT_REACHED = "limit_reached"
    TOOL_FAILED = "tool_failed"


class FailureReason(StrEnum):
    MODEL_NOT_CONFIGURED = "model_not_configured"
    MODEL_UNAVAILABLE = "model_unavailable"
    MODEL_CALL_LIMIT = "model_call_limit"
    TOOL_CALL_LIMIT = "tool_call_limit"
    RUN_DEADLINE = "run_deadline"
    EMPTY_MODEL_REPLY = "empty_model_reply"
    SESSION_BUSY = "session_busy"
    SESSION_CONFLICT = "session_conflict"
    LOCK_UNAVAILABLE = "lock_unavailable"
    UNEXPECTED_ERROR = "unexpected_error"


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(pattern=IDENTIFIER_PATTERN)
    session_id: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)
    message: str = Field(min_length=1, max_length=4000)


class ChatResponse(BaseModel):
    run_id: uuid.UUID
    session_id: str
    outcome: Outcome
    reason: FailureReason | None = None
    reply: str
