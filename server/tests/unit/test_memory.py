"""Memory and the assembled context, through the real ADK loop with a scripted model."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from google.adk.sessions import InMemorySessionService
from google.genai import errors as genai_errors
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.core.database import Database
from app.core.locks import RedisTurnLock
from app.core.redaction import REMOVED, compile_patterns, scrub_text
from app.domains.agent.runtime import APP_NAME, AdkAgentRuntime
from app.domains.agent.scripted import ScriptedLlm, ToolCall
from app.domains.bookings.service import ApprovalService
from app.domains.chat.schemas import ChatRequest, ChatResponse, FailureReason, Outcome
from app.domains.chat.service import ChatService
from app.domains.gateway.service import ToolGateway
from app.domains.memory import seed as seed_data
from app.domains.memory.assembler import (
    ContextInputs,
    ContextOverBudget,
    Turn,
    assemble,
    estimate,
)
from app.domains.memory.models import ContextLog, Memory, MemoryStatus, TurnView
from app.domains.memory.rules import (
    Fact,
    asks_to_forget,
    is_explicit,
    rank,
    read_memory_answer,
)
from app.domains.memory.seed import seed_memories
from app.domains.memory.service import MemoryNotFound, MemoryService
from app.domains.tracing.service import TraceService
from app.domains.workflow.service import WorkflowService
from tests.conftest import FakeClock, make_ready

USER = "u1"
NOW = datetime(2026, 11, 2, tzinfo=UTC)


def propose(key: str, value: str, quote: str, scope: str = "all") -> ToolCall:
    return ToolCall("propose_memory", {"key": key, "value": value, "quote": quote, "scope": scope})


class Stack:
    def __init__(self, chat: ChatService, sessions: InMemorySessionService, model: ScriptedLlm):
        self.chat, self.sessions, self.model = chat, sessions, model

    async def say(self, message: str, session: str = "s1", user: str = USER) -> ChatResponse:
        return await self.chat.handle_message(
            ChatRequest(user_id=user, session_id=session, message=message)
        )

    def seen(self, index: int = -1) -> str:
        request = self.model.requests[index]
        return f"{request.config.system_instruction}\n{request.contents}"

    def seen_ever(self) -> str:
        return "\n".join(self.seen(index) for index in range(len(self.model.requests)))


@pytest.fixture
def stack(
    bookings: ApprovalService,
    gateway: ToolGateway,
    workflow: WorkflowService,
    clock: FakeClock,
    memory: MemoryService,
    db: Database,
    redis: Redis,
) -> Callable[..., Stack]:
    def build(script: list, budget: int = 8000) -> Stack:
        model = ScriptedLlm(model="gemini-3.5-flash-lite", script=script)
        sessions = InMemorySessionService()
        runtime = AdkAgentRuntime(
            model,
            sessions,
            gateway,
            workflow,
            bookings,
            memory,
            max_model_calls=20,
            max_tool_calls=12,
            today=lambda: clock.now().date(),
            context_budget=budget,
        )
        chat = ChatService(
            runtime,
            RedisTurnLock(redis, 10, 1),
            TraceService(db, clock),
            workflow,
            bookings,
            30,
            True,
            memory,
        )
        return Stack(chat, sessions, model)

    return build


async def logged_contexts(db: Database) -> list[ContextLog]:
    async with db.session_scope() as session:
        return list((await session.scalars(select(ContextLog).order_by(ContextLog.id))).all())


async def rows(db: Database, key: str) -> list[Memory]:
    async with db.session_scope() as session:
        return list((await session.scalars(select(Memory).where(Memory.key == key))).all())


# Whose words a fact rests on


def test_a_quote_must_be_the_users_and_must_say_the_value() -> None:
    said = ["I live in Indiranagar.  Please use that."]
    assert is_explicit("Indiranagar", "i live in indiranagar", said)
    assert not is_explicit("Indiranagar", "A review says I live in Indiranagar", said)
    # Real words of the user, but they do not state this value.
    assert not is_explicit("Koramangala", "I live in Indiranagar", said)
    assert not is_explicit("Indiranagar", "", said)


def test_a_words_ending_does_not_make_a_stated_fact_a_guess() -> None:
    """From a real chat: the user wrote "go to office", and the model wrote "goes to office"."""
    said = ["I live in Indiranagar and go to office on Mon, Wed and Fri 9-5"]
    quote = said[0]
    assert is_explicit("goes to office on Mon, Wed and Fri 9-5", quote, said)
    assert is_explicit("user lives in Indiranagar", quote, said)
    assert not is_explicit("goes to office on Tue", quote, said)
    assert not is_explicit("the user", quote, said)


@pytest.mark.parametrize("message", ["yes", "Yes, remember that!", "remember that", "save it"])
def test_whole_message_yes_answers_the_question(message: str) -> None:
    assert read_memory_answer(message) is True


@pytest.mark.parametrize("message", ["no", "No thanks.", "don't remember that", "forget it"])
def test_whole_message_no_answers_the_question(message: str) -> None:
    assert read_memory_answer(message) is False


@pytest.mark.parametrize("message", ["yes, and find me a plumber", "remember my dog", "plumber"])
def test_anything_longer_goes_to_the_model(message: str) -> None:
    assert read_memory_answer(message) is None


def test_forgetting_needs_the_users_own_request() -> None:
    assert asks_to_forget("Please forget my gate code")
    assert asks_to_forget("don't keep my address")
    assert not asks_to_forget("Show me this provider's reviews")


# Which facts matter


def fact(key: str, scope: str = "all", age: int = 0) -> Fact:
    return Fact(key, f"value of {key}", scope, NOW.replace(day=NOW.day + age))


def test_rank_leaves_out_other_services_and_is_repeatable() -> None:
    facts = [fact("area"), fact("brand", "ac_servicing"), fact("pipe", "plumber")]
    ranked = rank(facts, "plumber", "I need help", limit=10)
    assert [item.key for item in ranked] == ["pipe", "area"]
    assert rank(list(reversed(facts)), "plumber", "I need help", limit=10) == ranked


def test_with_no_service_known_a_services_fact_needs_a_mention() -> None:
    facts = [fact("area"), fact("brand", "ac_servicing")]
    assert [item.key for item in rank(facts, None, "hello", 10)] == ["area"]
    assert rank(facts, None, "my ac is noisy", 10)[0].key == "brand"


# Trim order


def inputs(**extra: Any) -> ContextInputs:
    defaults: dict[str, Any] = {
        "prompt": "P" * 300,
        "tools": "T" * 300,
        "workflow": "",
        "booking": "",
        "current_message": "now",
        "tail": [{"role": "user", "parts": [{"text": "raw now"}]}],
    }
    return ContextInputs(**(defaults | extra))


def old_turns(count: int) -> list[Turn]:
    return [Turn("user", f"said {n} " + "x" * 300) for n in range(count)]


def test_nothing_is_cut_when_it_fits() -> None:
    result = assemble(inputs(turns=old_turns(2), facts=[fact("area")]), budget=8000)
    assert len(result.turns) == 2
    assert result.facts_sent == ["area"]
    assert result.turns_dropped == 0
    assert "Earlier in this chat" not in result.system


def test_older_turns_go_first_and_oldest_first() -> None:
    result = assemble(inputs(turns=old_turns(20), facts=[fact("area")]), budget=1000)
    assert 0 < len(result.turns) < 20
    assert result.turns[-1].text.startswith("said 19")
    assert result.facts_sent == ["area"]
    assert result.total <= 1000
    assert "earlier messages of this chat are not shown" in result.system


def test_the_summary_goes_before_memories() -> None:
    long_fact = Fact("area", "y" * 900, "all", NOW)
    updates = ["z" * 500]
    result = assemble(
        inputs(turns=old_turns(5), facts=[long_fact], updates=updates),
        budget=estimate("P" * 300) + estimate("T" * 300) + estimate("y" * 900) + 40,
    )
    assert result.turns == []
    assert result.facts_sent == ["area"]
    assert "Earlier in this chat" not in result.system


def test_lowest_ranked_memories_go_last_of_all() -> None:
    facts = [fact("first"), Fact("second", "y" * 900, "all", NOW)]
    result = assemble(inputs(facts=facts), budget=300)
    assert result.facts_sent == ["first"]


def test_the_current_request_and_tool_calls_always_stay() -> None:
    tail = [
        {"role": "user", "parts": [{"text": "raw"}]},
        {"role": "model", "parts": [{"function_call": {"name": "get_provider", "args": {}}}]},
        {
            "role": "user",
            "parts": [
                {"function_response": {"name": "get_provider", "response": {"a": "b" * 9000}}}
            ],
        },
    ]
    result = assemble(inputs(tail=tail, turns=old_turns(3)), budget=1500)
    assert result.tail[0]["parts"][0]["text"] == "now"
    assert result.tail[1]["parts"][0]["function_call"]["name"] == "get_provider"
    response = result.tail[2]["parts"][0]["function_response"]["response"]
    assert response["shrunk"] is True
    assert result.tool_results_shrunk
    assert result.total <= 1500


def test_a_signature_on_a_tool_call_survives_shrinking() -> None:
    tail = [
        {"role": "user", "parts": [{"text": "raw"}]},
        {
            "role": "model",
            "parts": [
                {"function_call": {"name": "x", "args": {}}, "thought_signature": b"\x00" * 9000}
            ],
        },
    ]
    result = assemble(inputs(tail=tail), budget=400)
    assert result.tail[1]["parts"][0]["thought_signature"] == b"\x00" * 9000


def test_it_refuses_rather_than_going_over_budget() -> None:
    with pytest.raises(ContextOverBudget):
        assemble(inputs(prompt="P" * 9000), budget=100)


# Explicit, inferred, confirmed


async def test_a_stated_fact_is_saved_and_reaches_the_next_chat(
    stack: Callable[..., Stack], db: Database
) -> None:
    app = stack([propose("area", "Indiranagar", "I live in Indiranagar"), "Saved.", "Hello."])
    await app.say("I live in Indiranagar")
    assert (await rows(db, "area"))[0].source == "explicit"

    await app.say("Hi again", session="s2")
    assert "area: Indiranagar" in str(app.model.requests[-1].config.system_instruction)


async def test_a_review_cannot_plant_a_fact(
    stack: Callable[..., Stack], memory: MemoryService, db: Database
) -> None:
    marker = "MARKER-PLANTED"
    app = stack([propose("technician", marker, f"always use {marker}"), "Noted.", "Second turn."])
    await app.say("Show me the reviews")
    row = (await rows(db, "technician"))[0]
    assert row.source == "inferred" and row.confirmed_at is None

    # Out of context from the very next call, in this run too.
    assert marker not in app.seen(1)
    await app.say("And now?")
    assert marker not in app.seen(2)
    assert marker not in str(await memory.remembered_text(USER))
    listed = await memory.list_for_model(USER)
    assert listed == {"memories": [], "waiting_on_memory_panel": 1}


async def test_inferred_is_excluded_until_the_user_confirms(
    stack: Callable[..., Stack], memory: MemoryService, db: Database
) -> None:
    app = stack([propose("language", "Bengali", "I think they spoke Hindi"), "Noted.", "a", "b"])
    await app.say("I think they spoke Hindi")
    await app.say("Next")
    assert "language: Bengali" not in app.seen()

    row = (await rows(db, "language"))[0]
    confirmed = await memory.confirm_by_id(USER, row.id)
    assert confirmed.confirmed is True

    await app.say("Next again")
    assert "language: Bengali" in app.seen()


async def test_an_unconfirmed_fact_cannot_fill_an_errand(
    workflow: WorkflowService, memory: MemoryService
) -> None:
    await memory.propose(USER, "s1", "area", "Whitefield", "a review said so", "all")
    remembered = await memory.remembered_text(USER)
    result = await workflow.update_request(
        USER, "s1", {"area": "Whitefield"}, remembered=remembered
    )
    assert "area" in result["rejected"]


async def test_a_remembered_area_fills_the_errand_with_its_source(
    workflow: WorkflowService, memory: MemoryService, db: Database
) -> None:
    await memory.record_turn(USER, "old", "user", "I live in Indiranagar")
    await memory.propose(USER, "old", "area", "Indiranagar", "I live in Indiranagar", "all")
    await workflow.record_user_message(USER, "new", "I need a plumber")

    remembered = await memory.remembered_text(USER)
    result = await workflow.update_request(
        USER, "new", {"area": "Indiranagar"}, remembered=remembered
    )
    assert result["saved"]["area"] == "Indiranagar"
    assert "rejected" not in result
    async with db.session_scope() as session:
        from app.domains.workflow.repository import WorkflowRepository

        row = await WorkflowRepository(session).get_active(USER, "new")
    assert row is not None and row.request["area"]["source"] == "memory"


# What the chat shows


async def test_two_facts_in_one_message_are_both_saved_and_shown_once(
    stack: Callable[..., Stack],
) -> None:
    said = "I live in Indiranagar and go to office on Mon, Wed and Fri 9-5"
    app = stack(
        [
            propose("area", "Indiranagar", said),
            propose("work_schedule", "goes to office on Mon, Wed and Fri 9-5", said),
            "What do you need?",
            "Sure.",
        ]
    )
    first = await app.say(said)
    assert [item.key for item in first.memory.saved] == ["area", "work_schedule"]
    assert first.memory.to_confirm == []

    second = await app.say("Hello again")
    assert second.memory.saved == []
    assert "work_schedule: goes to office" in app.seen()


async def test_a_guess_is_asked_in_chat_and_a_typed_yes_saves_it_without_the_model(
    stack: Callable[..., Stack], memory: MemoryService
) -> None:
    app = stack([propose("language", "Bengali", "I think they spoke Hindi"), "Noted.", "Hello."])
    first = await app.say("I think they spoke Hindi")
    assert [item.value for item in first.memory.to_confirm] == ["Bengali"]
    calls = len(app.model.requests)

    answer = await app.say("yes, remember that")
    assert len(app.model.requests) == calls
    assert "Bengali" in answer.reply
    assert answer.memory.to_confirm == []
    assert (await memory.list_all(USER))[0].confirmed

    await app.say("Next")
    assert "language: Bengali" in app.seen()


async def test_a_typed_no_drops_the_guess_and_leaves_the_chat_alone(
    stack: Callable[..., Stack], memory: MemoryService
) -> None:
    app = stack([propose("language", "Bengali", "I think they spoke Hindi"), "Noted.", "Hello."])
    await app.say("I think they spoke Hindi")
    await app.say("no")

    assert await memory.list_all(USER) == []
    await app.say("Next")
    assert "I think they spoke Hindi" in app.seen()
    assert "Bengali" not in app.seen()


async def test_the_question_lasts_one_message_and_a_later_yes_does_not_count(
    stack: Callable[..., Stack], memory: MemoryService
) -> None:
    app = stack(
        [propose("language", "Bengali", "I think they spoke Hindi"), "Noted.", "Search?", "Ok."]
    )
    await app.say("I think they spoke Hindi")
    moved_on = await app.say("I need a plumber")
    assert moved_on.memory.to_confirm == []
    await app.say("yes")

    assert not (await memory.list_all(USER))[0].confirmed
    assert "Bengali" not in app.seen()


async def test_saying_no_to_a_guess_keeps_the_trusted_value(memory: MemoryService) -> None:
    await memory.record_turn(USER, "s1", "user", "I live in Indiranagar")
    await memory.propose(USER, "s1", "area", "Indiranagar", "I live in Indiranagar", "all")
    await memory.propose(USER, "s1", "area", "Whitefield", "a review said so", "all")
    guess = next(item for item in await memory.list_all(USER) if not item.confirmed)

    await memory.forget_by_id(USER, guess.id)

    assert [item.value for item in await memory.list_all(USER)] == ["Indiranagar"]
    assert (await memory.recent_turns(USER, "s1"))[0].text == "I live in Indiranagar"


# Correcting


async def test_a_corrected_value_never_reaches_the_model_again(
    stack: Callable[..., Stack], db: Database
) -> None:
    app = stack(
        [
            propose("area", "MARKER-OLD-AREA", "my area is MARKER-OLD-AREA"),
            "Saved MARKER-OLD-AREA.",
            propose("area", "Koramangala", "now it is Koramangala"),
            "Changed from MARKER-OLD-AREA to Koramangala.",
            "Sure.",
        ]
    )
    await app.say("my area is MARKER-OLD-AREA")
    await app.say("now it is Koramangala")
    await app.say("What next?")

    assert "MARKER-OLD-AREA" not in app.seen()
    assert "area: Koramangala" in app.seen()
    old, new = sorted(await rows(db, "area"), key=lambda row: row.status)
    assert old.status == MemoryStatus.ACTIVE and new.status == MemoryStatus.SUPERSEDED
    assert new.superseded_by == old.id


async def test_correcting_to_a_longer_value_keeps_the_chat_readable(
    memory: MemoryService,
) -> None:
    await memory.record_turn(USER, "s1", "user", "HSR please. It is HSR Layout now.")
    await memory.propose(USER, "s1", "area", "HSR", "HSR please", "all")
    await memory.propose(USER, "s1", "area", "HSR Layout", "It is HSR Layout now", "all")
    assert "HSR Layout" in (await memory.recent_turns(USER, "s1"))[0].text


# Forgetting


async def test_forget_removes_the_fact_from_every_copy(
    stack: Callable[..., Stack],
    memory: MemoryService,
    workflow: WorkflowService,
    db: Database,
    clock: FakeClock,
) -> None:
    marker = "MARKER4417"
    app = stack(
        [
            propose("gate_code", marker, f"my gate code is {marker}"),
            f"Saved {marker}.",
            ToolCall("forget_memory", {"key": "gate_code"}),
            f"I forgot {marker}.",
            "Anything else?",
        ]
    )
    await app.say(f"my gate code is {marker}")
    assert await app.sessions.get_session(app_name=APP_NAME, user_id=USER, session_id="s1")
    await app.say(f"please forget my gate code {marker}")

    # The call right after the forget, in the same run, is already clean.
    assert marker not in app.seen(3)
    await app.say("What do you know?")
    assert marker not in app.seen()

    async with db.session_scope() as session:
        chat = (await session.scalars(select(TurnView))).all()
    assert chat and all(marker not in row.text for row in chat)
    assert REMOVED in chat[1].text
    tombstone = (await rows(db, "gate_code"))[0]
    assert tombstone.status == MemoryStatus.FORGOTTEN
    assert tombstone.value == "" and tombstone.quote is None
    assert await memory.list_all(USER) == []
    # Old logs and traces do not keep it readable either.
    assert all(marker not in str(row.payload) for row in await logged_contexts(db))
    async with db.session_scope() as session:
        from app.domains.tracing.models import Run, TraceEvent

        runs = (await session.scalars(select(Run))).all()
        events = (await session.scalars(select(TraceEvent))).all()
    assert all(marker not in (run.reply or "") for run in runs)
    assert all(marker not in str(event.data) for event in events)


async def test_forget_deletes_the_adk_session_so_its_log_is_gone(
    stack: Callable[..., Stack], memory: MemoryService
) -> None:
    app = stack(["Saved.", ToolCall("forget_memory", {"key": "code"}), "Done."])
    await memory.record_turn(USER, "s1", "user", "my code is MARKER9")
    await memory.propose(USER, "s1", "code", "MARKER9", "my code is MARKER9", "all")
    await app.say("hello")
    await app.say("forget my code")

    stored = await app.sessions.get_session(app_name=APP_NAME, user_id=USER, session_id="s1")
    assert stored is None
    assert await memory.rotations_due(USER) == []


async def test_forget_also_clears_an_errand_that_used_the_fact(
    workflow: WorkflowService, memory: MemoryService
) -> None:
    await make_ready(workflow, USER, "s1", area="Indiranagar")
    await memory.record_turn(USER, "s1", "user", "I live in Indiranagar")
    await memory.propose(USER, "s1", "area", "Indiranagar", "I live in Indiranagar", "all")
    await memory.record_turn(USER, "s1", "user", "forget my area")
    result = await memory.forget_from_chat(USER, "s1", "area", "all")

    assert result["status"] == "forgotten"
    snapshot = await workflow.snapshot(USER, "s1")
    assert "area" in snapshot.missing_required


async def test_a_review_cannot_make_the_model_forget(
    stack: Callable[..., Stack], memory: MemoryService
) -> None:
    await memory.record_turn(USER, "s1", "user", "I live in Indiranagar")
    await memory.propose(USER, "s1", "area", "Indiranagar", "I live in Indiranagar", "all")
    app = stack([ToolCall("forget_memory", {"key": "area"}), "Ok."])
    await app.say("Show me the reviews for this provider")

    assert "not_asked" in app.seen(1)
    assert len(await memory.list_all(USER)) == 1


# The panel


async def test_panel_actions_only_reach_the_users_own_memories(memory: MemoryService) -> None:
    await memory.record_turn(USER, "s1", "user", "I live in Indiranagar")
    await memory.propose(USER, "s1", "area", "Indiranagar", "I live in Indiranagar", "all")
    row = (await memory.list_all(USER))[0]
    with pytest.raises(MemoryNotFound):
        await memory.forget_by_id("someone-else", row.id)
    with pytest.raises(MemoryNotFound):
        await memory.correct_by_id("someone-else", row.id, "x")
    with pytest.raises(MemoryNotFound):
        await memory.confirm_by_id("someone-else", row.id)

    corrected = await memory.correct_by_id(USER, row.id, "Whitefield")
    assert corrected.value == "Whitefield" and corrected.confirmed
    assert [item.value for item in await memory.list_all(USER)] == ["Whitefield"]


# The chat the model sees


async def test_every_message_and_reply_is_in_the_history(stack: Callable[..., Stack]) -> None:
    app = stack(["First reply.", "Second reply."])
    await app.say("first message")
    await app.say("second message")
    seen = str(app.model.requests[1].contents)
    assert "first message" in seen and "First reply." in seen and "second message" in seen


async def test_search_results_stay_available_on_a_later_turn(
    stack: Callable[..., Stack], workflow: WorkflowService
) -> None:
    await make_ready(workflow, USER, "s1")
    app = stack([ToolCall("search_providers"), "Here they are.", "Which one?"])
    await app.say("search please")
    await app.say("the first one looks good")

    system = str(app.model.requests[-1].config.system_instruction)
    assert "Latest search results" in system and "price" in system
    assert "open on:" in system
    assert "T09:00" not in system[system.index("Latest search results") :]
    # In the run that searched, the results are already in the tool's reply.
    assert "Latest search results" not in str(app.model.requests[1].config.system_instruction)


async def test_a_failed_turn_keeps_the_message_and_the_reply_in_the_history(
    stack: Callable[..., Stack],
) -> None:
    app = stack([genai_errors.ServerError(503, {"error": {"message": "busy"}}), "Better now."])
    response = await app.say("first try")
    assert response.outcome == Outcome.TOOL_FAILED

    await app.say("second try")
    seen = str(app.model.requests[-1].contents)
    assert "first try" in seen and "not responding" in seen


# The budget and the log


async def test_seeded_memories_and_a_long_chat_stay_within_budget_on_every_call(
    stack: Callable[..., Stack], db: Database, clock: FakeClock, workflow: WorkflowService
) -> None:
    assert await seed_memories(db, clock)
    assert not await seed_memories(db, clock)
    await make_ready(workflow, seed_data.SEED_USER, seed_data.SEED_SESSION)
    app = stack([ToolCall("search_providers"), ToolCall("list_bookings"), "Done."])
    await app.say("Find me a plumber", session=seed_data.SEED_SESSION, user=seed_data.SEED_USER)

    logs = await logged_contexts(db)
    assert len(logs) == len(app.model.requests) == 3
    for log in logs:
        assert log.estimated_tokens <= log.budget == 8000
        assert sum(log.tokens.values()) >= log.estimated_tokens - 1
    seen = app.seen_ever()
    assert seed_data.FORGET_ME_MARKER in str(logs[0].payload)  # still current
    for marker in (
        *seed_data.UNCONFIRMED_MARKERS,
        *seed_data.SUPERSEDED_MARKERS,
        *seed_data.FORGOTTEN_MARKERS,
    ):
        assert marker not in seen
        assert all(marker not in str(log.payload) for log in logs)
    # Only what matters: plumber facts, never another service's.
    assert "MARKER-PLUMBER-00" in seen
    assert "MARKER-ELECTRICIAN-00" not in seen
    assert "MARKER-AC_SERVICING-00" not in seen
    assert "earlier messages of this chat are not shown" in seen


async def test_forgetting_a_seeded_fact_also_cleans_the_long_old_chat(
    stack: Callable[..., Stack], db: Database, clock: FakeClock
) -> None:
    await seed_memories(db, clock)
    app = stack(
        [ToolCall("forget_memory", {"key": seed_data.FORGET_ME_KEY}), "Forgotten.", "Hello."]
    )
    user, session = seed_data.SEED_USER, seed_data.SEED_SESSION
    await app.say("forget my gate code", session=session, user=user)
    await app.say("hello", session=session, user=user)

    assert seed_data.FORGET_ME_MARKER not in app.seen()
    async with db.session_scope() as scope:
        chat = (await scope.scalars(select(TurnView).where(TurnView.user_id == user))).all()
    assert all(seed_data.FORGET_ME_MARKER not in row.text for row in chat)


async def test_every_model_call_is_logged_with_the_full_payload_and_counts(
    stack: Callable[..., Stack], db: Database
) -> None:
    app = stack([ToolCall("list_bookings"), "Done."])
    await app.say("show my bookings")

    logs = await logged_contexts(db)
    assert len(logs) == 2
    assert "Errand Agent" in logs[0].payload["system"]
    assert "propose_memory" in logs[0].payload["tools"]
    assert "show my bookings" in str(logs[0].payload["contents"])
    assert "function_response" in str(logs[1].payload["contents"])
    assert logs[0].tokens["tools"] > 0 and logs[0].tokens["system"] > 0
    # Beside the estimate, the real count from the model.
    assert logs[0].actual_input_tokens == app.model.input_tokens


async def test_if_the_log_cannot_be_written_the_call_does_not_run(
    stack: Callable[..., Stack], memory: MemoryService, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*args: Any, **kwargs: Any) -> int:
        raise SQLAlchemyError("disk full")

    monkeypatch.setattr(memory, "log_context", broken)
    app = stack(["Should not run."])
    response = await app.say("hello")

    assert app.model.requests == []
    assert response.reason == FailureReason.CONTEXT_UNAVAILABLE
    assert response.outcome == Outcome.TOOL_FAILED


async def test_over_budget_is_a_clear_reply_and_no_call(stack: Callable[..., Stack]) -> None:
    app = stack(["Should not run."], budget=100)
    response = await app.say("hello")
    assert app.model.requests == []
    assert response.reason == FailureReason.CONTEXT_UNAVAILABLE


def test_scrubbing_takes_whole_words_only() -> None:
    pattern = compile_patterns(["HSR", "  "])
    assert scrub_text("HSR and hsr but not HSRX", pattern) == (
        f"{REMOVED} and {REMOVED} but not HSRX"
    )
    assert compile_patterns([""]) is None
