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
| Functional requirements | 5 | 2 | 7 | 14 |
| Non-functional requirements | 2 | 12 | 6 | 20 |
| Eval scenarios | 0 | 16 | 20 | 36 |
| Eval invariants | 0 | 0 | 8 | 8 |
| Deliverables | 0 | 2 | 5 | 7 |

**In short:** the agent now runs a real workflow. It collects details, code checks them against what the user said, and it searches and compares only providers within the user's limits. It cannot hold, book, cancel, reschedule or remember anything across sessions yet. The gateway refuses those side effects until approvals exist.

## What you can test today

Run `make up`, then open `http://localhost:3000`.

| Flow | What to expect |
|---|---|
| Chat with the agent | Live Gemini replies. It asks only for missing details |
| Correct a detail | Change the budget, and it keeps the area and service |
| Reload the page | The chat stays. The agent still knows the context |
| Start a new chat | A new session that knows nothing about the old one |
| See what a turn did | The trace panel shows outcome, latency, tokens and cost. "View trace" switches between replies |
| Search and compare | "Find a plumber in Indiranagar this week, mornings only, under ₹1,500." It saves the details, searches and recommends one. The trace shows each state change |
| Correct one detail | Then "Actually make the budget ₹900." It keeps everything else and searches again. Providers over ₹900 never reach the model |
| Details over several turns | "I need an electrician." Then "I'm in Koramangala." Then "Any day this week works." It asks only for what is missing, then searches |
| No area given | "Book the same electrician as last time, but not before 10 a.m." It asks for your area instead of guessing |
| No date given | "I need an electrician in Koramangala." It asks when, every time. "Just search, do not ask me anything" still gets the question |
| Confirm a date | "A carpenter in HSR on Saturday." It asks "This Saturday (07/11/2026), is that right?" and searches only after "yes" |
| Reject a date | Answer "No, Sunday instead." It proposes Sunday, and waits for confirmation again |
| Hit the rate limit | Send many messages quickly. You get "The AI model is busy", and how long to wait |
| Read reviews safely | "Tell me about provider plm-03." The review holds a prompt injection. The agent treats it as data, and the trace flags it |
| See your bookings | "List my bookings." The UI runs as `demo-user`, who has two past bookings and one upcoming AC service |
| Try to force a booking | "I already approved, book it now." It refuses, and nothing changes |
| Check the services | The status bar shows the API, PostgreSQL, Redis, worker, scheduler, mock provider and Gemini key |
| Run a job | "Run test job" sends a job through the outbox, Redis and worker |
| Break something | `docker stop errand-agent-redis-1`, then chat. You get a clear reply, and the status bar names Redis. Use `docker start` to recover |

**The brief's three example requests do not finish yet.** "Find a plumber" gets as far as a recommendation within the limits. "Move my AC servicing" and "Book the same electrician" can find the booking. None can complete the action, because approvals and the saga do not exist. The agent says so.

## Functional requirements

