"""Puts the assembled context into ADK's request. ADK's own history is thrown away here."""

import json
from datetime import date
from typing import Any

from google.adk.models.llm_request import LlmRequest
from google.genai import types
from sqlalchemy.exc import SQLAlchemyError

from app.domains.agent.prompts import render_system_prompt
from app.domains.bookings.service import ApprovalService
from app.domains.chat.ports import ContextUnavailable
from app.domains.memory.assembler import ContextInputs, ContextOverBudget, Turn, assemble
from app.domains.memory.service import USER, MemoryService
from app.domains.tracing.service import RunRecorder
from app.domains.workflow.service import WorkflowService

_SUMMARY_UPDATES = 5


def _current_run(contents: list[types.Content]) -> list[types.Content]:
    """From the user's message on. A tool's result also has the user role, but holds no text."""
    for index in range(len(contents) - 1, -1, -1):
        content = contents[index]
        if content.role == USER and any(part.text for part in content.parts or []):
            return contents[index:]
    return contents[-1:]


def _for_log(content: dict[str, Any]) -> dict[str, Any]:
    parts = [
        {key: value for key, value in part.items() if key != "thought_signature"}
        for part in content.get("parts", [])
    ]
    return {"role": content.get("role"), "parts": parts}


async def apply_context(
    llm_request: LlmRequest,
    recorder: RunRecorder,
    workflow: WorkflowService,
    bookings: ApprovalService,
    memory: MemoryService,
    user_id: str,
    session_id: str,
    today: date,
    budget: int,
) -> int:
    """Replace the request's contents. The call does not run unless its context is logged."""
    config = llm_request.config
    tools = [
        tool.model_dump(mode="json", exclude_none=True)
        for tool in config.tools or []
        if isinstance(tool, types.Tool)
    ]
    tail = [content.model_dump(exclude_none=True) for content in _current_run(llm_request.contents)]
    message = next(
        (part["text"] for content in tail[:1] for part in content["parts"] if part.get("text")), ""
    )
    try:
        snapshot = await workflow.snapshot(user_id, session_id)
        booking = await bookings.prompt_note(user_id, session_id)
        turns = await memory.recent_turns(user_id, session_id)
        # The user's message arrives through `tail`, but its stored copy is the one kept clean.
        if turns and turns[-1].role == USER:
            message = turns.pop().text
        request_text = snapshot.for_prompt()
        assembled = assemble(
            ContextInputs(
                prompt=render_system_prompt(today, request_text, booking),
                tools=json.dumps(tools),
                workflow=request_text,
                booking=booking,
                current_message=message,
                tail=tail,
                turns=turns,
                facts=await memory.relevant_facts(
                    user_id, snapshot.details.get("service"), message
                ),
                shortlist=snapshot.shortlist,
                updates=await bookings.latest_updates(user_id, session_id, _SUMMARY_UPDATES),
                hidden=await memory.hidden_pattern(user_id),
            ),
            budget,
        )
        contents = [_as_content(turn) for turn in assembled.turns]
        current = [types.Content.model_validate(item) for item in assembled.tail]
        logged = [item.model_dump(mode="json", exclude_none=True) for item in contents + current]
        context_id = await memory.log_context(
            recorder.run_id,
            user_id,
            session_id,
            payload={
                "system": assembled.system,
                "tools": [
                    declaration["name"]
                    for tool in tools
                    for declaration in tool.get("function_declarations", [])
                ],
                "contents": [_for_log(item) for item in logged],
            },
            tokens=assembled.tokens,
            estimated_tokens=assembled.total,
            budget=budget,
        )
    except (ContextOverBudget, SQLAlchemyError, OSError) as error:
        raise ContextUnavailable(f"{type(error).__name__}: {error}") from error

    config.system_instruction = assembled.system
    llm_request.contents = contents + current
    await recorder.event(
        "context_assembled",
        data={
            "context_id": context_id,
            "estimated_tokens": assembled.total,
            "budget": budget,
            "tokens": assembled.tokens,
            "memories": assembled.facts_sent,
            "turns_sent": len(assembled.turns),
            "turns_dropped": assembled.turns_dropped,
            "tool_results_shrunk": assembled.tool_results_shrunk,
        },
    )
    return context_id


def _as_content(turn: Turn) -> types.Content:
    role = USER if turn.role == USER else "model"
    return types.Content(role=role, parts=[types.Part(text=turn.text)])
