"""The ADK adapter: the inner model loop and the transcript. It holds no business decisions."""

import httpx
from google.adk.agents import LlmAgent
from google.adk.agents.invocation_context import LlmCallsLimitExceededError
from google.adk.agents.run_config import RunConfig
from google.adk.errors._stale_session_error import StaleSessionError
from google.adk.models.base_llm import BaseLlm
from google.adk.runners import Runner
from google.adk.sessions.base_session_service import BaseSessionService
from google.genai import errors as genai_errors
from google.genai import types

from app.domains.agent import callbacks
from app.domains.agent.prompts import SYSTEM_PROMPT
from app.domains.chat.ports import ModelCallLimitExceeded, ModelUnavailable, SessionConflict
from app.domains.tracing.service import RunRecorder

APP_NAME = "errand-agent"


def build_agent(model: BaseLlm, request_config: types.GenerateContentConfig | None) -> LlmAgent:
    return LlmAgent(
        name="errand_agent",
        model=model,
        instruction=SYSTEM_PROMPT,
        generate_content_config=request_config,
        tools=[],
        before_model_callback=callbacks.before_model,
        after_model_callback=callbacks.after_model,
        on_model_error_callback=callbacks.on_model_error,
        on_tool_error_callback=callbacks.on_tool_error,
    )


class AdkAgentRuntime:
    def __init__(
        self,
        model: BaseLlm,
        session_service: BaseSessionService,
        max_model_calls: int,
        request_config: types.GenerateContentConfig | None = None,
    ) -> None:
        self._sessions = session_service
        self._model_name = model.model
        self._run_config = RunConfig(max_llm_calls=max_model_calls)
        self._runner = Runner(
            app_name=APP_NAME,
            agent=build_agent(model, request_config),
            session_service=session_service,
        )

    async def run_turn(
        self, user_id: str, session_id: str, message: str, recorder: RunRecorder
    ) -> str:
        token = callbacks.current_turn.set(callbacks.TurnTelemetry(recorder, self._model_name))
        try:
            await self._ensure_session(user_id, session_id)
            return await self._collect_reply(user_id, session_id, message)
        except LlmCallsLimitExceededError as error:
            raise ModelCallLimitExceeded(str(error)) from error
        except StaleSessionError as error:
            raise SessionConflict(str(error)) from error
        except (genai_errors.APIError, httpx.TransportError) as error:
            raise ModelUnavailable(f"{type(error).__name__}: {error}") from error
        finally:
            callbacks.current_turn.reset(token)

    async def _ensure_session(self, user_id: str, session_id: str) -> None:
        existing = await self._sessions.get_session(
            app_name=APP_NAME, user_id=user_id, session_id=session_id
        )
        if existing is None:
            await self._sessions.create_session(
                app_name=APP_NAME, user_id=user_id, session_id=session_id
            )

    async def _collect_reply(self, user_id: str, session_id: str, message: str) -> str:
        new_message = types.Content(role="user", parts=[types.Part(text=message)])
        reply_parts: list[str] = []
        async for event in self._runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=new_message,
            run_config=self._run_config,
        ):
            if event.is_final_response() and event.content and event.content.parts:
                reply_parts.extend(
                    part.text for part in event.content.parts if part.text and not part.thought
                )
        return "".join(reply_parts)
