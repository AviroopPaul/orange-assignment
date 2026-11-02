# Build log

What is built, what is left, and how we know. Requirement IDs come from the [blueprint](docs/blueprint.html). The brief is `Orange_Engineering_Assignment.pdf` (kept out of git).

**Update this file with every change.** Move a status only when there is proof.

**Status key:**
- **Done:** Built and tested for the full requirement.
- **Partial:** Some of it works. The row says what is missing.
- **Not started:** Nothing built yet.

## Where we stand

Last updated: 2 Nov 2026.

| Area | Done | Partial | Not started | Total |
|---|---|---|---|---|
| Functional requirements | 0 | 3 | 11 | 14 |
| Non-functional requirements | 1 | 10 | 9 | 20 |
| Eval scenarios | 0 | 9 | 27 | 36 |
| Eval invariants | 0 | 0 | 8 | 8 |
| Deliverables | 0 | 2 | 5 | 7 |

**In short:** the foundation runs, but none of the agent's real work exists yet. It can chat and collect details. It cannot search, hold, book, cancel, reschedule or remember anything across sessions.

## What you can test today

Run `make up`, then open `http://localhost:3000`.

| Flow | What to expect |
|---|---|
| Chat with the agent | Live Gemini replies. It asks only for missing details |
| Correct a detail | Change the budget, and it keeps the area and service |
| Reload the page | The chat stays. The agent still knows the context |
| Start a new chat | A new session that knows nothing about the old one |
| See what a turn did | The trace panel shows outcome, latency, tokens and cost. "View trace" switches between replies |
| Check the services | The status bar shows the API, PostgreSQL, Redis, worker, scheduler and Gemini key |
| Run a job | "Run test job" sends a job through the outbox, Redis and worker |
| Break something | `docker stop errand-agent-redis-1`, then chat. You get a clear reply, and the status bar names Redis. Use `docker start` to recover |

**The brief's three example requests do not finish yet.** "Find a plumber", "Move my AC servicing" and "Book the same electrician" all stop at collecting details, because the tools do not exist. The agent says so.

## Functional requirements

| ID | Requirement | Status | Built | Left |
|---|---|---|---|---|
| F1 | Collect only missing details | Partial | System prompt tells the model to do this. A live test confirmed it | `update_request`, `missing_fields`, and the gateway refusing a search with fields missing |
| F2 | Keep context on a correction | Partial | ADK session history. A live test kept area and service after a budget change | Field-by-field merge on the workflow row, with source and time |
| F3 | Explicit workflow | Not started | | State machine, transition table, version column |
| F4 | Compare providers | Not started | | Search tools, hard-limit filter, model comparison |
| F5 | Book with approval | Not started | | Hold, approval card, saga |
| F6 | Cancel with approval | Not started | | Approval path, retry on 503 |
| F7 | Reschedule with approval | Not started | | Approval path, `slot_conflict` handling |
| F8 | Refer to past bookings | Not started | | `list_bookings` resolution |
| F9 | Remember across sessions | Not started | | `memories` table and domain |
| F10 | Inspect, correct, forget | Not started | Session delete was tested in a spike | Chat and REST, supersede, tombstone, `turn_view`, session rotation |
| F11 | Explicit vs inferred | Not started | | `propose_memory` with the quote check |
| F12 | Proactive cancellation | Not started | Worker and scheduler run, and the sweep job proves it | Provider poll job, notify policy, replacement search, notifications |
| F13 | Real LLM drives the agent | Partial | Gemini through ADK reads requests and replies | It has no tools to choose from yet |
| F14 | Mock tools match the contract | Not started | `mock_provider` database exists, empty | Service, all 8 tools, seeded outcomes, scenarios, fake clock, 70/15/15 test |

## Non-functional requirements

