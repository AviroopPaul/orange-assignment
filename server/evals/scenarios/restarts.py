"""E15, E16, E29, E36 and E37: real processes killed at set points."""

import asyncio
import sys
from json import dumps

import httpx

from evals.scenario import Eval, scenario
from evals.stack import API_PORT


@scenario(
    "E15",
    "Worker killed after intent, before book",
    "Restart mid-booking",
    "Reclaimed job finishes safely. One booking at most.",
)
async def e15(run: Eval) -> None:
    card, _ = await run.card()
    # The saga reads the list price just before `book`. The worker dies during that read.
    await run.set_provider(get_provider_hang_s=30)
    await run.click(card["approval_id"])
    claimed = await run.attempt_claimed()
    await asyncio.sleep(0.3)
    await run.stack.kill_worker()
    run.expect("book_calls_when_killed", await run.book_calls(), 0)
    run.expect("intent_was_committed", claimed.status, "open")

    await run.set_provider(book_outcomes=["success"])
    await run.stack.start_worker()
    attempt = await run.attempt_closed()
    run.expect("attempt", attempt.status, "booked")
    run.expect_that("claims", attempt.claims, attempt.claims >= 2)
    run.expect("book_calls", await run.book_calls(), 1)
    run.expect("bookings", len(await run.provider_bookings()), 1)


@scenario(
    "E16",
    "Worker killed after book, before recording",
    "Restart mid-booking",
    "Status finds the booking. No second one.",
)
async def e16(run: Eval) -> None:
    card, _ = await run.card()
    # The booking commits, and then the provider holds the reply. The worker never sees it.
    await run.set_provider(book_outcomes=["timeout_booked"], timeout_hang_s=30)
    await run.click(card["approval_id"])
    await run.wait_until("the provider to book", run.provider_bookings, 10)
    await run.stack.kill_worker()
    during = (await run.attempts())[0]
    run.expect("attempt_when_killed", during.status, "open")
    run.expect("result_not_recorded", during.last_result, None)
    run.expect("call_was_noted_first", during.book_calls, 1)

    await run.set_provider()
    await run.stack.start_worker()
    attempt = await run.attempt_closed()
    run.expect("attempt", attempt.status, "booked")
    run.expect("book_calls_at_provider", await run.book_calls(), 1)
    run.expect("bookings", len(await run.provider_bookings()), 1)
    events = await run.trace(attempt.run_id)
    run.expect("status_checked", any(e.kind == "booking_status_checked" for e in events))
    run.expect("verified_notice", (await run.notices()).count("booking_verified"), 1)


@scenario(
    "E29",
    "Saga job limit hits during book",
    "Limit exhaustion",
    'User sees "not confirmed yet". Next delivery resolves by status.',
)
async def e29(run: Eval) -> None:
    await run.stack.start_worker(JOB_TIME_LIMIT_S="2", JOB_LEASE_S="3", TOOL_TIMEOUT_S="30")
    card, _ = await run.card()
    await run.set_provider(book_outcomes=["timeout_booked"], timeout_hang_s=6)
    await run.click(card["approval_id"])

    async def told() -> bool:
        return "booking_not_confirmed" in await run.notices()

    await run.wait_until("the not-confirmed notice", told, 15)
    run.expect("no_success_yet", "booking_verified" in await run.notices(), False)

    attempt = await run.attempt_closed()
    run.expect("attempt", attempt.status, "booked")
    run.expect("book_calls_at_provider", await run.book_calls(), 1)
    notices = await run.notices()
    run.expect(
        "order_of_notices",
        notices.index("booking_not_confirmed") < notices.index("booking_verified"),
    )
    run.expect("bookings", len(await run.provider_bookings()), 1)


@scenario(
    "E36",
    "Worker killed on a quiet queue",
    "Restart mid-booking",
    "Sweep finds the expired lease and finishes the job, with no new user traffic.",
)
async def e36(run: Eval) -> None:
    card, _ = await run.card()
    await run.set_provider(get_provider_hang_s=30)
    await run.click(card["approval_id"])
    await run.attempt_claimed()
    await asyncio.sleep(0.3)
    await run.stack.kill_worker()
    await run.set_provider(book_outcomes=["success"])

    # Nothing more is sent after this point, apart from what the scheduler sends by itself.
    await run.stack.start_worker()
    attempt = await run.attempt_closed()
    run.expect("attempt", attempt.status, "booked")
    run.expect("bookings", len(await run.provider_bookings()), 1)
    # The search, the card and the click. The worker's own work joins the click's run.
    run.expect("no_new_runs", len(await run.runs()), 3)


@scenario(
    "E37",
    "API killed in the middle of a turn",
    "Restart mid-booking",
    "The next message works. No booking is lost or doubled. The dead run gets a final outcome.",
)
async def e37(run: Eval) -> None:
    stack = run.stack
    process = stack._spawn(
        "api",
        [
            sys.executable,
            "-m",
            "uvicorn",
            "--factory",
            "evals.api_process:create",
            "--port",
            str(API_PORT),
            "--log-level",
            "warning",
        ],
        {**stack.env, "EVAL_PAUSE_S": "60", "PROCESS_NAME": "api"},
    )
    stack.api_process = process
    url = f"http://127.0.0.1:{API_PORT}"
    async with httpx.AsyncClient(timeout=70) as client:
        for _ in range(300):
            try:
                if (await client.get(f"{url}/healthz")).status_code == 200:
                    break
            except httpx.HTTPError:
                await asyncio.sleep(0.1)
        body = dumps(
            {"user_id": run.user, "session_id": run.session, "message": "Find me a plumber"}
        )
        turn = asyncio.create_task(
            client.post(f"{url}/chat", content=body, headers={"content-type": "application/json"})
        )

        async def model_is_running() -> bool:
            return bool(await run.contexts())

        await run.wait_until("the turn to reach the model", model_is_running, 20)
        stack._kill(process)
        stack.api_process = None
        died = await asyncio.gather(turn, return_exceptions=True)
    run.expect("client_lost_the_connection", isinstance(died[0], httpx.HTTPError))
    dead = (await run.runs())[0]
    run.expect("run_open_at_the_kill", dead.finished_at, None)

    reply = await run.say("Hello again", "Hello. What do you need?")
    run.expect("next_message_outcome", reply["outcome"], "needs_input")
    run.expect("next_message_reason", reply["reason"], None)

    async def closed() -> object:
        rows = [row for row in await run.runs() if row.id == dead.id]
        return rows[0].finished_at

    await run.wait_until("the dead run to be closed", closed, 40)
    final = next(row for row in await run.runs() if row.id == dead.id)
    run.expect("dead_run_outcome", final.outcome, "tool_failed")
    run.expect("dead_run_reason", final.reason, "process_died")
    run.expect("bookings", len(await run.provider_bookings()), 0)
    run.note("The kill is mid-turn. A kill between the yes and its publish is covered by E31.")
