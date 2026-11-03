"""ADK callbacks that report into the current run's trace."""

import time
from contextvars import ContextVar
from dataclasses import dataclass

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from app.domains.bookings.matching import claimed_actions
from app.domains.bookings.service import ApprovalService
from app.domains.gateway.service import CallContext, ToolBudgetExhausted, ToolGateway
from app.domains.tracing.service import ModelUsage, RunRecorder
from app.domains.workflow.service import WorkflowService

FALSE_CLAIM_REPLY = (
    "I can't confirm that yet. I only say something is booked or changed "
    "once the provider has verified it."
)


@dataclass
class TurnTelemetry:
    recorder: RunRecorder
    model_name: str
    gateway: ToolGateway
    tool_context: CallContext
    model_tool_names: frozenset[str]
    workflow: WorkflowService
    session_id: str
    bookings: ApprovalService
    call_started_at: float = 0.0
    # A statement about a booking is fair once the source of truth was read in this turn.
    read_bookings: bool = False


current_turn: ContextVar[TurnTelemetry | None] = ContextVar("current_turn", default=None)


async def before_model(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> LlmResponse | None:
    turn = current_turn.get()
    if turn is not None:
        turn.call_started_at = time.monotonic()
    return None


async def after_model(
    callback_context: CallbackContext, llm_response: LlmResponse
) -> LlmResponse | None:
    turn = current_turn.get()
    if turn is None:
        return None
    usage = llm_response.usage_metadata
    parts = llm_response.content.parts if llm_response.content else None
    requested_tools = [part.function_call.name for part in parts or [] if part.function_call]
    await turn.recorder.model_call(
        turn.model_name,
        ModelUsage(
            latency_ms=(time.monotonic() - turn.call_started_at) * 1000,
            input_tokens=(usage.prompt_token_count or 0) if usage else 0,
            output_tokens=(
                (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)
                if usage
                else 0
            ),
        ),
        {"requested_tools": requested_tools, "finish_reason": str(llm_response.finish_reason)},
    )
    if requested_tools:
        return None
    return await _block_false_claim(turn, parts, llm_response)


async def _block_false_claim(
    turn: TurnTelemetry, parts: list[types.Part] | None, llm_response: LlmResponse
) -> LlmResponse | None:
    """Success text comes from a template after verification. The model may not write its own."""
    text = "".join(part.text for part in parts or [] if part.text and not part.thought)
    claimed = claimed_actions(text)
    if not claimed:
        return None
    user_id = turn.tool_context.user_id
    # Reading the bookings proves nothing about a change still waiting for a yes or a check.
    unfinished = await turn.bookings.unfinished_actions(user_id, turn.session_id)
    if not claimed & unfinished:
        if turn.read_bookings:
            return None
        if claimed <= await turn.bookings.verified_actions(user_id, turn.session_id):
            return None
    await turn.recorder.event("false_claim_blocked", data={"text": text[:500]})
    return LlmResponse(
        content=types.Content(role="model", parts=[types.Part(text=FALSE_CLAIM_REPLY)]),
        usage_metadata=llm_response.usage_metadata,
        finish_reason=llm_response.finish_reason,
    )


async def on_model_error(
    callback_context: CallbackContext, llm_request: LlmRequest, error: Exception
) -> LlmResponse | None:
    turn = current_turn.get()
    if turn is not None:
        await turn.recorder.event(
            "model_error",
            name=turn.model_name,
            data={"error": type(error).__name__, "detail": str(error)[:500]},
            latency_ms=(time.monotonic() - turn.call_started_at) * 1000,
        )
    return None


async def on_tool_error(
    tool: BaseTool, args: dict, tool_context: ToolContext, error: Exception
) -> dict | None:
    """ADK fires this for a tool the model made up, or one it was never given.

    Both go to the gateway, which refuses, traces and counts them. The model gets the refusal.
    Returning None lets a spent budget end the run.
    """
    if isinstance(error, ToolBudgetExhausted):
        return None
    turn = current_turn.get()
    if turn is None:
        return {"error": "tool_failed", "message": "No action was taken."}
    if tool.name in turn.model_tool_names:
        # A real tool raised. It never reached the gateway's result path, so report it here.
        await turn.recorder.event(
            "tool_error",
            name=tool.name,
            data={"error": type(error).__name__},
        )
        return {"error": "tool_failed", "message": "That tool failed. No action was taken."}
    result = await turn.gateway.call(tool.name, args, turn.tool_context)
    return {"error": result.error_code, "message": result.message}