| ID | Requirement | Status | Built and proven | Left |
|---|---|---|---|---|
| N1 | Hard run limits | Partial | 8 model calls, 120 s per run, 30 s per request, 45 s per job. Tested, including a real stuck model | 12 tool calls per run (needs the gateway) |
| N2 | Clear result on every exit | Partial | Every chat exit maps to an outcome and a reply. All nine failure reasons are tested | `needs_input`, `awaiting_approval` and `verifying` are unused until the workflow exists. Saga exits |
| N3 | Approval enforced by code | Not started | | Gateway, approval records, tokens |
| N4 | Material change voids approval | Not started | Conditional update proven in a spike | Hash and recheck |
| N5 | No duplicate bookings | Not started | Partial unique index proven in a spike. Job-level leases built | Idempotency keys, attempts, index, replay |
| N6 | Verify after a timeout | Not started | | Saga with VERIFYING |
| N7 | No false success claims | Not started | System prompt forbids claims, which is not enforcement | Template replies from verified rows, `after_model_callback` guard |
| N8 | Safe across restarts | Partial | Jobs recover from a killed worker through the sweep. Sessions survive an API restart | Booking attempts, crash points in the saga, `api` kill tests |
| N9 | Tool output is data | Not started | Invented tool names run nothing | Labelled reviews, flagging, clean card, memory provenance |
| N10 | Fixed context budget | Not started | | ContextAssembler. **The full session history is sent to the model today** |
| N11 | No forgotten or unconfirmed facts | Not started | | Filter, redacted `turn_view`, marker tests |
| N12 | Log context each turn | Not started | | `context_log` |
| N13 | Trace every run | Partial | `runs` and `trace_events`: model calls, latency, outcome, refused tools. Shown in the UI | JSONL file per run, state changes, tool arguments and sanitized results, run ID across jobs |
| N14 | Tokens and cost | Done | Usage and cost per call and per run. Tested with live and scripted models | |
| N15 | Survive model and tool failure | Partial | Request timeout, exactly one retry on 429, 503 and 504, clear reply. Tested against a local server and a stuck model | Tool failure rules (needs the saga) |
| N16 | Safe concurrency | Partial | Redis turn lock, `StaleSessionError` backstop, job leases with fencing. Only `api` writes ADK sessions | Workflow version check, booking attempt leases |
| N17 | Reproducible offline evals | Partial | Scripted model through the real ADK loop. Injectable clock. Unit tests need no network | Recorded Gemini replies, seeded mock, `evals` service on an internal network |
| N18 | Survive Redis or PostgreSQL loss | Partial | Redis wipe, Redis down and PostgreSQL down all checked on the live stack. Redis wipe has automated tests | Automated E34 and E35 on the full stack, with bookings in flight |
| N19 | One-command local run | Partial | `make up` from clean, with ordered health checks. Migrations safe to repeat | Automated clean-start check. `mock-provider` is not in the stack yet |
| N20 | Secrets stay out | Partial | Key comes from the environment at run time and is hidden in `repr`. Manual scan of repo, image history and trace events found nothing | Automated scan. Trace sanitizing, because refused tool arguments are stored as sent |

## Components

| Component | Status | Notes |
|---|---|---|
| `postgres`, `redis`, `migrate` | Done | Three databases. `mock_provider` is empty |
| `api` | Partial | Has `/chat`, `/runs/{id}/trace`, `/healthz`, `/readyz`, `/system/*`. Missing `/approvals/{id}`, `/memories`, `/events` |
| `worker`, `scheduler` | Partial | Outbox, leases and sweep work. Saga and proactive jobs are missing |
| `web` | Partial | Chat, trace after each turn, status bar. Missing approval card, memory page, live trace through SSE |
| `mock-provider` | Not started | |
| `evals` | Not started | |
| Tool gateway | Not started | Left out on request |
| ContextAssembler | Not started | |

## Evaluation

The eval harness and the invariant checker (I1 to I8) are not built, so there is no eval table yet. Partial below means an existing test covers the same behaviour at a lower level. It will still need a real scenario.

