# Errand Agent

A chat agent that arranges home-service errands. This is the Orange founding AI engineer assignment.

**Status:** the server, agent, LLM, workers, UI, mock provider and tool gateway run. The agent can
collect details, search and compare providers within your limits, and book, cancel or move a
booking after your approval. It remembers lasting facts across chats, which you can correct or
forget, and every model call gets a logged context within a fixed token budget.
[BUILD_LOG.md](BUILD_LOG.md) tracks what is built and what is left against every requirement.

The full design is in [docs/blueprint.html](docs/blueprint.html). Code layout and rules are in
[server/app/CLAUDE.md](server/app/CLAUDE.md).

## Run it

You need Docker. Live chat also needs a Gemini key.

```bash
export GOOGLE_API_KEY=your-key
```

```bash
make up
```

Open the app at `http://localhost:3000`. It shows chat and the run trace side by side, plus the state
of each service.

The API is at `http://localhost:8000`, with docs at `/docs`. Without a key the stack still starts, and
chat replies that no key is set.

```bash
curl -s localhost:8000/chat -H 'content-type: application/json' -d '{"user_id":"me","message":"I need a plumber in Indiranagar"}'
```

Send back the returned `session_id` to continue a chat. To see what a run did:

```bash
curl -s localhost:8000/runs/RUN_ID/trace
```

## Commands

| Command | What it does |
|---|---|
| `make up` | Build, start and wait until every service is healthy |
| `make demo` | The stack for the demo: provider controls on, and the console at `http://localhost:8100/admin/panel`. `make demo-reset` deletes all data first. See `docs/demo-plan.md` |
| `make smoke` | Check the running stack end to end, using the live model |
| `make test` | Server unit tests. No Docker or network needed |
| `make test-web` | UI tests. No Docker or network needed |
| `make test-integration` | Tests on real PostgreSQL and Redis, including killed workers and Redis wipes |
| `make evals` | Every eval scenario, offline, on its own throwaway PostgreSQL and Redis. Writes `evals/results.md`, `.json` and `.csv`. Needs no key |
| `make check` | Lint, format, types, import rules and all unit tests |
| `make web-dev` | UI with hot reload on port 5173, against the running API |
| `make transcript` | Append new Claude Code conversation to `docs/transcript.md` |
| `make logs` / `make ps` | Follow logs / show service state |
| `make down` | Stop and keep data |
| `make reset` | Delete all data and start again |

## Services

| Service | Job |
|---|---|
| `postgres` | Source of truth: databases `errand`, `adk_sessions`, `mock_provider` |
| `mock-provider` | Stands in for an outside provider: the 8 tools from the brief, on port 8100 |
| `redis` | Job stream and turn lock. It never holds the truth |
| `migrate` | One-shot job: Alembic upgrade and ADK tables |
| `api` | FastAPI: chat turns (ADK inside), approvals, memory, traces, health, and news by SSE |
| `worker` | Runs jobs, including the booking saga and the provider poll. Does not load ADK |
| `scheduler` | Enqueues the sweep every 15 s and the provider poll every 20 s. Run exactly one copy |
| `web` | nginx serving the React app on port 3000, and proxying `/api` to `api` |

## Change the schema

Edit a model, then generate a migration and review it:

```bash
docker-compose run --rm --no-deps --user root -v "$PWD/server/alembic:/srv/alembic" migrate alembic revision --autogenerate -m "describe the change"
```

`make test-integration` fails if models and migrations disagree.

GitHub CI (`.github/workflows/ci.yml`) runs the same checks and the integration tests on every pull request and on pushes to `main`.
