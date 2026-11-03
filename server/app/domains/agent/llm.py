"""Builds the model from settings. Evals swap the model here and nowhere else."""

import asyncio
import re
from collections.abc import AsyncGenerator

from google.adk.models.base_llm import BaseLlm
from google.adk.models.google_llm import Gemini
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import errors as genai_errors
from google.genai import types

from app.core.config import Settings
from app.domains.agent.callbacks import current_turn
from app.domains.agent.scripted import ScriptedLlm

# 429 is left out: an immediate retry only spends another request. RateLimitAwareGemini waits.
RETRYABLE_STATUS_CODES = [503, 504]
DEFAULT_RATE_LIMIT_WAIT_S = 1.0


def retry_delay_s(error: genai_errors.APIError) -> float | None:
    """The wait Gemini asks for after a 429, or None if it did not say."""
    details = error.details if isinstance(error.details, dict) else {}
    body = details.get("error", details)
    for item in body.get("details", []) if isinstance(body, dict) else []:
        delay = item.get("retryDelay") if isinstance(item, dict) else None
        if isinstance(delay, str) and delay.endswith("s"):
            try:
                return float(delay[:-1])
            except ValueError:
                continue
    match = re.search(r"retry in ([\d.]+)\s*s", str(error), re.I)
    return float(match.group(1)) if match else None


class RateLimitAwareGemini(Gemini):
    """On a 429, waits as long as Gemini asks and tries once more, if that wait is short enough.

    This happens inside one model call, so the session is untouched and nothing is repeated. A
    longer wait is raised, so the user is told right away instead of being left waiting.
    """

    rate_limit_max_wait_s: float = 30

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        try:
            async for response in super().generate_content_async(llm_request, stream):
                yield response
            return
        except genai_errors.ClientError as error:
            if error.code != 429:
                raise
            delay = retry_delay_s(error)
            if delay is not None and delay > self.rate_limit_max_wait_s:
                raise
            wait_s = DEFAULT_RATE_LIMIT_WAIT_S if delay is None else delay

        turn = current_turn.get()
        if turn is not None:
            await turn.recorder.event(
                "model_rate_limited", name=turn.model_name, data={"wait_s": wait_s}
            )
        await asyncio.sleep(wait_s)
        async for response in super().generate_content_async(llm_request, stream):
            yield response


def build_model(settings: Settings) -> BaseLlm:
    if settings.model_mode == "scripted":
        return ScriptedLlm(model="scripted")
    return RateLimitAwareGemini(
        model=settings.model_name,
        rate_limit_max_wait_s=settings.model_rate_limit_max_wait_s,
        retry_options=types.HttpRetryOptions(
            attempts=settings.model_attempts,
            initial_delay=1,
            max_delay=4,
            http_status_codes=RETRYABLE_STATUS_CODES,
        ),
    )


def build_request_config(settings: Settings) -> types.GenerateContentConfig:
    """A live call has no deadline by default, and a stuck model holds it until the timeout."""
    return types.GenerateContentConfig(
        http_options=types.HttpOptions(timeout=int(settings.model_request_timeout_s * 1000)),
    )
