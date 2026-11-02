"""A deterministic model, so tests and evals run offline through the real ADK loop."""

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


class ScriptedLlm(BaseLlm):
    """Plays its steps in order, then repeats the last one. An Exception step is raised."""

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
        if isinstance(step, Exception):
            raise step
        part = (
            types.Part(function_call=types.FunctionCall(name=step.name, args=step.args))
            if isinstance(step, ToolCall)
            else types.Part(text=step)
        )
        yield LlmResponse(
            content=types.Content(role="model", parts=[part]),
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=self.input_tokens,
                candidates_token_count=self.output_tokens,
                total_token_count=self.input_tokens + self.output_tokens,
            ),
        )