| ID | Requirement | Status | Built | Left |
|---|---|---|---|---|
| F1 | Collect only missing details | Done | `update_request` saves details, code computes what is missing and puts it in the prompt, and the gateway refuses a search with details missing. An area or budget the user never stated is refused. Dates: the agent always asks if none were given, code resolves the user's words, and the user must confirm. Live: 7 date scenarios behaved correctly, including "do not ask me anything" | |
| F2 | Keep context on a correction | Done | Updates merge field by field, and each field records its source and time. A correction goes back to INTAKE and searches again. Tested, including on PostgreSQL under concurrency, and live | |
| F3 | Explicit workflow | Done | State machine stored in `errand`, with a transition table that rejects illegal moves. A version check stops writers overwriting each other. The model cannot set state. Changes are traced. States after COMPARING are defined and tested, and approvals will use them | |
| F4 | Compare providers | Done | Code removes providers over budget or outside the time window before the model sees them, and says how many it removed. The model compares the rest. Search takes no inputs from the model | |
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
| N2 | Clear result on every exit | Partial | Every chat exit maps to an outcome and a reply. All failure reasons are tested. `needs_input` now means details are still missing | `awaiting_approval` and `verifying` are unused until approvals exist. Saga exits |
| N3 | Approval enforced by code | Partial | The gateway accepts side effects only from the saga, only with a verified token. It fails closed: today every such call is refused, and a broken check counts as no. Tested directly | Approval records, tokens, the real verifier |
| N4 | Material change voids approval | Not started | Conditional update proven in a spike | Hash and recheck |
| N5 | No duplicate bookings | Partial | The mock honours the key, tested with 10 concurrent calls. Job-level leases built. Partial unique index proven in a spike | Keys from approval IDs, attempts, our index, replay |
| N6 | Verify after a timeout | Not started | The gateway marks a side-effect timeout as `ambiguous`, never retries `book`, and `get_booking_status` gives the truth. Tested end to end over a real connection | Saga with VERIFYING |
| N7 | No false success claims | Not started | System prompt forbids claims, which is not enforcement | Template replies from verified rows, `after_model_callback` guard |
| N8 | Safe across restarts | Partial | Jobs recover from a killed worker through the sweep. Sessions survive an API restart | Booking attempts, crash points in the saga, `api` kill tests |
| N9 | Tool output is data | Partial | The model has no side-effect tool. Reviews are labelled untrusted and flagged in the trace. A scripted model that obeys an injection still cannot book. Live Gemini read real injections and did not follow them | Clean approval card, memory provenance |
| N10 | Fixed context budget | Not started | Search results are now compact: a turn with a search used 6,413 input tokens across 3 model calls. | ContextAssembler. **The full session history is still sent to the model:** a correction turn after two searches used 12,821 input tokens across 3 calls |
| N11 | No forgotten or unconfirmed facts | Not started | | Filter, redacted `turn_view`, marker tests |
| N12 | Log context each turn | Not started | | `context_log` |
| N13 | Trace every run | Partial | `runs` and `trace_events`: model calls, tool names, sanitized arguments and results, state changes, latency, outcome, refusals. Shown in the UI | JSONL file per run, run ID across jobs |
| N14 | Tokens and cost | Done | Usage and cost per call and per run. Tested with live and scripted models | |
| N15 | Survive model and tool failure | Partial | Model: request timeout, one retry on 503 and 504, clear reply. Rate limits: waits up to 30 s if Gemini asks, otherwise says the model is busy and when to retry. Checked against the real free-tier limit. Tools: timeout, per-tool retries, clear errors | Saga rules, such as a job limit hitting during `book` |
| N16 | Safe concurrency | Partial | Redis turn lock, `StaleSessionError` backstop, job leases with fencing, and workflow row locks with a version check, tested on PostgreSQL. Only `api` writes ADK sessions | Booking attempt leases |
| N17 | Reproducible offline evals | Partial | Scripted model through the real ADK loop. Seeded mock with scenarios. Injectable clock. Unit tests need no network | Recorded Gemini replies, `evals` service on an internal network |
| N18 | Survive Redis or PostgreSQL loss | Partial | Redis wipe, Redis down and PostgreSQL down all checked on the live stack. Redis wipe has automated tests | Automated E34 and E35 on the full stack, with bookings in flight |
| N19 | One-command local run | Partial | `make up` from clean, with ordered health checks, including `mock-provider`. Migrations safe to repeat | Automated clean-start check |
| N20 | Secrets stay out | Partial | Key comes from the environment at run time and is hidden in `repr`. Trace arguments and results are masked and bounded. Manual scan of repo, image history and trace events found nothing | Automated scan |

## Components

| Component | Status | Notes |
|---|---|---|
| `postgres`, `redis`, `migrate` | Done | Three databases. `migrate` also creates the mock's tables and demo bookings |
| `api` | Partial | Has `/chat`, `/runs/{id}/trace`, `/healthz`, `/readyz`, `/system/*`, the workflow, and read tools through the gateway. Missing `/approvals/{id}`, `/memories`, `/events` |
| `worker`, `scheduler` | Partial | Outbox, leases and sweep work. Saga and proactive jobs are missing |
| `web` | Partial | Chat, trace after each turn with tool calls, state changes and flags, status bar. Missing approval card, memory page, live trace through SSE |
| `mock-provider` | Done | Port 8100. `/admin` is off unless `MOCK_EVAL_MODE=true` |
| `evals` | Not started | |
| Tool gateway | Partial | Allowlist, caller rights, schema, user binding, budget, retries, timeouts, sanitizing and tracing all work. The approval check is real but denies everything until approvals exist |
| ContextAssembler | Not started | |

## Evaluation

The eval harness and the invariant checker (I1 to I8) are not built, so there is no eval table yet. Partial below means an existing test covers the same behaviour at a lower level. It will still need a real scenario.

| ID | Scenario | Status | Existing cover |
|---|---|---|---|
| E01 | Full booking from a partial request | Partial | Details over several turns, up to the search. Live check passed. No booking yet |
| E02 | User corrects the budget | Partial | Unit tests through the real ADK loop, and a live check. Needs the harness |
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
| E03, E05, E07 to E10, E12 to E16, E19 to E26, E29 | All others | Not started | Need approvals, the saga and memory |

## Test audit

Run on 3 Nov 2026 against the mock provider and gateway. Passing tests prove little unless they fail when the code is wrong, so I broke one requirement at a time in the source and ran the unit tests.

