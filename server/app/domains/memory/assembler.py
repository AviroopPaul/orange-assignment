"""Builds what the model sees on every call. Pure code, with no ADK and no database.

Trim order: large tool results to a cap, then older turns, then the summary, then memories. The
current request and the current tool calls stay. Tool results shrink further only if all of that was
not enough.
"""

import copy
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from math import ceil
from typing import Any

from app.core.redaction import scrub_text, scrub_value
from app.domains.memory.rules import Fact
from app.domains.workflow.filters import open_days

CHARS_PER_TOKEN = 3
TOOL_RESULT_CAP_CHARS = 4500
TOOL_RESULT_FLOOR_CHARS = 400
SUMMARY_QUOTES = 6
SUMMARY_QUOTE_CHARS = 80
UNCONFIRMED = "[unconfirmed]"
_NO_TIME = datetime.min.replace(tzinfo=UTC)


class ContextOverBudget(Exception):
    """Nothing left to trim. The call must not run over budget."""


@dataclass(frozen=True)
class Turn:
    role: str
    text: str
    at: datetime | None = None


def with_updates(turns: list[Turn], updates: list[Turn]) -> list[Turn]:
    """The chat as the user saw it. The system's own news appears in it, in time order."""
    return sorted([*turns, *updates], key=lambda turn: turn.at or _NO_TIME)


@dataclass(frozen=True)
class ContextInputs:
    prompt: str
    tools: str
    workflow: str
    booking: str
    current_message: str
    # This run so far, as plain dicts of the model's content. The first holds the user's message.
    tail: list[dict[str, Any]]
    turns: list[Turn] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    shortlist: dict[str, Any] = field(default_factory=dict)
    updates: list[str] = field(default_factory=list)
    hidden: re.Pattern[str] | None = None


@dataclass(frozen=True)
class AssembledContext:
    system: str
    turns: list[Turn]
    tail: list[dict[str, Any]]
    tokens: dict[str, int]
    total: int
    facts_sent: list[str]
    turns_dropped: int
    tool_results_shrunk: bool


def estimate(text: str) -> int:
    """Pessimistic, so an offline run stays within budget. Live runs log the real count too."""
    return ceil(len(text) / CHARS_PER_TOKEN)


def _size(part: dict[str, Any]) -> str:
    visible = {key: value for key, value in part.items() if key != "thought_signature"}
    return json.dumps(visible, default=str)


def _tail_tokens(tail: list[dict[str, Any]]) -> int:
    return sum(estimate(_size(part)) for content in tail for part in content.get("parts", []))


