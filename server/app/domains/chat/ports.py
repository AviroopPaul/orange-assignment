"""What the chat domain needs from an agent. Only the adapter knows about ADK."""

from contextlib import AbstractAsyncContextManager
from typing import Protocol

from app.domains.tracing.service import RunRecorder


class ModelCallLimitExceeded(Exception):
    pass


class ToolCallLimitExceeded(Exception):
    pass


class ModelRateLimited(Exception):
    """The model's usage limit was hit, and the wait is too long to do inside this turn."""

    def __init__(self, retry_after_s: float | None) -> None:
        super().__init__(f"rate limited, retry after {retry_after_s} s")
        self.retry_after_s = retry_after_s


class ModelUnavailable(Exception):
    """The model failed or timed out after its retry."""


class SessionConflict(Exception):
    """Something else wrote to the session during this turn."""


class ContextUnavailable(Exception):
    """What the model would see could not be built within budget, or could not be logged."""


class AgentRuntime(Protocol):
    async def run_turn(
        self, user_id: str, session_id: str, message: str, recorder: RunRecorder
    ) -> str:
        """Run one turn and return the model's reply text, which may be empty."""
        ...

    async def rotate_session(self, user_id: str, session_id: str) -> None:
        """Delete the session's stored log. The caller holds the turn lock."""
        ...


class TurnLock(Protocol):
    def hold(self, key: str) -> AbstractAsyncContextManager[None]: ...
