"""ADK callbacks that report into the current run's trace."""

import time
from contextvars import ContextVar
from dataclasses import dataclass

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from app.domains.tracing.service import ModelUsage, RunRecorder


@dataclass
class TurnTelemetry:
    recorder: RunRecorder
    model_name: str
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
) -> dict:
    """ADK fires this for a tool the model made up. The model gets the error and nothing runs."""
    turn = current_turn.get()
    if turn is not None:
        await turn.recorder.event(
            "tool_refused",
            name=tool.name,
            data={"args": args, "error": type(error).__name__},
        )
    return {"error": f"Tool '{tool.name}' is not available. No action was taken."}
