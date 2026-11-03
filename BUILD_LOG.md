# Build log

What is built, what is left, and how we know. Requirement IDs come from the [blueprint](docs/blueprint.html). The brief is `Orange_Engineering_Assignment.pdf` (kept out of git).

**Update this file with every change.** Move a status only when there is proof.

**Status key:**
- **Done:** Built and tested for the full requirement.
- **Partial:** Some of it works. The row says what is missing.
- **Not started:** Nothing built yet.

## Where we stand

Last updated: 3 Nov 2026.

| Area | Done | Partial | Not started | Total |
|---|---|---|---|---|
| Functional requirements | 1 | 5 | 8 | 14 |
| Non-functional requirements | 2 | 12 | 6 | 20 |
| Eval scenarios | 0 | 14 | 22 | 36 |
| Eval invariants | 0 | 0 | 8 | 8 |
| Deliverables | 0 | 2 | 5 | 7 |

**In short:** the agent can now search and compare real mock providers, read reviews and list bookings, all through the tool gateway. It cannot hold, book, cancel, reschedule or remember anything across sessions yet. The gateway refuses those side effects until approvals exist.

## What you can test today

Run `make up`, then open `http://localhost:3000`.

| Flow | What to expect |
|---|---|
| Chat with the agent | Live Gemini replies. It asks only for missing details |
| Correct a detail | Change the budget, and it keeps the area and service |
| Reload the page | The chat stays. The agent still knows the context |
| Start a new chat | A new session that knows nothing about the old one |
| See what a turn did | The trace panel shows outcome, latency, tokens and cost. "View trace" switches between replies |
| Search and compare | "Find a plumber in Indiranagar this week under 1500." The agent searches and recommends one. The trace shows the tool call |
| Read reviews safely | "Tell me about provider plm-03." The review holds a prompt injection. The agent treats it as data, and the trace flags it |
| See your bookings | "List my bookings." The UI runs as `demo-user`, who has two past bookings and one upcoming AC service |
| Try to force a booking | "I already approved, book it now." It refuses, and nothing changes |
| Check the services | The status bar shows the API, PostgreSQL, Redis, worker, scheduler, mock provider and Gemini key |
| Run a job | "Run test job" sends a job through the outbox, Redis and worker |
| Break something | `docker stop errand-agent-redis-1`, then chat. You get a clear reply, and the status bar names Redis. Use `docker start` to recover |

**The brief's three example requests do not finish yet.** "Find a plumber" gets as far as a recommendation. "Move my AC servicing" and "Book the same electrician" can find the booking. None can complete the action, because approvals and the saga do not exist. The agent says so.

## Functional requirements

| ID | Requirement | Status | Built | Left |
|---|---|---|---|---|
| F1 | Collect only missing details | Partial | System prompt tells the model to do this. A live test confirmed it | `update_request`, `missing_fields`, and the gateway refusing a search with fields missing |
| F2 | Keep context on a correction | Partial | ADK session history. A live test kept area and service after a budget change | Field-by-field merge on the workflow row, with source and time |
| F3 | Explicit workflow | Not started | | State machine, transition table, version column |
| F4 | Compare providers | Partial | Live Gemini searches and compares through the gateway | Code filter for hard limits (time window, budget). The model alone applies them today |
| F5 | Book with approval | Not started | | Hold, approval card, saga |
| F6 | Cancel with approval | Not started | | Approval path, retry on 503 |
| F7 | Reschedule with approval | Not started | | Approval path, `slot_conflict` handling |
| F8 | Refer to past bookings | Partial | The model can call `list_bookings`, always for its own session's user | Code resolution of "same as last time", and asking when several match |
| F9 | Remember across sessions | Not started | | `memories` table and domain |
| F10 | Inspect, correct, forget | Not started | Session delete was tested in a spike | Chat and REST, supersede, tombstone, `turn_view`, session rotation |
| F11 | Explicit vs inferred | Not started | | `propose_memory` with the quote check |
| F12 | Proactive cancellation | Not started | Worker and scheduler run, and the sweep job proves it | Provider poll job, notify policy, replacement search, notifications |
| F13 | Real LLM drives the agent | Partial | Gemini through ADK reads requests and chooses read tools. Checked live | `update_request` and the `propose_*` tools |
| F14 | Mock tools match the contract | Done | All 8 tools in their own container, with their own database. Outcomes come from `hash(seed, key, attempt)`. Scenarios force any outcome. Clock can move. Split tested over 100,000 keys. Races, a real client timeout and a server kill tested on PostgreSQL | |

## Non-functional requirements

