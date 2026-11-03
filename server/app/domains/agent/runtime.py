"""The ADK adapter: the inner model loop and the transcript. It holds no business decisions."""

from collections.abc import Callable
from datetime import date

import httpx
from google.adk.agents import LlmAgent
from google.adk.agents.invocation_context import LlmCallsLimitExceededError
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.agents.run_config import RunConfig
from google.adk.errors._stale_session_error import StaleSessionError
from google.adk.models.base_llm import BaseLlm
from google.adk.runners import Runner
from google.adk.sessions.base_session_service import BaseSessionService
from google.genai import errors as genai_errors
from google.genai import types

from app.domains.agent import callbacks
from app.domains.agent.prompts import render_system_prompt
from app.domains.agent.tools import MODEL_TOOLS
from app.domains.chat.ports import (
    ModelCallLimitExceeded,
    ModelUnavailable,
    SessionConflict,
    ToolCallLimitExceeded,
)
from app.domains.gateway.schemas import Caller
from app.domains.gateway.service import CallContext, ToolBudget, ToolBudgetExhausted, ToolGateway
from app.domains.tracing.service import RunRecorder

APP_NAME = "errand-agent"
MODEL_TOOL_NAMES = frozenset(tool.__name__ for tool in MODEL_TOOLS)


def build_agent(
    model: BaseLlm,
    request_config: types.GenerateContentConfig | None,
    today: Callable[[], date],
) -> LlmAgent:
    def instruction(context: ReadonlyContext) -> str:
        return render_system_prompt(today())

    return LlmAgent(
        name="errand_agent",
        model=model,
        instruction=instruction,
        generate_content_config=request_config,
        tools=list(MODEL_TOOLS),
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
        gateway: ToolGateway,
        max_model_calls: int,
        max_tool_calls: int,
        today: Callable[[], date],
        request_config: types.GenerateContentConfig | None = None,
    ) -> None:
        self._sessions = session_service
        self._gateway = gateway
        self._model_name = model.model
        self._max_tool_calls = max_tool_calls
        self._run_config = RunConfig(max_llm_calls=max_model_calls)
        self._runner = Runner(
            app_name=APP_NAME,
            agent=build_agent(model, request_config, today),
            session_service=session_service,
        )

    async def run_turn(
        self, user_id: str, session_id: str, message: str, recorder: RunRecorder
    ) -> str:
        tool_context = CallContext(
            user_id=user_id,
            caller=Caller.MODEL,
            budget=ToolBudget(self._max_tool_calls),
            recorder=recorder,
        )
        turn = callbacks.TurnTelemetry(
            recorder, self._model_name, self._gateway, tool_context, MODEL_TOOL_NAMES
        )
        turn_token = callbacks.current_turn.set(turn)
        try:
            await self._ensure_session(user_id, session_id)
            return await self._collect_reply(user_id, session_id, message)
        except ToolBudgetExhausted as error:
            raise ToolCallLimitExceeded(str(error)) from error
        except LlmCallsLimitExceededError as error:
            raise ModelCallLimitExceeded(str(error)) from error
        except StaleSessionError as error:
            raise SessionConflict(str(error)) from error
        except (genai_errors.APIError, httpx.TransportError) as error:
            raise ModelUnavailable(f"{type(error).__name__}: {error}") from error
        finally:
            callbacks.current_turn.reset(turn_token)

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
