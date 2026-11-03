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

## 11. The false-claim guard let any claim through once a booking was read or verified

- **What broke:** The guard allowed any "it has happened" text if the model had read the bookings in that turn, or if the chat had any verified booking. Cancelling always reads the bookings first, so "Your booking has been cancelled" would have passed while the card was still waiting for a yes. Found while designing cancel, before it was built, by reading the guard.
- **Why:** The guard asked "did something verified happen here?", not "did this thing happen?". That was good enough with only one action.
- **Fix:** It now reads which action a claim names. A claim about anything still waiting for a yes or a check is blocked, even after a list read. A claim is fair only if that same action was verified or the list was read.
- **Regression tests:** `test_the_model_cannot_say_it_was_cancelled_after_only_reading_the_list` and `test_a_verified_booking_does_not_make_a_cancel_claim_fair` in `server/tests/unit/test_booking_chat.py`.

## 12. "Wednesday next week" gave this Wednesday, at a time nobody chose

- **What broke:** The user said "Move my AC servicing to next week", then "around wednesday". The card offered Wed 04/11/2026 at 10:00, which is this week, at a time they never gave. "Wednesday next week" and "11th I guess?" then got only "please give the exact date". Found by the user in the UI.
- **Why:** Four faults, seen in the trace. The model sent only "wednesday", losing "next week" from the earlier message. The date reader did not know "Wednesday next week" or a bare "11th". The model rewrote "11th" as "11 Nov", which was refused as words the user never said. And it sent `time: 10:00` all three times, because only the prompt said not to pick a time.
- **Fix:** The reader now knows a weekday in a named week and a bare ordinal. Code refuses a time the user never said, with "the same time" as the one exception. The prompt says to join the user's words across messages. That last part is guidance only, and the card still shows the exact date.
- **Why tests missed it:** They all passed a clean day and time, the way the code expected. The user answered over several messages, in loose words.
- **Regression tests:** `test_a_weekday_next_week_is_in_next_calendar_week`, `test_a_bare_ordinal_is_the_next_one_to_come_round` and `test_a_time_the_user_never_gave_does_not` in `server/tests/unit/test_dates.py`, and `test_the_model_cannot_pick_a_time` in `server/tests/unit/test_booking_changes.py`.

## 13. "Next week sometime", then "thur", could never become a date

- **What broke:** The user said "next week sometime", then "lets do thur", then "cant you find out the date?". The agent kept asking for the exact date, and no search ran.
- **Why:** Three faults, seen in the trace. The reader did not know "sometime", so the first answer was `date_not_understood`. The model then correctly sent "Thursday next week", but the check compared whole words, and the user had written "thur", so it was `date_not_stated`. Both refusals told the model to ask the user for a date, so it did. A later replay found a fourth: with the first answer fixed, the model sent just "thur", which read as this Thursday.
- **Fix:** The check treats "thur" and "Thursday", "nov" and "November", "14th" and "14", and "tmrw" and "tomorrow" as the same word. The reader ignores "sometime", "anytime" and "whenever" beside a real date. A bare weekday inside the saved dates means that day of the saved range. The refusals now say never to ask for a calendar date. "Tuesday or Thursday next week" also used to start from this Tuesday; the week now applies to both days.
- **Why tests missed it:** Grounding tests used full day names. This is the same kind of miss as bug 12: tests passed clean words, and users shorten them.
- **Regression tests:** `test_the_days_a_user_shortens_count_as_their_own_words`, `test_sometime_next_week_is_the_whole_week` and `test_the_week_belongs_to_both_days_in_an_or` in `server/tests/unit/test_dates.py`, and `test_a_bare_day_after_next_week_means_that_weeks_day` in `server/tests/unit/test_workflow.py`.

## 14. The agent named times a provider never had