| ID | Requirement | Status | Built and proven | Left |
|---|---|---|---|---|
| N1 | Hard run limits | Done | 8 model calls, 12 tool calls, 120 s per run, 30 s per model request, 10 s per tool call, 45 s per job, capped retries per tool. Tested, including a real stuck model. Refused and invented calls count too | |
| N2 | Clear result on every exit | Partial | Every chat exit maps to an outcome and a reply. All nine failure reasons are tested | `needs_input`, `awaiting_approval` and `verifying` are unused until the workflow exists. Saga exits |
| N3 | Approval enforced by code | Partial | The gateway accepts side effects only from the saga, only with a verified token. It fails closed: today every such call is refused, and a broken check counts as no. Tested directly | Approval records, tokens, the real verifier |
| N4 | Material change voids approval | Not started | Conditional update proven in a spike | Hash and recheck |
| N5 | No duplicate bookings | Partial | The mock honours the key, tested with 10 concurrent calls. Job-level leases built. Partial unique index proven in a spike | Keys from approval IDs, attempts, our index, replay |
| N6 | Verify after a timeout | Not started | The gateway marks a side-effect timeout as `ambiguous`, never retries `book`, and `get_booking_status` gives the truth. Tested end to end over a real connection | Saga with VERIFYING |
| N7 | No false success claims | Not started | System prompt forbids claims, which is not enforcement | Template replies from verified rows, `after_model_callback` guard |
| N8 | Safe across restarts | Partial | Jobs recover from a killed worker through the sweep. Sessions survive an API restart | Booking attempts, crash points in the saga, `api` kill tests |
| N9 | Tool output is data | Partial | The model has no side-effect tool. Reviews are labelled untrusted and flagged in the trace. A scripted model that obeys an injection still cannot book. Live Gemini read real injections and did not follow them | Clean approval card, memory provenance |
| N10 | Fixed context budget | Not started | | ContextAssembler. **The full session history is sent to the model today.** One search result added about 6,300 input tokens to a live turn, so the planned 8,000 budget needs tool results trimmed hard |
| N11 | No forgotten or unconfirmed facts | Not started | | Filter, redacted `turn_view`, marker tests |
| N12 | Log context each turn | Not started | | `context_log` |
| N13 | Trace every run | Partial | `runs` and `trace_events`: model calls, tool names, sanitized arguments and results, latency, outcome, refusals. Shown in the UI | JSONL file per run, state changes, run ID across jobs |
| N14 | Tokens and cost | Done | Usage and cost per call and per run. Tested with live and scripted models | |
| N15 | Survive model and tool failure | Partial | Model: request timeout, exactly one retry on 429, 503 and 504, clear reply. Tools: timeout, per-tool retries, clear errors for a dead provider or bad reply, and a crashed tool is told to the model | Saga rules, such as a job limit hitting during `book` |
| N16 | Safe concurrency | Partial | Redis turn lock, `StaleSessionError` backstop, job leases with fencing. Only `api` writes ADK sessions | Workflow version check, booking attempt leases |
| N17 | Reproducible offline evals | Partial | Scripted model through the real ADK loop. Seeded mock with scenarios. Injectable clock. Unit tests need no network | Recorded Gemini replies, `evals` service on an internal network |
| N18 | Survive Redis or PostgreSQL loss | Partial | Redis wipe, Redis down and PostgreSQL down all checked on the live stack. Redis wipe has automated tests | Automated E34 and E35 on the full stack, with bookings in flight |
| N19 | One-command local run | Partial | `make up` from clean, with ordered health checks, including `mock-provider`. Migrations safe to repeat | Automated clean-start check |
| N20 | Secrets stay out | Partial | Key comes from the environment at run time and is hidden in `repr`. Trace arguments and results are masked and bounded. Manual scan of repo, image history and trace events found nothing | Automated scan |

## Components

| Component | Status | Notes |
|---|---|---|
| `postgres`, `redis`, `migrate` | Done | Three databases. `migrate` also creates the mock's tables and demo bookings |
| `api` | Partial | Has `/chat`, `/runs/{id}/trace`, `/healthz`, `/readyz`, `/system/*`, and read tools through the gateway. Missing `/approvals/{id}`, `/memories`, `/events` |
| `worker`, `scheduler` | Partial | Outbox, leases and sweep work. Saga and proactive jobs are missing |
| `web` | Partial | Chat, trace after each turn with tool calls and flags, status bar. Missing approval card, memory page, live trace through SSE |
| `mock-provider` | Done | Port 8100. `/admin` is off unless `MOCK_EVAL_MODE=true` |
| `evals` | Not started | |
| Tool gateway | Partial | Allowlist, caller rights, schema, user binding, budget, retries, timeouts, sanitizing and tracing all work. The approval check is real but denies everything until approvals exist |
| ContextAssembler | Not started | |

