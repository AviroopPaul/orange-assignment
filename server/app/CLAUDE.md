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
    agent/        ADK adapter, Gemini config, scripted model, prompt, callbacks
    jobs/         outbox, publish, sweep, lease, taskiq broker and tasks
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
- **Gemini rejects a request deadline under 10 s.** Config validation stops this at startup.

## Not built yet

See `BUILD_LOG.md` at the repo root for the full list. Until ContextAssembler exists, ADK sends the full
session history to the model.

## Checks

Run once, at the end of a change: `./scripts/check.sh`, then `make test-integration` from the repo root.
Schema change: edit a model, then generate and review a migration (see the root README).
