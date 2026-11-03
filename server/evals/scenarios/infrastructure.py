"""E31 to E35: Redis or PostgreSQL goes away, or a job arrives twice."""

import asyncio
import uuid

from sqlalchemy import select
from taskiq_redis import RedisStreamBroker

from app.domains.agent.scripted import Pause
from app.domains.jobs.broker import TaskiqJobPublisher
from app.domains.jobs.models import OutboxJob
from evals.scenario import Eval, scenario


async def _job(run: Eval) -> OutboxJob:
    async with run.stack.db.session_scope() as session:
        rows = list(await session.scalars(select(OutboxJob)))
    return rows[0]


@scenario(
    "E31",
    "Redis wiped with a booking job queued",
    "Infrastructure loss",
    "Sweep republishes from outbox. One booking.",
)
async def e31(run: Eval) -> None:
    card, _ = await run.card()
    await run.set_provider(book_outcomes=["success"])
    await run.stack.kill_worker()
    await run.click(card["approval_id"])
    job = await _job(run)
    run.expect("job_was_published", job.status, "published")
    await run.stack.redis.flushdb()

    await run.stack.start_worker()
    attempt = await run.attempt_closed()
    run.expect("attempt", attempt.status, "booked")
    run.expect("book_calls", await run.book_calls(), 1)
    run.expect("bookings", len(await run.provider_bookings()), 1)


@scenario(
    "E32",
    "Same job delivered twice while the first still runs",
    "Infrastructure loss",
    "Lease stops the second. One provider side effect.",
)
async def e32(run: Eval) -> None:
    card, _ = await run.card()
    await run.set_provider(book_outcomes=["timeout_booked"], timeout_hang_s=4)
    await run.click(card["approval_id"])
    await run.wait_until("the provider to book", run.provider_bookings, 10)

    broker = run.stack.container.broker
    assert isinstance(broker, RedisStreamBroker)
    publisher = TaskiqJobPublisher(broker)
    job = await _job(run)
    await publisher.publish(uuid.UUID(str(job.id)))
    await publisher.publish(uuid.UUID(str(job.id)))
    await asyncio.sleep(0.5)

    attempt = await run.attempt_closed()
    run.expect("attempt", attempt.status, "booked")
    run.expect("book_calls", await run.book_calls(), 1)
    run.expect("bookings", len(await run.provider_bookings()), 1)
    claims = await run.all_events("saga_claimed")
    run.expect("saga_ran_once", len(claims), 1)


@scenario(
    "E33",
    "Two messages at once on one session",
    "Infrastructure loss",
    "Lock serialises them. No `StaleSessionError` reaches the user.",
)
async def e33(run: Eval) -> None:
    run._script((Pause(1, "First reply."), "Second reply."))
    first, second = await asyncio.gather(
        run.post_chat("First message"), run.post_chat("Second message")
    )
    reasons = sorted(str(item["reason"]) for item in (first, second))
    run.expect("reasons", reasons, ["None", "None"])
    run.expect("outcomes", {first["outcome"], second["outcome"]}, {"needs_input"})
    run.expect(
        "replies",
        sorted([first["reply"], second["reply"]]),
        ["First reply.", "Second reply."],
    )
    # The first holds the model for 1 s, so with no lock they would finish about together.
    ends = [row.finished_at for row in await run.runs() if row.finished_at]
    run.expect("both_finished", len(ends), 2)
    run.expect("no_conflict_events", await run.all_events("unexpected_error"), [])


@scenario(
    "E34",
    "Redis down during chat and approval",
    "Infrastructure loss",
    "Clear reply. Work finishes after Redis returns.",
)
async def e34(run: Eval) -> None:
    card, _ = await run.card()
    await run.set_provider(book_outcomes=["success"])
    run.stack.redis_proxy.down()

    chat = await run.say("Are you there?", "This must not run.")
    run.expect("chat_outcome", chat["outcome"], "tool_failed")
    run.expect("chat_reason", chat["reason"], "lock_unavailable")
    run.expect("card_still_shown", chat["approval"]["approval_id"], card["approval_id"])
    run.expect("model_not_called", len(run.stack.switchboard.active.requests), 0)

    answer = await run.click(card["approval_id"])
    run.expect("click_status", answer.json()["status"], "in_progress")
    await asyncio.sleep(2)
    run.expect("nothing_booked_during_the_outage", await run.book_calls(), 0)
    job = await _job(run)
    run.expect("job_waits_in_postgres", job.status, "pending")

    run.stack.redis_proxy.up()
    attempt = await run.attempt_closed()
    run.expect("attempt", attempt.status, "booked")
    run.expect("bookings", len(await run.provider_bookings()), 1)


@scenario(
    "E35",
    "PostgreSQL down during approval",
    "Infrastructure loss",
    "No side-effect call. Clear error. Nothing is consumed.",
)
async def e35(run: Eval) -> None:
    card, _ = await run.card()
    await run.set_provider(book_outcomes=["success"])
    run.stack.pg_proxy.down()

    answer = await run.click(card["approval_id"])
    run.expect("click_status_code", answer.status_code, 503)
    run.expect("click_error", answer.json()["error"], "database_unavailable")
    chat = await run.stack.http.post(
        "/chat", json={"user_id": run.user, "session_id": run.session, "message": "Hello"}
    )
    run.expect("chat_status_code", chat.status_code, 503)
    run.expect("book_calls_during_outage", await run.book_calls(), 0)

    run.stack.pg_proxy.up()
    approval = (await run.approvals())[0]
    run.expect("approval_not_consumed", approval.status, "pending")
    retry = await run.click(card["approval_id"])
    run.expect("retry_status", retry.json()["status"], "in_progress")
    attempt = await run.attempt_closed()
    run.expect("attempt", attempt.status, "booked")
    run.expect("bookings", len(await run.provider_bookings()), 1)
