"""Builds the model from settings. Evals swap the model here and nowhere else."""

from google.adk.models.base_llm import BaseLlm
from google.adk.models.google_llm import Gemini
from google.genai import types

from app.core.config import Settings
from app.domains.agent.scripted import ScriptedLlm

RETRYABLE_STATUS_CODES = [429, 503, 504]


def build_model(settings: Settings) -> BaseLlm:
    if settings.model_mode == "scripted":
        return ScriptedLlm(model="scripted")
    return Gemini(
        model=settings.model_name,
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