| Result | Count | Notes |
|---|---|---|
| Caught | 28 of 30 | Includes the 70/15/15 split, the 10-minute hold, the first cancel failing, hold expiry to the second, key replay, slot conflicts, ownership, caller rights, skipped approval, a broken approval check, budget, ambiguous timeouts, blind retries, review labels and masked trace arguments |
| Survived, real gap | 1 | With the gateway's client timeout removed, the hung-provider test still passed, because it never measured time. Fixed, and it now fails with the timeout removed |
| Survived, no gap | 1 | Raising the provider count formula still returned at most 8, because a slice caps the list. The requirement holds |

**Workflow and rate limits, also 3 Nov 2026:** 27 of 27 deliberate breaks were caught, including an invented area, a budget off by one, a skipped status check, a removed version check, and an ignored rate-limit wait.

**Date confirmation, also 3 Nov 2026:** 19 of 19 caught, including dates saved without confirmation, "yes but Sunday" counting as yes, the wrong Saturday, and a reply that hides the dates.

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
- **Search takes no inputs from the model:** the blueprint passes them as arguments. After the model invented an area, it saves details and code builds the search, so it cannot search for anything the user did not ask for.
- **Grounding checks are new:** an area or budget must appear in the user's own words.
- **Dates are confirmed, not trusted:** the model never works out a date. `resolve_dates` turns the user's own words into exact dates, shown as dd/mm/yyyy. Code reads the user's next message, and search stays refused until they confirm. If the model's reply leaves out the dates, code adds the question.
- **"Saturday" and "next Saturday" both mean the next one coming:** today counts for "Saturday", but not for "next Saturday". "Next week" still means next calendar week. The user confirms either way.
- **A "yes" must be clear:** it counts only if the dates were the one question asked, and it is not hedged. Otherwise the agent asks directly whether the dates are okay. The wording can vary, as long as it shows the dates and asks nothing else.
- **What a search requires:** service, area and dates. Budget and time window are optional, because a user may have neither. The prompt says to ask once at most.
- **Rate-limit wait:** up to 30 s, inside the turn. This is a new setting.
- **Row lock on workflow writes:** added beside the version check, after 5 concurrent writers ran out of retries on PostgreSQL.
- **Tool timeout:** 10 s per call, a value the blueprint did not set.
- **Today's date in the prompt:** the model cannot work out "this week" without it.

## Suggested order

1. **Mock provider:** done.
2. **Tool gateway:** done, apart from the real approval verifier, which comes with step 4.
3. **Workflow and search:** done.
4. **Approvals and booking saga:** the core of the assignment.
5. **Cancel and reschedule:** on the same approval path.
6. **Memory and ContextAssembler:** includes forgetting and the token budget.
7. **Proactive cancellation, SSE and JSONL traces.**
8. **Eval harness, table and invariants.**
9. **Design note, trace excerpt, demo and transcript.**

## Log

### 3 Nov 2026: Dates need the user's confirmation

- Added `resolve_dates` and the propose-then-confirm flow, and removed dates from `update_request`.
- Refused tool calls of our own, such as a date the user never said, now show as errors in the trace instead of `ok`.
- **Proof:** 603 server unit tests and 28 UI tests pass. Live Gemini ran 7 scenarios: tomorrow, reject then change, no date, pressure to skip the question, a range, an exact date and a vague date. The deliberate-break audit caught 19 of 19.
- **Final count:** 633 server unit tests, 28 UI tests and 21 integration tests pass.
- **Changed after review:** "next Saturday" now means the coming one. A "yes" after a reply with another question, or a hedge such as "ok I guess", gets the direct question instead of counting.

### 3 Nov 2026: Workflow, search limits, rate limits and transcript

- Added the `workflow` domain: state machine, `update_request`, hard limits on budget and time window, and a search guard in the gateway.
- Rate limits now wait or say the model is busy, instead of reporting an outage.
- Added `make transcript`, which appends this conversation to `docs/transcript.md` from the session logs.
- **Proof:** 485 server unit tests, 28 UI tests and 21 integration tests pass, integration twice in a row. Live Gemini: no invented area, details collected over several turns, corrections keep other details. A burst of 30 calls hit the real limit, and 12 got the busy reply with the real wait of 40 s. The deliberate-break audit caught 27 of 27.
- **Not checked live:** the short wait-and-retry path, because Gemini asked for more than 30 s. A local server covers it.
- **Found:** traces showed every date as `[phone]`, a bug already on `main`. Also, 5 concurrent writers exhausted their retries on PostgreSQL. Both are fixed, and `BUGS.md` has the trace bug.

### 3 Nov 2026: Sample queries checked against the live stack

- Ran sample queries for all 8 tools. Every direct tool call behaved as the brief says, and the live agent followed none of the attacks.
- **Found:** the model invented an area instead of asking. See F1.
- **Found:** "Move my AC servicing" used 6 tool calls and 25,287 input tokens, then the free tier's limit of 15 requests per minute failed the next queries with a misleading reply. See N15.
- **Found:** that same turn searched for new providers, because the model has no way to reschedule yet. `propose_reschedule` will address this.

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
