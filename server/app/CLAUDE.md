# Errand Agent server: the map

Domain-led FastAPI. Each domain uses the standard files it needs, and nothing more.

```
app/
  main.py         api process (create_app, lifespan, error handlers)
  worker.py       worker process (taskiq broker and startup)
  scheduler.py    scheduler process, one copy only
  migrate.py      one-shot job: Alembic upgrade, then ADK tables
  healthcheck.py  Compose health check for worker and scheduler
  container.py    composition root (constructor injection)
  web.py          FastAPI dependencies
  core/           config, clock, database, turn lock, heartbeats
  domains/
    chat/         turn controller: limits, lock, outcomes, replies
    agent/        ADK adapter, Gemini config, scripted model, prompt, callbacks, model tools
    gateway/      the only path to any tool: rights, schema, budget, retries, sanitize, trace
    jobs/         outbox, publish, sweep, lease, taskiq broker and tasks
    workflow/     state machine, the errand's details, hard limits on search results
    bookings/     approvals, the approval hash, reading a yes or no, and the booking saga
    notifications/ what the system tells a user outside a chat turn
    memory/       facts across chats, the redactable chat, the context assembler and its log
    tracing/      runs, trace events, cost
    system/       health, status, ping job
```

Domain files: `router.py` (HTTP only), `schemas.py` (Pydantic), `models.py` (SQLAlchemy),
`repository.py` (queries), `service.py` (business logic).

## Rules

Enforced by `tests/unit/test_architecture.py` and `.importlinter`.

1. **Routers do not touch the database.** No SQLAlchemy, models or repositories in a router.
2. **Services own transactions.** A service opens `session_scope` and passes the session to a
   repository. Repositories only flush.
3. **No transaction across a network call.** Read, close the scope, do the slow call, then write in a
   new scope.
4. **Only `domains/agent` imports ADK.** `container.py` and `migrate.py` are the exceptions. Workers
   never load ADK.
5. **`core` imports no domain.** `jobs` knows no other domain; handlers are registered in
   `container.py`.

## Facts that are easy to get wrong

- **PostgreSQL is the truth, Redis only carries work.** Change a domain row and enqueue its job in one
  transaction with `JobService.enqueue_in`.
- **Jobs can run twice, and can run twice at once.** `JobService.execute` claims a lease, and `attempt`
  fences a worker that lost its lease. Handlers must still be safe to repeat.
- **Recovery comes from `sweep`, not the broker.** taskiq only reclaims a dead worker's job when a new
  job arrives.
- **Only `api` writes to ADK sessions, under the turn lock.** A second writer kills the running turn
  with `StaleSessionError`. Workers write to `errand` instead.
- **Every exit from a chat turn maps to one `Outcome`.** New failures need a `FailureReason` and a reply
  in `FAILURE_REPLIES`.
- **Every tool call goes through `ToolGateway.call`.** Only `gateway/client.py` talks to the provider. `book`, `cancel` and `reschedule` need `Caller.SAGA` and a valid approval. The model never gets them as tools.
- **The model never chooses search inputs.** It saves details with `update_request`, and code checks them against the user's words. `search_providers` takes no inputs.
- **The model sees days, not times, until it asks.** `search_providers` returns each provider's open days. `get_open_slots` returns one provider's times on one day, from the stored search.
- **The model never works out a date.** `resolve_dates` reads the user's own words and saves the dates with no question. The next reply must show the exact dates: `ChatService` adds them if the model left them out. Bookings are approved on the card, not here.
- **A yes comes only from the raw message or a click.** `decide_from_message` reads it before the model runs. The model can call `propose_booking`, `propose_cancel` and `propose_reschedule`, which only show a card.
- **A card comes from the approval row only.** Never put model text or provider reviews on it.
- **`book` needs a token built from what the saga sees now.** `token_for` hashes the approved facts with the current list price, and the verifier refuses any difference. A refused call was never sent.
- **Only the source of truth can report success.** `get_booking_status` runs after every `book`, and `list_bookings` after every `cancel` or `reschedule`, whatever the call returned. Success text is a template, written in the same transaction that closes the attempt.
- **A word on a card counts only for that card's action.** `read_decision` takes the action, so "book it" cannot approve a cancel. Pass the action whenever a card is open.
- **Read again after taking a row lock.** A row read before the wait can be out of date, and SQLite tests will not show it. See `BUGS.md`.
- **The saga commits before each call,** so a job that dies or repeats continues from what is known. Open attempts come back through `BookingSaga.recover`, which the sweep runs.
- **Only code moves workflow state**, through `WorkflowService`. Add a move to `TRANSITIONS` before using it.
- **A refusal is a result, not an exception.** Only a spent budget raises, because it must end the run.
- **Reviews are data.** Safety comes from what the model can do, not from filtering text. Flags inform the trace; they do not block.
- **A 429 is not an outage.** `RateLimitAwareGemini` waits if Gemini asks for 30 s or less, inside one model call. Longer waits become `ModelRateLimited`, and the user gets a busy reply.
- **Gemini rejects a request deadline under 10 s.** Config validation stops this at startup.
- **The model never sees ADK's history.** `agent/context.py` replaces it on every model call with what `memory/assembler.py` builds. ADK's session only runs the loop. If the context cannot be built within budget or logged, the call does not run.
- **Read the chat from `turn_view`, not from ADK.** ChatService writes each message and reply. Only the current run comes from ADK.
- **A fact counts only if it is explicit or confirmed.** The filter is in SQL. `propose_memory` cannot trust a fact unless the user's own sentence states it, and the model has no confirm tool. A guess is asked about in the chat, and `answer_from_message` reads a whole-message yes or no before the model runs, after any open booking card.
- **Removing a fact has two steps.** `_scrub_now` cleans what the next model call reads. `finish_scrub` runs after the reply is stored, because the reply may quote the fact, and only then wipes the value. ADK sessions are deleted under the turn lock, never mid-run.

## Not built yet

See `BUILD_LOG.md` at the repo root for the full list.

## Checks

While working, run only the tests a change touches. GitHub CI runs `./scripts/check.sh` and `make test-integration` on every PR.
Schema change: edit a model, then generate and review a migration (see the root README).