| ID | Scenario | Status | Existing cover |
|---|---|---|---|
| E04 | Model tries `book` directly | Partial | Invented tool runs nothing, and is traced |
| E27 | Model loops until the call limit | Partial | Unit tests |
| E30 | Model 503 and unexpected tool error | Partial | Model side only |
| E31 | Redis wiped with a job queued | Partial | Integration test, using a ping job |
| E32 | Job delivered twice | Partial | Unit test of the lease and fence |
| E33 | Two messages at once | Partial | Unit and integration tests |
| E34 | Redis down during chat | Partial | Unit tests and a manual check. No approval yet |
| E35 | PostgreSQL down | Partial | Unit tests and a manual check. No approval yet |
| E36 | Worker killed on a quiet queue | Partial | Integration test, using a ping job |
| E01 to E03, E05 to E26, E28, E29 | All others | Not started | Need the mock, gateway, approvals, saga and memory |

## Deliverables

| Deliverable | Status | Notes |
|---|---|---|
| Git repo | Partial | [Private repo](https://github.com/AviroopPaul/orange-assignment) has the foundation |
| Design note, 1 page | Not started | Write once the core exists |
| Eval table | Not started | Needs the harness |
| Three real bugs | Partial | [BUGS.md](BUGS.md) has three candidates and one smaller bug |
| End-to-end trace | Not started | Needs a timeout-after-success run |
| Demo, 3 to 5 min | Not started | The UI can now show chat and trace side by side |
| AI assistant transcript | Not started | Export at the end |

## Changes from the blueprint

The blueprint on claude.ai is version 4. These are not in it yet.

- **Layout:** domain-led, with standard FastAPI files in each domain and shared code in `core/`.
- **Request deadline:** at least 10 s, because Gemini rejects less. Config validation enforces it.
- **Timeouts are not retried:** the single retry covers 429, 503 and 504 only.
- **Added `tracing` early:** it was needed to prove the LLM works.
- **Added a ping job and `/system/*`:** these prove the job path, and the UI uses them.
- **Chat history in the browser:** the server has no history endpoint, so the UI keeps the visible chat in `localStorage`. The model's memory of the chat is on the server.

## Suggested order

1. **Mock provider:** all 8 tools, seeded outcomes, scenarios, fake clock.
2. **Tool gateway:** allowlist, caller rights, budget, retries, sanitizing, tracing.
3. **Workflow and search:** state machine, `update_request`, provider comparison.
4. **Approvals and booking saga:** the core of the assignment.
5. **Cancel and reschedule:** on the same approval path.
6. **Memory and ContextAssembler:** includes forgetting and the token budget.
7. **Proactive cancellation, SSE and JSONL traces.**
8. **Eval harness, table and invariants.**
9. **Design note, trace excerpt, demo and transcript.**

## Log

### 2 Nov 2026: React UI and this log

- Added `web`: React and Vite, with `chat`, `tracing` and `system` domains. nginx serves it on port 3000 and proxies `/api`.
- nginx resolves `api` at request time, so replacing the `api` container does not break the proxy. Checked by recreating the container with a new IP.
- **Proof:** 23 UI tests pass. Headless Chromium drove the real UI against live Gemini: chat, correction, trace switching, reload, new chat, test job. No browser errors and no sideways scroll at phone width.

### 2 Nov 2026: Server, agent, LLM and workers

- Added `api`, `worker`, `scheduler`, `migrate`, PostgreSQL and Redis in Compose. Commit `7fa5352`.
- **Proof:** 139 unit tests and 8 integration tests pass. The live smoke test passes. Checked Redis down, PostgreSQL down, API restart, a stuck model and no API key on the running stack.

### 2 Nov 2026: Design

- Blueprint versions 1 to 4. The stack moved from SQLite to PostgreSQL, Redis and Docker Compose. Spikes tested ADK, `taskiq` and Gemini behaviour.
