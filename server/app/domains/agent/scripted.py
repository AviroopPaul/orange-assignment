"""A deterministic model, so tests and evals run offline through the real ADK loop."""

import asyncio
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, field
from typing import Any

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import ConfigDict, Field


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Pause:
    """Holds the model call open and then plays `then`, so a test can act during a turn."""

    seconds: float
    then: Any = "Scripted reply."


class ScriptedLlm(BaseLlm):
    """Plays its steps in order, then repeats the last one.

    A step is text, a ToolCall, a list of ToolCalls made at once, or a Pause. An Exception step is
    raised.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    script: Sequence[Any] = Field(default_factory=lambda: ["Scripted reply."])
    requests: list[LlmRequest] = Field(default_factory=list)
    input_tokens: int = 10
    output_tokens: int = 5

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        step = self.script[min(len(self.requests), len(self.script) - 1)]
        self.requests.append(llm_request)
        while isinstance(step, Pause):
            await asyncio.sleep(step.seconds)
            step = step.then
        if isinstance(step, Exception):
            raise step
        calls = step if isinstance(step, list) else [step]
        parts = [
            types.Part(function_call=types.FunctionCall(name=call.name, args=call.args))
            if isinstance(call, ToolCall)
            else types.Part(text=call)
            for call in calls
        ]
        yield LlmResponse(
            content=types.Content(role="model", parts=parts),
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=self.input_tokens,
                candidates_token_count=self.output_tokens,
                total_token_count=self.input_tokens + self.output_tokens,
            ),
        )
