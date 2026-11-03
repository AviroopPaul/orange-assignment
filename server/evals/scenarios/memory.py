"""E21 to E23 and E26: what the model may know."""

from sqlalchemy import text

from app.domains.agent.scripted import ToolCall
from app.domains.memory import seed as seed_data
from app.domains.memory.seed import seed_memories
from evals.scenario import Eval, scenario


def _remember(key: str, value: str, quote: str) -> ToolCall:
    return ToolCall("propose_memory", {"key": key, "value": value, "quote": quote})


async def _seeded(run: Eval) -> None:
    core = run.stack.container.core
    await seed_memories(run.stack.db, core.clock)
    run.user, run.session = seed_data.SEED_USER, seed_data.SEED_SESSION


def _seen(run: Eval) -> str:
    return str(run.stack.switchboard.active.requests)


OLD_AREA = "MARKER-OLD-AREA"


@scenario(
    "E21",
    "Correct a memory",
    "Memory correction and forgetting",
    "Old value never reaches the model again, and leaves the stored logs too.",
)
async def e21(run: Eval) -> None:
    run.never_show(OLD_AREA)
    said = f"my area is {OLD_AREA}"
    await run.say(said, _remember("area", OLD_AREA, said), "Saved.")
    later = "now my area is Koramangala"
    await run.say(later, _remember("area", "Koramangala", later), "Changed to Koramangala.")
    await run.say("What next?", "Sure.")

    seen = _seen(run)
    run.expect("old_value_seen", OLD_AREA in seen, False)
    run.expect("new_value_seen", "area: Koramangala" in seen, True)
    rows = {row.status: row for row in await run.memories()}
    run.expect("statuses", sorted(rows), ["active", "superseded"])
    run.expect("superseded_points_at_new", rows["superseded"].superseded_by, rows["active"].id)


@scenario(
    "E22",
    "Forget a memory also present in old chat",
    "Memory correction and forgetting",
    "Marker is absent from all later contexts, the old chat, traces and the ADK session.",
)
async def e22(run: Eval) -> None:
    marker = seed_data.FORGET_ME_MARKER
    run.never_show(marker)
    await _seeded(run)
    await run.say("What do you know about my gate?", "You told me.")
    run.expect("visible_before_forgetting", marker in _seen(run), True)

    await run.say(
        "Please forget my gate code",
        ToolCall("forget_memory", {"key": seed_data.FORGET_ME_KEY}),
        "Forgotten.",
    )
    await run.say("Hello", "Hello.")
    run.expect("seen_after_forgetting", marker in _seen(run), False)

    forgotten = [row for row in await run.memories() if row.key == seed_data.FORGET_ME_KEY]
    run.expect("status", forgotten[0].status, "forgotten")
    run.expect("value_wiped", marker in forgotten[0].value, False)
    async with run.stack.db.session_scope() as session:
        in_chat = await session.scalar(
            text("SELECT count(*) FROM turn_view WHERE text LIKE :m"), {"m": f"%{marker}%"}
        )
        in_traces = await session.scalar(
            text("SELECT count(*) FROM trace_events WHERE data::text LIKE :m"), {"m": f"%{marker}%"}
        )
    run.expect("in_stored_chat", in_chat, 0)
    run.expect("in_trace_events", in_traces, 0)
    in_files = [
        path.name for path in run.stack.trace_dir.glob("*.jsonl") if marker in path.read_text()
    ]
    run.expect("in_trace_files", in_files, [])
    async with run.stack.adk_db.session_scope() as session:
        in_adk = await session.scalar(
            text("SELECT count(*) FROM events WHERE event_data::text LIKE :m"),
            {"m": f"%{marker}%"},
        )
    run.expect("in_adk_events", in_adk, 0)


@scenario(
    "E23",
    "Inferred fact, then confirmed",
    "Memory correction and forgetting",
    "Excluded before confirmation, included after.",
)
async def e23(run: Eval) -> None:
    await run.say(
        "I think they spoke Hindi",
        _remember("language", "Bengali", "I think they spoke Hindi"),
        "Noted.",
    )
    row = (await run.memories())[0]
    run.expect("source", row.source, "inferred")
    await run.say("Next", "Sure.")
    run.expect("used_before_confirming", "language: Bengali" in _seen(run), False)

    response = await run.stack.http.post(f"/memories/{row.id}/confirm", json={"user_id": run.user})
    run.expect("confirm_status", response.status_code, 200)
    await run.say("Next again", "Sure.")
    run.expect("used_after_confirming", "language: Bengali" in _seen(run), True)


@scenario(
    "E26",
    "60 memories and a long chat",
    "Context budget",
    "Within budget on every call, mid-run calls included. Only relevant facts sent.",
)
async def e26(run: Eval) -> None:
    run.never_show(
        *seed_data.UNCONFIRMED_MARKERS,
        *seed_data.SUPERSEDED_MARKERS,
        *seed_data.FORGOTTEN_MARKERS,
    )
    await _seeded(run)
    run.expect("memories_seeded", len(await run.memories()), 60)
    await run.search()

    contexts = await run.contexts()
    run.expect("every_model_call_logged", len(contexts), len(run.stack.switchboard.active.requests))
    biggest = max(row.estimated_tokens for row in contexts)
    run.expect_that("largest_context_of_8000", biggest, biggest <= 8000)
    run.expect_that("calls_near_the_budget", biggest, biggest > 4000)
    seen = _seen(run)
    run.expect("relevant_fact_sent", "MARKER-PLUMBER-00" in seen, True)
    run.expect("other_service_fact_sent", "MARKER-ELECTRICIAN-00" in seen, False)
    run.expect("old_chat_trimmed", "earlier messages of this chat are not shown" in seen, True)
