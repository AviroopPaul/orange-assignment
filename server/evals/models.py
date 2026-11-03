"""One model object for the agent, so each scenario can plug in its own script."""

from collections.abc import AsyncGenerator
from typing import Any

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from pydantic import ConfigDict


class Switchboard(BaseLlm):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    active: Any = None

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        async for response in self.active.generate_content_async(llm_request, stream):
            yield response
