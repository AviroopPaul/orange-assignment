# Real bugs hit while building

Candidates for the "three real bugs" deliverable. Each one really happened, and each has a test that guards it.

## 1. A killed worker's job never came back on a quiet queue

- **What broke:** After `kill -9` on a worker mid-job, the job stayed undelivered. With a 5 s idle timeout it had still not returned after 40 s.
- **Why:** In `taskiq-redis`, the reclaim step comes after `if not fetched: continue`. So it only runs when a new job arrives. On a quiet queue, it never runs.
- **Fix:** Recovery no longer depends on the broker. Jobs have leases in PostgreSQL, and a sweep every 15 s requeues expired ones.
- **Regression test:** `test_killed_worker_on_a_quiet_queue_recovers_through_the_sweep` in `server/tests/integration/test_worker_recovery.py`.

## 2. A background write killed the user's running turn

- **What broke:** A background write to an ADK session during a tool call succeeded, and the user's run then died with `StaleSessionError` and no reply. Two turns at once on one session failed the same way.
- **Why:** ADK checks a revision marker on every write. Whoever holds the older copy of the session loses.
- **Fix:** Workers never write to ADK sessions. Only `api` writes, under a per-session Redis lock. `StaleSessionError` maps to a clear reply as the backstop.
- **Regression tests:** `test_chat_on_real_services_keeps_history_and_serialises_turns` in `server/tests/integration/test_postgres.py`, and `test_stale_session_write_is_translated` in `server/tests/unit/test_agent_runtime.py`.

## 3. A short request timeout failed every chat turn

- **What broke:** A 5 s model request timeout made every call fail at once, with `400 INVALID_ARGUMENT`.
- **Why:** Gemini rejects any request deadline under 10 s. The failure looks like a model outage, not a config mistake.
- **Fix:** Settings validation refuses a timeout under 10 s at startup.
- **Regression test:** `test_request_timeout_respects_geminis_minimum` in `server/tests/unit/test_config.py`.

## 4. Traces showed every date as a phone number

- **What broke:** Every date and slot in a trace read `[phone]`, so a trace could not show which day a search covered.
- **Why:** The privacy mask matched any run of digits and dashes. A date such as `2026-11-05` has the same shape as a phone number. Earlier trace tests only checked provider IDs, so nothing noticed, and it reached `main`.
- **Fix:** The mask now needs at least 10 digits. A date has 8.
- **Regression tests:** `test_trace_keeps_dates_slots_and_numbers` and `test_trace_masks_phone_numbers` in `server/tests/unit/test_gateway.py`.

## 5. The agent invented the user's area

- **What broke:** Asked to "book the same electrician", with no area given, live Gemini searched "Jayanagar". The prompt told it never to guess, and it guessed anyway.
- **Why:** The model chose the search inputs, and nothing checked them against what the user said. A prompt cannot enforce this.
- **Fix:** Search now takes no inputs. The model saves details with `update_request`, code checks that the area and budget appear in the user's own words, and the search uses only the saved details.
- **Regression test:** `test_invented_area_is_refused_and_no_search_runs` in `server/tests/unit/test_agent_tools.py`.

## 6. Rate limits were reported as a model outage

- **What broke:** After a heavy turn used up the free tier's 15 requests per minute, users were told "The AI model is not responding", and the SDK spent another request on an immediate retry that could not succeed.
- **Why:** A 429 was treated like any other model error, and Gemini's stated wait was ignored.
- **Fix:** A 429 now waits as long as Gemini asks and tries once more, if that wait is 30 s or less. A longer wait replies straight away that the model is busy, and says when to try again.
- **Regression tests:** `test_rate_limit_waits_as_long_as_gemini_asks_then_tries_once_more` and `test_long_rate_limit_is_not_waited_for` in `server/tests/unit/test_model_retry.py`, and `test_rate_limit_says_busy_and_when_to_retry` in `server/tests/unit/test_chat_service.py`.

## 7. A double-click said the approval was no longer valid

- **What broke:** Ten clicks on Confirm at once started one booking, which was correct. But the other nine replied "that confirmation is no longer valid, so nothing was booked", while the booking was running. A user would have believed the opposite of the truth.
- **Why:** Each click read the approval, then waited for the workflow's row lock. By the time it got the lock, the first click had consumed the approval and moved the workflow on. The click still held its old copy of the approval, so it took the moved workflow to mean the request had changed.
- **Fix:** Read the approval again once the lock is held. The same fix went into the code that closes a stale card, which could also have recorded a false `approval_voided` event.
- **Why tests missed it at first:** SQLite in the unit tests has one shared connection, so nothing waits on a lock. Only PostgreSQL showed it.
- **Regression test:** `test_ten_concurrent_confirmations_start_one_attempt` in `server/tests/integration/test_booking_saga.py`.