- **What broke:** The user picked Quick AC Care, which has no Tuesday slots. The agent offered Tuesday at 10:00, 11:00 and 12:00. Code refused three proposals, and each time the agent listed more invented Tuesday times. It also said "you work on Tuesdays", against the saved Mon, Wed, Fri. Nothing wrong could be booked, and no card appeared.
- **Why:** Tool results were correct. The search showed only the first 6 of 34 slots, later turns the first 8, and no tool could read the rest, so a question about Tuesday had no answer. The made-up times match the other providers' Tuesday slots, listed beside Quick's (inferred from the logs). The refusal named no open slots. The prompt said "list a few open slots", with no rule against times it had not seen.
- **Fix:** The user's idea: the model asks for what it needs. A new local tool, `get_open_slots(provider_id, day)`, returns one provider's times on one day. Search results now name the open days and no times. A refused slot returns the real times that day and the open days. The prompt forbids naming a time not returned in the same turn.
- **Why tests missed it:** Scripted models only say what they are told, so nothing tested a model answering from a cut list. It took a person asking about a day past the cut.
- **Regression tests:** `test_a_providers_times_on_one_day`, `test_each_provider_has_its_own_times`, `test_a_refused_slot_says_what_is_really_open` and `test_result_names_the_open_days_and_no_times` in `server/tests/unit/test_workflow.py`. The rule against naming times rests on the prompt, checked live only.

## 15. "Thanks" after a verified booking got "please confirm the card"

- **What broke:** Found in a live run of this step. The user clicked Confirm, the provider verified the booking, and the user said "thanks". The reply was "Please confirm the booking using the card on screen".
- **Why:** Workers cannot write to the chat, so the model's history ended with its own "please confirm". The verified news was only one line in the booking status, and the model followed the history.
- **Fix:** The system's news now goes into the chat history the model reads, in time order. It is what the user saw on screen too.
- **Why tests missed it:** Scripted models say what they are told, so a test cannot show a real model trusting its history over the status line.
- **Regression test:** `test_the_systems_news_sits_in_the_chat_in_time_order` in `server/tests/unit/test_proactive.py`. That the model now answers correctly was checked live, once.

## 16. A new state move broke a safety rule, and the docs still claimed it

- **What broke:** The proactive work made `INTAKE` to `DONE` legal, so a blank errand can give way. Two state machine tests failed, and the N6 row in the build log still said no other path reaches `DONE`. Only GitHub CI caught it, because that change ran only the test files it touched.
- **Why:** The rule was written as "only VERIFYING reaches DONE". What it must protect is narrower: nothing that executed can reach `DONE` without a status check.
- **Fix:** The move stays, because the unique index on active errands needs it. `reopen_in` allows it only for a blank errand. The test now walks every path out of `EXECUTING`, and the N6 row says what is true.
- **Regression test:** `test_nothing_that_executed_reaches_done_without_verifying` in `server/tests/unit/test_workflow.py`.

## 17. A cancelled tool call left a gap in the trace

- **What broke:** Found by invariant I6 in E28. The model made 15 tool calls at once, against a limit of 12. The trace jumped from sequence 4 to 6, and one refusal was gone.
- **Why:** When one parallel call spends the budget, ADK cancels the others. A call cancelled in the middle of writing its trace event lost the write, but its sequence number was already used. Real Gemini can make parallel calls, so this could happen outside a test.
- **Fix:** The write now runs under `asyncio.shield`, so it finishes even if its caller is cancelled.
- **Why tests missed it:** Unit tests of the budget made one call per step, because `ScriptedLlm` could only make one. It can now make several at once.
- **Regression tests:** `test_a_cancelled_write_leaves_no_gap_in_the_trace` in `server/tests/unit/test_tracing.py`, and E28 with I6.

## 18. A killed API left its run open for good

- **What broke:** Found by E37. The `api` process was killed in the middle of a turn. The next message worked, but the dead run never got an outcome or a reply. So the trace panel would show it as running forever, and I6 failed.
- **Why:** Only the process running a turn could finish it. The sweep recovered jobs and booking attempts, but nothing looked at runs.
- **Fix:** The sweep now closes any run older than the turn lock's expiry, because a live turn cannot outlast its lock. It ends as `tool_failed`, with the reason `process_died` and a clear reply.
- **Why tests missed it:** Earlier kill tests killed only workers, whose work has a lease. No test killed the `api`, although the blueprint's N8 asked for it.
- **Regression tests:** `test_a_run_left_open_by_a_dead_process_gets_an_outcome` in `server/tests/unit/test_tracing.py`, and E37 with I6.

## Smaller