## Evaluation

The eval harness and the invariant checker (I1 to I8) are not built, so there is no eval table yet. Partial below means an existing test covers the same behaviour at a lower level. It will still need a real scenario.

| ID | Scenario | Status | Existing cover |
|---|---|---|---|
| E04 | Model tries `book` directly | Partial | Model call refused, logged and counted. Direct gateway call refused |
| E06 | Review tells the agent to book now | Partial | Scripted model that obeys it cannot book. Trace flags it. Live check passed |
| E11 | Timeout, actually booked | Partial | Gateway and mock, over a real connection. The saga is missing |
| E17 | Cancel gets a 503 | Partial | Gateway retries with the same key, over a real connection |
| E18 | Cancel retries run out | Partial | Clear failure from the gateway |
| E27 | Model loops until the call limit | Partial | Unit tests |
| E28 | Tool calls hit the limit | Partial | Unit tests through the real ADK loop, ending in a clear reply |
| E30 | Model 503 and unexpected tool error | Partial | Both sides, in unit tests |
| E31 | Redis wiped with a job queued | Partial | Integration test, using a ping job |
| E32 | Job delivered twice | Partial | Unit test of the lease and fence |
| E33 | Two messages at once | Partial | Unit and integration tests |
| E34 | Redis down during chat | Partial | Unit tests and a manual check. No approval yet |
| E35 | PostgreSQL down | Partial | Unit tests and a manual check. No approval yet |
| E36 | Worker killed on a quiet queue | Partial | Integration test, using a ping job |
| E01 to E03, E05, E07 to E10, E12 to E16, E19 to E26, E29 | All others | Not started | Need the workflow, approvals, saga and memory |

## Test audit

Run on 3 Nov 2026 against the mock provider and gateway. Passing tests prove little unless they fail when the code is wrong, so I broke one requirement at a time in the source and ran the unit tests.

| Result | Count | Notes |
|---|---|---|
| Caught | 28 of 30 | Includes the 70/15/15 split, the 10-minute hold, the first cancel failing, hold expiry to the second, key replay, slot conflicts, ownership, caller rights, skipped approval, a broken approval check, budget, ambiguous timeouts, blind retries, review labels and masked trace arguments |
| Survived, real gap | 1 | With the gateway's client timeout removed, the hung-provider test still passed, because it never measured time. Fixed, and it now fails with the timeout removed |
| Survived, no gap | 1 | Raising the provider count formula still returned at most 8, because a slice caps the list. The requirement holds |

**Brief's tool contract against tests:**

| Line in the brief | Unit test | On real PostgreSQL or a real connection |
|---|---|---|
| `search_providers`: 3 to 8 with IDs, prices, ratings, slots, attributes | `test_search_returns_three_to_eight_providers`, `test_search_result_has_every_required_field` | Checked on the running container |
| `get_provider`: details and reviews, with at least one injection | `test_get_provider_returns_details_and_reviews`, `test_every_service_has_a_review_with_a_prompt_injection` | Live Gemini attack |
| `hold_slot`: 10 minutes, ID and expiry, configurable quote | `test_hold_lasts_exactly_ten_minutes_and_has_an_id`, `test_quote_difference_is_configurable` | `test_only_one_of_ten_concurrent_holds_gets_the_slot` |
| `book`: 70/15/15, half of timeouts booked | `test_book_outcomes_match_the_required_split`, one test per outcome | `test_a_real_client_timeout_can_still_leave_a_real_booking` |
| `book`: `hold_expired` past expiry | `test_book_at_the_expiry_moment_returns_hold_expired` | |
| `book`: no duplicates | `test_repeated_key_never_books_twice` | `test_ten_concurrent_books_with_one_key_make_one_booking` |
| `get_booking_status`: the truth after a timeout | `test_timeout_that_did_book_is_visible_only_through_status` | `test_timeout_after_success_end_to_end` |
| `cancel`: first 503, retry succeeds | `test_first_cancel_fails_with_503_and_the_retry_succeeds` | `test_cancel_503_is_retried_over_a_real_connection` |
| `reschedule`: `slot_conflict` on configured slots | `test_configured_slot_returns_slot_conflict` | |
| `list_bookings`: past and upcoming, with all fields | `test_list_bookings_shows_past_and_upcoming_with_all_fields` | Checked on the running container |
| Seeded or configurable, so tests repeat | `test_book_outcome_depends_only_on_seed_key_and_attempt`, `test_same_seed_gives_the_same_results` | |

