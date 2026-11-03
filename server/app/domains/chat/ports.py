"""What the chat domain needs from an agent. Only the adapter knows about ADK."""

from contextlib import AbstractAsyncContextManager
from typing import Protocol

from app.domains.tracing.service import RunRecorder


class ModelCallLimitExceeded(Exception):
    pass


class ToolCallLimitExceeded(Exception):
    pass


class ModelUnavailable(Exception):
    """The model failed or timed out after its retry."""


class SessionConflict(Exception):
    """Something else wrote to the session during this turn."""


class AgentRuntime(Protocol):
    async def run_turn(
        self, user_id: str, session_id: str, message: str, recorder: RunRecorder
    ) -> str:
        """Run one turn and return the model's reply text, which may be empty."""
        ...


class TurnLock(Protocol):
    def hold(self, key: str) -> AbstractAsyncContextManager[None]: ...