def _parts_of(tail: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [part[kind] for content in tail for part in content.get("parts", []) if part.get(kind)]


def _shrink_results(tail: list[dict[str, Any]], cap: int) -> bool:
    shrunk = False
    for result in _parts_of(tail, "function_response"):
        text = json.dumps(result.get("response"), default=str)
        if len(text) > cap:
            result["response"] = {"shrunk": True, "preview": text[:cap]}
            shrunk = True
    return shrunk


def _hide_unconfirmed(tail: list[dict[str, Any]]) -> None:
    """A guess stays out of context from the moment code refuses to trust it."""
    unconfirmed = {
        result.get("id")
        for result in _parts_of(tail, "function_response")
        if result.get("name") == "propose_memory"
        and (result.get("response") or {}).get("status") == "unconfirmed"
    }
    for call in _parts_of(tail, "function_call"):
        if call.get("name") == "propose_memory" and call.get("id") in unconfirmed:
            call["args"] = {**call.get("args", {}), "value": UNCONFIRMED, "quote": UNCONFIRMED}


def _render_facts(facts: list[Fact]) -> str:
    if not facts:
        return ""
    lines = "\n".join(
        f"- {fact.key}: {fact.value}" + ("" if fact.scope == "all" else f" (for {fact.scope})")
        for fact in facts
    )
    return f"What you remember about this user, which they stated or confirmed:\n{lines}"


def _render_results(shortlist: dict[str, Any]) -> str:
    if not shortlist:
        return ""
    lines = [
        f"- {provider_id}, {provider.get('name')}, price {provider.get('price')}, "
        f"rating {provider.get('rating')}, open on: {', '.join(open_days(provider['slots']))}"
        for provider_id, provider in shortlist.items()
    ]
    listed = "\n".join(lines)
    return (
        "Latest search results, written by the system, still valid. Times are not listed: use "
        f"get_open_slots.\n{listed}"
    )


def _render_summary(dropped: list[Turn], updates: list[str]) -> str:
    """Built by code from text already redacted, so it cannot bring a forgotten fact back."""
    if not dropped:
        return ""
    said = [turn.text for turn in dropped if turn.role == "user"][-SUMMARY_QUOTES:]
    lines = [f"{len(dropped)} earlier messages of this chat are not shown."]
    lines.extend(f'- The user said: "{text[:SUMMARY_QUOTE_CHARS]}"' for text in said)
    lines.extend(f"- {update}" for update in updates)
    return "Earlier in this chat:\n" + "\n".join(lines)


def _searched_in_this_run(tail: list[dict[str, Any]]) -> bool:
    return any(call.get("name") == "search_providers" for call in _parts_of(tail, "function_call"))


def assemble(inputs: ContextInputs, budget: int) -> AssembledContext:
    pattern = inputs.hidden
    prompt = scrub_text(inputs.prompt, pattern)
    current = scrub_text(inputs.current_message, pattern)
    turns = [Turn(turn.role, scrub_text(turn.text, pattern)) for turn in inputs.turns]
    updates = [scrub_text(update, pattern) for update in inputs.updates]
    facts = list(inputs.facts)
    tail: list[dict[str, Any]] = copy.deepcopy(inputs.tail)
    _hide_unconfirmed(tail)
    tail = [scrub_value(content, pattern) for content in tail]
    for part in tail[0].get("parts", []) if tail else []:
        if part.get("text"):
            part["text"] = current
    shrunk = _shrink_results(tail, TOOL_RESULT_CAP_CHARS)

    shortlist = {} if _searched_in_this_run(tail) else inputs.shortlist
    dropped: list[Turn] = []
    keep_summary = True
    fixed = estimate(prompt) + estimate(inputs.tools)

    def tokens() -> dict[str, int]:
        workflow, booking = estimate(inputs.workflow), estimate(inputs.booking)
        return {
            "system": max(0, estimate(prompt) - workflow - booking),
            "tools": estimate(inputs.tools),
            "workflow": workflow,
            "booking": booking,
            "search_results": estimate(_render_results(shortlist)),
            "memories": estimate(_render_facts(facts)),
            "summary": estimate(_render_summary(dropped, updates)) if keep_summary else 0,
            "older_turns": sum(estimate(turn.text) for turn in turns),
            "current_run": _tail_tokens(tail),
        }

    def total() -> int:
        counts = tokens()
        return fixed + sum(
            counts[name]
            for name in ("search_results", "memories", "summary", "older_turns", "current_run")
        )

    while total() > budget and turns:
        dropped.append(turns.pop(0))
    if total() > budget:
        keep_summary = False
    while total() > budget and facts:
        facts.pop()
    if total() > budget:
        shrunk = _shrink_results(tail, TOOL_RESULT_FLOOR_CHARS) or shrunk
    if total() > budget:
        raise ContextOverBudget(f"{total()} estimated tokens, budget {budget}")

    sections = [
        prompt,
        _render_facts(facts),
        _render_results(shortlist),
        _render_summary(dropped, updates) if keep_summary else "",
    ]
    return AssembledContext(
        system="\n\n".join(section for section in sections if section),
        turns=turns,
        tail=tail,
        tokens=tokens(),
        total=total(),
        facts_sent=[fact.key for fact in facts],
        turns_dropped=len(dropped),
        tool_results_shrunk=shrunk,
    )
