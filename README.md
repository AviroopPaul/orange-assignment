# Errand Agent

A chat agent that arranges home-service errands. This is the Orange founding AI engineer assignment.

**Status:** the server, agent, LLM and workers run. The tool gateway, mock provider, approvals and
memory are not built yet, so the agent can only chat for now. It says so if you ask it to book.

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
| `make smoke` | Check the running stack end to end, using the live model |
| `make test` | Unit tests. No Docker or network needed |
| `make test-integration` | Tests on real PostgreSQL and Redis, including killed workers and Redis wipes |
| `make check` | Lint, format, types, import rules and unit tests |
| `make logs` / `make ps` | Follow logs / show service state |
| `make down` | Stop and keep data |
| `make reset` | Delete all data and start again |

## Services

| Service | Job |
|---|---|
| `postgres` | Source of truth: databases `errand`, `adk_sessions`, `mock_provider` |
| `redis` | Job stream and turn lock. It never holds the truth |
| `migrate` | One-shot job: Alembic upgrade and ADK tables |
| `api` | FastAPI: chat turns (ADK inside), traces, health |
| `worker` | Runs jobs. Does not load ADK |
| `scheduler` | Enqueues the sweep every 15 s. Run exactly one copy |

## Change the schema

Edit a model, then generate a migration and review it:

```bash
docker-compose run --rm --no-deps --user root -v "$PWD/server/alembic:/srv/alembic" migrate alembic revision --autogenerate -m "describe the change"
```

`make test-integration` fails if models and migrations disagree.