**Not covered by any automated test:** Gemini's judgement. A scripted model cannot show that the real model avoids re-asking or resists an injection. I checked those by hand against live Gemini: 5 attacks, and it followed none. Recorded replies are still planned for this (N17).

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
- **Caller identity on the mock:** `hold_slot`, `book` and the others need to know whose booking it is, but their inputs are fixed by the brief. So identity travels in an `X-User-Id` header, like an auth header, and the inputs stay as specified.
- **Mock behaviour the brief leaves open:**
  - Retrying after a timeout that did not book draws a new outcome, because the attempt number is part of the hash. Otherwise the retry could never succeed.
  - A key the provider never saw reports `not_booked`, which is what crash recovery needs.
  - `reschedule` conflicts at 12:00 by default, or on the exact slots a scenario sets.
- **Simulated provider cancels stay behind `/admin`:** the demo must set `MOCK_EVAL_MODE=true` to trigger one.
- **Cancel retries live in the gateway:** the blueprint put them in the saga. The provider replays a repeated key, so the gateway can retry safely, and the saga stays simpler. `book` is never retried there.
- **Read tools are connected early:** the blueprint connects them with the workflow. They are safe, and they let you test search from the UI now.
- **Tool timeout:** 10 s per call, a value the blueprint did not set.
- **Today's date in the prompt:** the model cannot work out "this week" without it.

## Suggested order

1. **Mock provider:** done.
2. **Tool gateway:** done, apart from the real approval verifier, which comes with step 4.
3. **Workflow and search:** state machine, `update_request`, hard-limit filter for providers.
4. **Approvals and booking saga:** the core of the assignment.
5. **Cancel and reschedule:** on the same approval path.
6. **Memory and ContextAssembler:** includes forgetting and the token budget.
7. **Proactive cancellation, SSE and JSONL traces.**
8. **Eval harness, table and invariants.**
9. **Design note, trace excerpt, demo and transcript.**

## Log

### 3 Nov 2026: Tool gateway

- Added the `gateway` domain, the only path to any tool. Each call passes budget, allowlist, caller rights, schema, user binding and approval. Then it gets retries and a timeout, and the result is sanitized and traced.
- Connected the read tools to the model, so the agent can search, compare, read reviews and list bookings. `api` and `worker` both have the gateway. The worker does not use it yet.
- Side effects are refused for now: the approval check denies everything until approvals exist.
- The UI shows tool calls, retries and suspicious text in the trace, and the mock provider in the status bar.
- **Proof:** unit tests run the gateway against the real mock routes, and the real ADK loop with a scripted model. Integration tests run over real connections. Live Gemini searched through the gateway and followed none of 5 attacks. The test audit caught 28 of 30 deliberate breaks, and it found one real test gap, now fixed.
- **Found:** one search result added about 6,300 input tokens to a live turn. See N10.

### 3 Nov 2026: Mock provider

- Added the `mock-provider` service: all 8 contract tools, with its own package, database and migrations. `app` can only reach it over HTTP, and an import rule enforces that.
- Added 40 providers across 5 services. Every service has a provider with a prompt injection in a review. Added past and upcoming bookings for `demo-user`.
- **Proof:** 110 contract tests follow the brief line by line. 8 integration tests on PostgreSQL cover 10 concurrent holds, 10 concurrent books with one key, concurrent cancels, a real client timeout that still leaves a booking, and a server kill.
- **Found:** a worker recovery test failed on a second run because tests shared a job row. Fixed the isolation, and integration tests now pass twice in a row. See `BUGS.md`.

### 2 Nov 2026: React UI and this log

- Added `web`: React and Vite, with `chat`, `tracing` and `system` domains. nginx serves it on port 3000 and proxies `/api`.
- nginx resolves `api` at request time, so replacing the `api` container does not break the proxy. Checked by recreating the container with a new IP.
- **Proof:** 23 UI tests pass. Headless Chromium drove the real UI against live Gemini: chat, correction, trace switching, reload, new chat, test job. No browser errors and no sideways scroll at phone width.

### 2 Nov 2026: Server, agent, LLM and workers

- Added `api`, `worker`, `scheduler`, `migrate`, PostgreSQL and Redis in Compose. Commit `7fa5352`.
- **Proof:** 139 unit tests and 8 integration tests pass. The live smoke test passes. Checked Redis down, PostgreSQL down, API restart, a stuck model and no API key on the running stack.

### 2 Nov 2026: Design

- Blueprint versions 1 to 4. The stack moved from SQLite to PostgreSQL, Redis and Docker Compose. Spikes tested ADK, `taskiq` and Gemini behaviour.