**The eval job failed in CI after every scenario had passed.** All 38 scenarios passed on GitHub's runner, and then writing the table failed with `PermissionError`. Once `evals/results.*` were committed, a fresh checkout owned them as the runner's user, and the container runs as another user. Only the folder was made writable, not the files. This never showed on a Mac, where Docker maps file ownership. `make evals` now makes the files writable too. Regression test: the Evals job in CI.

**The false-claim guard's own reply read as a claim.** I3 flagged it in E05. "I only say something is booked" matches the pattern for a claim, and the words "once it is verified" come after it, so they did not count. Nothing reached a user wrongly, but the reply was the one thing that should never look like a claim. It now says "I only report a booking or a change". Regression test: E05 with I3.

**The event stream said "no news" as soon as it opened.** Redis answers a subscribe with a message, and the client reports that as nothing. So the first wait ended at once. Users would have seen no difference, because it only sent a keepalive early, but the listener's answer was wrong. Regression test: `test_a_wake_up_delivers_at_once_over_real_redis` in `server/tests/integration/test_proactive.py`.


**A stated fact was marked as a guess, and the other fact in the message was lost.** From testing the UI: "I live in Indiranagar and go to office on Mon, Wed and Fri 9-5", then "remember that". The model correctly wrote "goes to office", in the third person. The quote check wanted every word of the value in the user's sentence, so "goes" against "go" made it unconfirmed. The model also made only one call, so the area was never proposed. The check now ignores word endings and filler words, and the prompt asks for one call per fact. Regression tests: `test_a_words_ending_does_not_make_a_stated_fact_a_guess` and `test_two_facts_in_one_message_are_both_saved_and_shown_once` in `server/tests/unit/test_memory.py`. The one-call part rests on the prompt, checked live.

**Saying no to a guess would have forgotten the real fact.** Forget on the panel worked by key. So forgetting an unconfirmed guess also forgot the trusted value under that key, and scrubbed it from the chat. I found this while adding the "Remember this?" card. It now drops only the guess. Regression test: `test_saying_no_to_a_guess_keeps_the_trusted_value`.

**A typed yes or no never reached the model's history.** `decide_from_message` answers before the model runs, so neither the message nor the reply went into the ADK session. On a later turn the model could not see what the user had said. The new `turn_view` records every message and reply that ChatService handles. Regression test: `test_a_typed_answer_on_a_card_is_in_the_next_history` in `server/tests/unit/test_booking_chat.py`.

**Retry tests needed a real API key.** `test_model_retry.py` passed `test-key` to `Settings`, but the Gemini client reads `GOOGLE_API_KEY` from the environment. The tests passed here only because this machine has a real key, and that key went to the local stub server. A dry run for CI found it. The tests now set a fake key in the environment. Separately, `Settings.google_api_key` only says whether a key is set: it is not what reaches the client, which matters only if the app runs outside Compose with the key only in `.env`.

**The agent asked again after the user had answered.** Live Gemini re-sent the date string to `resolve_dates`. It was refused as words the user never said, so the agent asked a second time. `resolve_dates` now recognises dates already saved, and says to search. `test_repeating_saved_dates_says_to_search_instead_of_refusing` guards it.

**A timeout test passed with the timeout removed.** The test said a hung provider gets cut off, but it never measured time. With the gateway's client timeout gone, it waited the full 5 s, got the same error, and still passed. A deliberate-break audit found this. `test_a_hung_provider_is_cut_off_at_the_timeout` now asserts the elapsed time.

**A recovery test passed once and failed on the next run.** The sweep test asserted that it republished exactly one job. The concurrent-claim test leaves a job with a 60 s lease in the test database. The next run came more than 60 s later, the lease had expired, and the sweep correctly picked that job up too. The sweep was right, and the test was not isolated. The `jobs` fixture in `test_worker_recovery.py` now empties the outbox first.

**Autogenerated migration could not run.** Alembic rendered our custom column type as `app.core.database.UtcDateTime` without importing it, so the upgrade would have crashed. I caught this on review, before running it. `alembic/env.py` now renders it as `sa.DateTime(timezone=True)`. `test_models_and_migrations_agree` runs the upgrade and checks models against the migrations.