## 8. A refused booking counted as a call to the provider

- **What broke:** When the gateway refused a booking because the price had moved, the attempt row said `book_calls = 1`, although nothing was ever sent. A later check of "was the provider called?" would have got the wrong answer.
- **Why:** The count is committed before the call, on purpose, so a crash leaves a record that a call may have happened. A refusal happens inside the gateway, after that commit.
- **Fix:** After a refusal, the saga puts the count back.
- **Why tests missed it at first:** The unit test counted calls at the transport, which was correct, and never looked at the row. The integration test asserted the row and found it.
- **Regression tests:** `test_price_change_after_approval_on_real_services` in `server/tests/integration/test_booking_saga.py`, and `test_price_change_after_approval_books_nothing_and_shows_the_new_price` in `server/tests/unit/test_bookings.py`.

## 9. A yes that answered two questions failed, and the app said nothing changed

- **What broke:** The agent asked "Are these dates okay? Also, what area are you in?" The user replied "yes, and im in indiranagar". They got "I didn't get a usable reply. Nothing was changed", and the agent then asked about the dates again. Found by the user in the UI.
- **Why:** Three faults in a row. The model asked two questions, although the prompt says not to. The date rule treated any yes after two questions as unclear, even one that answered both. Then the model saved the area and wrote no text, so the turn failed with a reply that was false, because the area had been saved.
- **Fix:** A yes that adds a detail now counts as clear, and a plain "yes" after two questions still gets the direct question. "Yes, Sunday" is no longer read as a yes to Saturday. When the model writes nothing, code asks what comes next from the saved state. Failure replies now say only that this message made or changed no booking, and they never deny a booking that is running.
- **Later change:** the user then asked for clear dates to be saved with no question, so the date question is gone and this kind of yes can no longer fail. The empty-reply and failure-reply fixes stay.
- **Regression test:** `test_empty_reply_after_saving_asks_what_comes_next` in `server/tests/unit/test_booking_chat.py`.

## 10. A card could be lost for good

- **What broke:** The user saw "Awaiting approval" and "check the card on screen", with no card. The approval was open on the server, and a reload did not bring it back. Found by the user in the UI.
- **Why:** The tab was running the build from before the card existed: the web logs show no asset requests from that browser until after the turn. That old code dropped the card from the reply, and the card lived only in the reply and the browser's saved chat.
- **Fix:** On load, the page asks `/bookings/current` what is open, and shows the card or follows the running booking. A card the server has closed also closes on the page. `index.html` is now served with `no-cache`, so a reload picks up a new build.
- **Regression tests:** `test_http_current_booking_gives_a_page_the_open_card_again` in `server/tests/unit/test_booking_chat.py`, and the "after a reload" tests in `web/src/App.test.tsx`.

## Smaller

**The agent asked again after the user had answered.** Live Gemini re-sent the date string to `resolve_dates`. It was refused as words the user never said, so the agent asked a second time. `resolve_dates` now recognises dates already saved, and says to search. `test_repeating_saved_dates_says_to_search_instead_of_refusing` guards it.

**A timeout test passed with the timeout removed.** The test said a hung provider gets cut off, but it never measured time. With the gateway's client timeout gone, it waited the full 5 s, got the same error, and still passed. A deliberate-break audit found this. `test_a_hung_provider_is_cut_off_at_the_timeout` now asserts the elapsed time.

**A recovery test passed once and failed on the next run.** The sweep test asserted that it republished exactly one job. The concurrent-claim test leaves a job with a 60 s lease in the test database. The next run came more than 60 s later, the lease had expired, and the sweep correctly picked that job up too. The sweep was right, and the test was not isolated. The `jobs` fixture in `test_worker_recovery.py` now empties the outbox first.

**Autogenerated migration could not run.** Alembic rendered our custom column type as `app.core.database.UtcDateTime` without importing it, so the upgrade would have crashed. I caught this on review, before running it. `alembic/env.py` now renders it as `sa.DateTime(timezone=True)`. `test_models_and_migrations_agree` runs the upgrade and checks models against the migrations.
