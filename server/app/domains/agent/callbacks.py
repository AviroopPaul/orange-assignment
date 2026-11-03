"""ADK callbacks that report into the current run's trace."""

import time
from contextvars import ContextVar
from dataclasses import dataclass

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from app.domains.gateway.service import CallContext, ToolBudgetExhausted, ToolGateway
from app.domains.tracing.service import ModelUsage, RunRecorder


@dataclass
class TurnTelemetry:
    recorder: RunRecorder
    model_name: str
    gateway: ToolGateway
    tool_context: CallContext
    model_tool_names: frozenset[str]
    call_started_at: float = 0.0


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
    return None


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
