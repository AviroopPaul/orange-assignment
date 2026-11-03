import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import { App } from "./App";
import {
  fakeApi,
  FakeEventSource,
  HEALTHY_STATUS,
  pushNotification,
  traceFor,
} from "./test/fakeApi";

const STATUS_OK = {
  "GET /api/system/status": () => ({ json: HEALTHY_STATUS }),
};

function reply(runId: string, text: string, extra: Record<string, unknown> = {}) {
  return () => ({
    json: {
      run_id: runId,
      session_id: "session-1",
      outcome: "done",
      reason: null,
      reply: text,
      ...extra,
    },
  });
}

const chatRegion = () => screen.getByRole("region", { name: "Chat" });

function openPanel(name: "Trace" | "Memory") {
  return userEvent.click(screen.getByRole("button", { name }));
}

async function openServices() {
  await userEvent.click(await screen.findByRole("button", { name: /services|down|unreachable/i }));
  return screen.findByRole("list", { name: "Service status" });
}

async function say(text: string) {
  const input = screen.getByLabelText("Your message");
  await userEvent.type(input, `${text}{Enter}`);
}

describe("chat", () => {
  it("sends a message, shows the reply and shows that run's trace", async () => {
    const calls = fakeApi({
      ...STATUS_OK,
      "POST /api/chat": reply("run-1", "Which area are you in?"),
      "GET /api/runs/run-1/trace": () => ({ json: traceFor("run-1") }),
    });
    render(<App />);

    await say("I need a plumber");

    expect(await screen.findByText("Which area are you in?")).toBeInTheDocument();
    expect(within(chatRegion()).getByText("I need a plumber")).toBeInTheDocument();
    const posted = calls.find((call) => call.key === "POST /api/chat");
    expect(posted?.body).toEqual({
      user_id: "demo-user",
      message: "I need a plumber",
    });

    await openPanel("Trace");
    const trace = screen.getByRole("region", { name: "Run trace" });
    expect(await within(trace).findByText("Model call")).toBeInTheDocument();
    expect(within(trace).getByText("gemini-3.5-flash-lite")).toBeInTheDocument();
    expect(within(trace).getByText("1.5 s")).toBeInTheDocument();
    expect(within(trace).getByText("842 ms")).toBeInTheDocument();
    expect(within(trace).getAllByText("263 in, 10 out")).toHaveLength(2);
    expect(within(trace).getAllByText("$0.000104")).toHaveLength(2);
  });

  it("continues the same session on the next message", async () => {
    const calls = fakeApi({
      ...STATUS_OK,
      "POST /api/chat": reply("run-1", "Noted."),
      "GET /api/runs/run-1/trace": () => ({ json: traceFor("run-1") }),
    });
    render(<App />);

    await say("first");
    await screen.findByText("Noted.");
    await say("second");

    await waitFor(() => {
      const chats = calls.filter((call) => call.key === "POST /api/chat");
      expect(chats).toHaveLength(2);
      expect(chats[1].body).toEqual({
        user_id: "demo-user",
        session_id: "session-1",
        message: "second",
      });
    });
  });

  it("does not send an empty message", async () => {
    const calls = fakeApi(STATUS_OK);
    render(<App />);

    await say("   ");

    expect(calls.some((call) => call.key === "POST /api/chat")).toBe(false);
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
  });

  it("makes a new line with Shift+Enter instead of sending", async () => {
    const calls = fakeApi(STATUS_OK);
    render(<App />);

    const input = screen.getByLabelText("Your message");
    await userEvent.type(input, "line one{Shift>}{Enter}{/Shift}line two");

    expect(input).toHaveValue("line one\nline two");
    expect(calls.some((call) => call.key === "POST /api/chat")).toBe(false);
  });

  it("blocks a second send while one is in flight", async () => {
    let finish: (value: unknown) => void = () => {};
    const pending = new Promise((resolve) => (finish = resolve));
    const calls = fakeApi({
      ...STATUS_OK,
      "POST /api/chat": (async () => {
        await pending;
        return reply("run-1", "Done.")();
      }) as never,
      "GET /api/runs/run-1/trace": () => ({ json: traceFor("run-1") }),
    });
    render(<App />);

    await say("first");
    expect(await screen.findByRole("status")).toHaveTextContent("Thinking");
    await say("second");

    expect(calls.filter((call) => call.key === "POST /api/chat")).toHaveLength(1);
    expect(screen.getByLabelText("Your message")).toHaveValue("second");

    finish(null);
    expect(await screen.findByText("Done.")).toBeInTheDocument();
  });

  it("marks a turn that did not finish normally", async () => {
    fakeApi({
      ...STATUS_OK,
      "POST /api/chat": reply("run-1", "The AI model is not responding right now.", {
        outcome: "tool_failed",
        reason: "model_unavailable",
      }),
      "GET /api/runs/run-1/trace": () => ({
        json: traceFor("run-1", {
          outcome: "tool_failed",
          reason: "model_unavailable",
        }),
      }),
    });
    render(<App />);

    await say("hello");

    expect(await screen.findByText("Failed")).toBeInTheDocument();
    expect(screen.getByText("The AI model is not responding right now.")).toBeInTheDocument();
  });

  it("does not flag a turn that simply asks the user a question", async () => {
    fakeApi({
      ...STATUS_OK,
      "POST /api/chat": reply("run-1", "Which area are you in?", {
        outcome: "needs_input",
      }),
      "GET /api/runs/run-1/trace": () => ({ json: traceFor("run-1") }),
    });
    render(<App />);

    await say("I need a plumber");

    expect(await screen.findByText("Which area are you in?")).toBeInTheDocument();
    expect(screen.queryByText("Needs input")).not.toBeInTheDocument();
  });

  it("shows when the model is busy, and when to try again", async () => {
    fakeApi({
      ...STATUS_OK,
      "POST /api/chat": reply(
        "run-1",
        "The AI model is busy because we hit its usage limit. Please try again in about 46 seconds.",
        {
          outcome: "limit_reached",
          reason: "model_rate_limited",
          retry_after_s: 46,
        },
      ),
      "GET /api/runs/run-1/trace": () => ({
        json: traceFor("run-1", {
          events: [
            {
              seq: 1,
              kind: "model_rate_limited",
              name: null,
              data: { retry_after_s: 46 },
              latency_ms: null,
              input_tokens: null,
              output_tokens: null,
              estimated_cost_usd: null,
              created_at: "2026-11-02T12:00:01+00:00",
            },
          ],
        }),
      }),
    });
    render(<App />);

    await say("hello");

    expect(await screen.findByText(/in about 46 seconds/)).toBeInTheDocument();
    expect(within(chatRegion()).getByText("Limit reached")).toBeInTheDocument();
    await openPanel("Trace");
    expect(await screen.findByText("Rate limited")).toBeInTheDocument();
    expect(screen.getByText("46 s")).toBeInTheDocument();
  });

  it("shows the server's message when the database is down and keeps the draft", async () => {
    fakeApi({
      ...STATUS_OK,
      "POST /api/chat": () => ({
        status: 503,
        json: {
          error: "database_unavailable",
          message: "The database is not reachable. Nothing was changed. Please try again.",
        },
      }),
    });
    render(<App />);

    await say("hello");

    expect(await screen.findByText(/database is not reachable/)).toBeInTheDocument();
    expect(screen.getByLabelText("Your message")).toHaveValue("hello");
  });

  it("explains when the server cannot be reached", async () => {
    fakeApi({
      ...STATUS_OK,
      "POST /api/chat": () => Promise.reject(new TypeError("network down")),
    });
    render(<App />);

    await say("hello");

    expect(await screen.findByText(/Could not reach the server/)).toBeInTheDocument();
  });

  it("switches the trace when another reply is selected", async () => {
    let count = 0;
    fakeApi({
      ...STATUS_OK,
      "POST /api/chat": () => {
        count += 1;
        return reply(`run-${count}`, `Reply ${count}`)();
      },
      "GET /api/runs/run-1/trace": () => ({ json: traceFor("run-1") }),
      "GET /api/runs/run-2/trace": () => ({ json: traceFor("run-2") }),
    });
    render(<App />);

    await say("first");
    await screen.findByText("Reply 1");
    await say("second");
    await screen.findByText("Reply 2");
    await openPanel("Trace");
    expect(await screen.findByText("Run run-2")).toBeInTheDocument();

    await userEvent.click(screen.getAllByRole("button", { name: "View trace" })[0]);

    expect(await screen.findByText("Run run-1")).toBeInTheDocument();
  });

  it("restores the chat after a reload and starts fresh on New chat", async () => {
    const calls = fakeApi({
      ...STATUS_OK,
      "POST /api/chat": reply("run-1", "Noted."),
      "GET /api/runs/run-1/trace": () => ({ json: traceFor("run-1") }),
    });
    const first = render(<App />);
    await say("remember me");
    await screen.findByText("Noted.");
    first.unmount();

    render(<App />);
    expect(within(chatRegion()).getByText("remember me")).toBeInTheDocument();
    expect(screen.getByText("Session session-")).toBeInTheDocument();
    await openPanel("Trace");
    expect(await screen.findByText("Run run-1")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "New chat" }));
    expect(within(chatRegion()).queryByText("remember me")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "New chat" })).toBeInTheDocument();
    expect(screen.queryByText("Session session-")).not.toBeInTheDocument();

    await say("fresh");
    await waitFor(() => {
      const last = calls.filter((call) => call.key === "POST /api/chat").at(-1);
      expect(last?.body).toEqual({ user_id: "demo-user", message: "fresh" });
    });
  });

  it("explains memory on the empty chat", () => {
    fakeApi(STATUS_OK);
    render(<App />);
    expect(screen.getByText(/remembers lasting preferences/)).toBeInTheDocument();
  });

  it("fills the box from an example without sending it", async () => {
    const calls = fakeApi(STATUS_OK);
    render(<App />);

    await userEvent.click(screen.getByRole("button", { name: /Move my AC servicing/ }));

    expect(screen.getByLabelText("Your message")).toHaveValue("Move my AC servicing to Saturday.");
    expect(calls.some((call) => call.key === "POST /api/chat")).toBe(false);
  });
});

describe("trace", () => {
  it("shows an invented tool as refused", async () => {
    fakeApi({
      ...STATUS_OK,
      "POST /api/chat": reply("run-1", "I can't book that."),
      "GET /api/runs/run-1/trace": () => ({
        json: traceFor("run-1", {
          events: [
            {
              seq: 1,
              kind: "tool_refused",
              name: "book",
              data: {
                caller: "model",
                reason: "not_allowed",
                args: { hold_id: "h1" },
              },
              latency_ms: null,
              input_tokens: null,
              output_tokens: null,
              estimated_cost_usd: null,
              created_at: "2026-11-02T12:00:01+00:00",
            },
          ],
        }),
      }),
    });
    render(<App />);
    await openPanel("Trace");

    await say("book it");

    expect(await screen.findByText("Tool refused")).toBeInTheDocument();
    expect(screen.getByText("book")).toBeInTheDocument();
    expect(screen.getByText("not_allowed")).toBeInTheDocument();
    expect(screen.getByText('{"hold_id":"h1"}')).toBeInTheDocument();
  });

  it("shows a tool call, its retries and any suspicious text", async () => {
    fakeApi({
      ...STATUS_OK,
      "POST /api/chat": reply("run-1", "Here is what I found."),
      "GET /api/runs/run-1/trace": () => ({
        json: traceFor("run-1", {
          events: [
            {
              seq: 1,
              kind: "tool_call",
              name: "get_provider",
              data: {
                caller: "model",
                status: "ok",
                error_code: null,
                attempts: 3,
                injection_flags: ["ignore_instructions", "skip_approval"],
                args: { provider_id: "plm-03" },
              },
              latency_ms: 120,
              input_tokens: null,
              output_tokens: null,
              estimated_cost_usd: null,
              created_at: "2026-11-02T12:00:01+00:00",
            },
          ],
        }),
      }),
    });
    render(<App />);
    await openPanel("Trace");

    await say("tell me about them");

    expect(await screen.findByText("Tool call")).toBeInTheDocument();
    expect(screen.getByText("get_provider")).toBeInTheDocument();
    expect(screen.getByText("ignore_instructions, skip_approval")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
  });

  it("shows state changes", async () => {
    fakeApi({
      ...STATUS_OK,
      "POST /api/chat": reply("run-1", "Searching."),
      "GET /api/runs/run-1/trace": () => ({
        json: traceFor("run-1", {
          events: [
            {
              seq: 1,
              kind: "state_transition",
              name: "searching",
              data: { from: "intake", to: "searching" },
              latency_ms: null,
              input_tokens: null,
              output_tokens: null,
              estimated_cost_usd: null,
              created_at: "2026-11-02T12:00:01+00:00",
            },
          ],
        }),
      }),
    });
    render(<App />);
    await openPanel("Trace");

    await say("go");

    expect(await screen.findByText("State change")).toBeInTheDocument();
    expect(screen.getByText("intake to searching")).toBeInTheDocument();
  });

  it("warns when a tool's outcome is unknown", async () => {
    fakeApi({
      ...STATUS_OK,
      "POST /api/chat": reply("run-1", "Checking."),
      "GET /api/runs/run-1/trace": () => ({
        json: traceFor("run-1", {
          events: [
            {
              seq: 1,
              kind: "tool_call",
              name: "book",
              data: {
                caller: "saga",
                status: "timeout",
                error_code: "timeout",
                ambiguous: true,
              },
              latency_ms: 10000,
              input_tokens: null,
              output_tokens: null,
              estimated_cost_usd: null,
              created_at: "2026-11-02T12:00:01+00:00",
            },
          ],
        }),
      }),
    });
    render(<App />);
    await openPanel("Trace");

    await say("hello");

    expect(await screen.findByText(/check the source of truth/)).toBeInTheDocument();
  });

  it("explains when a trace cannot be loaded", async () => {
    fakeApi({
      ...STATUS_OK,
      "POST /api/chat": reply("run-1", "Noted."),
      "GET /api/runs/run-1/trace": () => ({
        status: 404,
        json: { detail: "Run not found" },
      }),
    });
    render(<App />);
    await openPanel("Trace");

    await say("hello");

    expect(await screen.findByRole("alert")).toHaveTextContent("not found");
  });
});

describe("service status", () => {
  it("shows every service as up", async () => {
    fakeApi(STATUS_OK);
    render(<App />);

    expect(await screen.findByRole("button", { name: "All services up" })).toBeInTheDocument();
    const services = await openServices();
    await waitFor(() => expect(within(services).getAllByRole("listitem")).toHaveLength(7));
    expect(within(services).getByText("Mock provider")).toBeInTheDocument();
    expect(within(services).getByText("Worker")).toBeInTheDocument();
    expect(within(services).queryByText(/is down/)).not.toBeInTheDocument();
  });

  it("names the service that is down", async () => {
    fakeApi({
      "GET /api/system/status": () => ({
        json: { ...HEALTHY_STATUS, redis: false },
      }),
    });
    render(<App />);

    // The top bar names it without opening anything.
    expect(await screen.findByRole("button", { name: "Redis is down" })).toBeInTheDocument();
    const services = await openServices();
    expect(within(services).getByText("Redis")).toHaveClass("down");
    expect(within(services).getByText("is down", { exact: false })).toHaveTextContent("is down");
  });

  it("says when the API cannot be reached", async () => {
    fakeApi({
      "GET /api/system/status": () => Promise.reject(new TypeError("down")),
    });
    render(<App />);

    expect(await screen.findByText("API unreachable")).toBeInTheDocument();
  });

  it("runs a test job until it completes", async () => {
    const id = "job-1";
    let polls = 0;
    fakeApi({
      ...STATUS_OK,
      "POST /api/system/ping": () => ({
        status: 202,
        json: { id, status: "published", attempts: 0, last_error: null },
      }),
      [`GET /api/system/ping/${id}`]: () => {
        polls += 1;
        return {
          json: {
            id,
            status: polls > 1 ? "completed" : "running",
            attempts: 1,
            last_error: null,
          },
        };
      },
    });
    render(<App />);

    await openServices();
    await userEvent.click(screen.getByRole("button", { name: "Run test job" }));

    expect(
      await screen.findByText(/Test job finished/, undefined, {
        timeout: 3000,
      }),
    ).toBeVisible();
  });

  it("reports a failed test job", async () => {
    const id = "job-2";
    fakeApi({
      ...STATUS_OK,
      "POST /api/system/ping": () => ({
        status: 202,
        json: { id, status: "failed", attempts: 5, last_error: "boom" },
      }),
    });
    render(<App />);

    await openServices();
    await userEvent.click(screen.getByRole("button", { name: "Run test job" }));

    expect(await screen.findByText("Test job failed: boom")).toBeInTheDocument();
  });
});

const CARD = {
  approval_id: "approval-1",
  action: "book",
  provider_name: "Sharma Plumbing",
  service: "plumber",
  slot: "2026-11-05T09:00:00+05:30",
  slot_label: "Thu 05/11/2026, 09:00",
  price: 1200,
  currency: "INR",
  notes: ["The quote is higher than the search price of ₹1,100."],
  expires_at: "2026-11-03T10:00:00+00:00",
  status: "pending",
};

const DECISION = {
  status: "in_progress",
  outcome: "verifying",
  reply: "Your booking is in progress. It is not confirmed yet.",
  run_id: "run-2",
  session_id: "session-1",
  approval: null,
};

describe("approval card", () => {
  const withCard = {
    ...STATUS_OK,
    "POST /api/chat": reply("run-1", "Please confirm the card.", {
      outcome: "awaiting_approval",
      approval: CARD,
    }),
    "GET /api/runs/run-1/trace": () => ({ json: traceFor("run-1") }),
    "GET /api/runs/run-2/trace": () => ({ json: traceFor("run-2") }),
  };

  it("shows the exact details from the server", async () => {
    fakeApi(withCard);
    render(<App />);
    await say("book it");

    const card = await screen.findByRole("region", {
      name: "Confirm this booking",
    });
    expect(within(card).getByText("Sharma Plumbing")).toBeInTheDocument();
    expect(within(card).getByText("Thu 05/11/2026, 09:00")).toBeInTheDocument();
    expect(within(card).getByText("₹1,200")).toBeInTheDocument();
    expect(within(card).getByText(/higher than the search price/)).toBeInTheDocument();
  });

  it("says plainly when a card is about cancelling", async () => {
    fakeApi({
      ...withCard,
      "POST /api/chat": reply("run-1", "It is not cancelled yet.", {
        outcome: "awaiting_approval",
        approval: { ...CARD, action: "cancel", notes: [] },
      }),
    });
    render(<App />);
    await say("cancel my booking");

    const card = await screen.findByRole("region", {
      name: "Cancel this booking?",
    });
    expect(within(card).getByText("Thu 05/11/2026, 09:00")).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: "Confirm" })).toBeInTheDocument();
  });

  it("shows both times when a card is about moving a booking", async () => {
    fakeApi({
      ...withCard,
      "POST /api/chat": reply("run-1", "It has not moved yet.", {
        outcome: "awaiting_approval",
        approval: {
          ...CARD,
          action: "reschedule",
          old_slot_label: "Tue 03/11/2026, 10:00",
          notes: ["This new time is not held."],
        },
      }),
    });
    render(<App />);
    await say("move it");

    const card = await screen.findByRole("region", {
      name: "Move this booking?",
    });
    expect(within(card).getByText("Now")).toBeInTheDocument();
    expect(within(card).getByText("Tue 03/11/2026, 10:00")).toBeInTheDocument();
    expect(within(card).getByText("Move to")).toBeInTheDocument();
    expect(within(card).getByText("Thu 05/11/2026, 09:00")).toBeInTheDocument();
    expect(within(card).getByText("This new time is not held.")).toBeInTheDocument();
  });

  it("confirms with a click, closes the card and reports progress honestly", async () => {
    const calls = fakeApi({
      ...withCard,
      "POST /api/approvals/approval-1/confirm": () => ({ json: DECISION }),
    });
    render(<App />);
    await say("book it");
    await userEvent.click(await screen.findByRole("button", { name: "Confirm" }));

    expect(await screen.findByText(/not confirmed yet/)).toBeInTheDocument();
    const posted = calls.find((call) => call.key.endsWith("/confirm"));
    expect(posted?.body).toEqual({ user_id: "demo-user" });
    expect(screen.queryByRole("button", { name: "Confirm" })).not.toBeInTheDocument();
    expect(screen.getByText("This card is closed.")).toBeInTheDocument();
  });

  it("can say not now", async () => {
    const calls = fakeApi({
      ...withCard,
      "POST /api/approvals/approval-1/reject": () => ({
        json: {
          ...DECISION,
          status: "rejected",
          outcome: "needs_input",
          reply: "I did not book it.",
        },
      }),
    });
    render(<App />);
    await say("book it");
    await userEvent.click(await screen.findByRole("button", { name: "Not now" }));

    expect(await screen.findByText("I did not book it.")).toBeInTheDocument();
    expect(calls.some((call) => call.key.endsWith("/reject"))).toBe(true);
    expect(screen.queryByRole("button", { name: "Confirm" })).not.toBeInTheDocument();
  });

  it("closes a card when the next reply has none", async () => {
    let turn = 0;
    fakeApi({
      ...withCard,
      "POST /api/chat": () => {
        turn += 1;
        return turn === 1
          ? {
              json: {
                run_id: "run-1",
                session_id: "s",
                outcome: "awaiting_approval",
                reason: null,
                reply: "Confirm?",
                approval: CARD,
              },
            }
          : {
              json: {
                run_id: "run-2",
                session_id: "s",
                outcome: "needs_input",
                reason: null,
                reply: "Which day instead?",
              },
            };
      },
    });
    render(<App />);
    await say("book it");
    await screen.findByRole("button", { name: "Confirm" });
    await say("change the day");

    await screen.findByText("Which day instead?");
    expect(screen.queryByRole("button", { name: "Confirm" })).not.toBeInTheDocument();
  });

  it("shows the verified result when it arrives, with no new message from the user", async () => {
    fakeApi({
      ...withCard,
      "POST /api/approvals/approval-1/confirm": () => ({ json: DECISION }),
    });
    render(<App />);
    await say("book it");
    await userEvent.click(await screen.findByRole("button", { name: "Confirm" }));
    await screen.findByText(DECISION.reply);
    act(() =>
      pushNotification({
        id: 7,
        kind: "booking_verified",
        text: "Booked and verified with the provider.",
        data: { run_id: "run-2" },
        created_at: "2026-11-03T10:00:00+00:00",
      }),
    );

    expect(await screen.findByText("Booked and verified with the provider.")).toBeInTheDocument();
  });

  it("shows a new card that arrives from the saga, and only that one can be answered", async () => {
    fakeApi({
      ...withCard,
      "POST /api/approvals/approval-1/confirm": () => ({ json: DECISION }),
    });
    render(<App />);
    await say("book it");
    await userEvent.click(await screen.findByRole("button", { name: "Confirm" }));
    await screen.findByText(DECISION.reply);
    act(() =>
      pushNotification({
        id: 8,
        kind: "booking_price_changed",
        text: "The price changed. Nothing was booked.",
        data: {
          approval: {
            ...CARD,
            approval_id: "approval-2",
            price: 1700,
            notes: [],
          },
        },
        created_at: "2026-11-03T10:00:00+00:00",
      }),
    );

    expect(await screen.findByText("₹1,700")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Confirm" })).toHaveLength(1);
  });
});

describe("after a reload", () => {
  const stored = (messages: unknown[], extra: Record<string, unknown> = {}) =>
    localStorage.setItem(
      "errand.chat.v1",
      JSON.stringify({ sessionId: "session-1", messages, ...extra }),
    );
  const currentRoute = "GET /api/bookings/current?user_id=demo-user&session_id=session-1";

  it("shows an open card this page never received", async () => {
    stored([
      {
        id: "1",
        role: "agent",
        text: "Please confirm the card.",
        runId: "run-1",
      },
    ]);
    fakeApi({
      ...STATUS_OK,
      "GET /api/runs/run-1/trace": () => ({ json: traceFor("run-1") }),
      [currentRoute]: () => ({ json: { approval: CARD, attempt_open: false } }),
    });
    render(<App />);

    const card = await screen.findByRole("region", {
      name: "Confirm this booking",
    });
    expect(within(card).getByText("Sharma Plumbing")).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: "Confirm" })).toBeEnabled();
  });

  it("does not show a card twice", async () => {
    stored([
      {
        id: "1",
        role: "agent",
        text: "Confirm?",
        runId: "run-1",
        approval: CARD,
        approvalOpen: true,
      },
    ]);
    const calls = fakeApi({
      ...STATUS_OK,
      "GET /api/runs/run-1/trace": () => ({ json: traceFor("run-1") }),
      [currentRoute]: () => ({ json: { approval: CARD, attempt_open: false } }),
    });
    render(<App />);

    await waitFor(() => expect(calls.some((call) => call.key === currentRoute)).toBe(true));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.getAllByRole("button", { name: "Confirm" })).toHaveLength(1);
  });

  it("closes a stored card the server no longer has open", async () => {
    stored([
      {
        id: "1",
        role: "agent",
        text: "Confirm?",
        runId: "run-1",
        approval: CARD,
        approvalOpen: true,
      },
    ]);
    fakeApi({
      ...STATUS_OK,
      "GET /api/runs/run-1/trace": () => ({ json: traceFor("run-1") }),
      [currentRoute]: () => ({ json: { approval: null, attempt_open: false } }),
    });
    render(<App />);

    expect(await screen.findByText("This card is closed.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Confirm" })).not.toBeInTheDocument();
  });

  it("picks up the result of a booking that was running", async () => {
    stored([{ id: "1", role: "agent", text: "In progress.", runId: "run-1" }]);
    fakeApi({
      ...STATUS_OK,
      "GET /api/runs/run-1/trace": () => ({ json: traceFor("run-1") }),
      [currentRoute]: () => ({ json: { approval: null, attempt_open: true } }),
    });
    render(<App />);
    act(() =>
      pushNotification({
        id: 9,
        kind: "booking_verified",
        text: "Booked and verified with the provider.",
        data: {},
        created_at: "2026-11-03T10:00:00+00:00",
      }),
    );

    expect(await screen.findByText("Booked and verified with the provider.")).toBeInTheDocument();
  });
});

describe("news from the server", () => {
  const CANCELLED = {
    id: 20,
    kind: "provider_cancelled",
    text: "The provider cancelled your booking.",
    data: { proactive: true, run_id: "run-9" },
  };

  beforeEach(() => {
    fakeApi({
      ...STATUS_OK,
      "GET /api/runs/run-9/trace": () => ({ json: traceFor("run-9") }),
    });
  });

  it("connects from the stored cursor, so a reload replays only what it missed", async () => {
    localStorage.setItem(
      "errand.chat.v1",
      JSON.stringify({
        sessionId: "session-1",
        messages: [],
        lastNotificationId: 12,
      }),
    );
    render(<App />);
    expect(FakeEventSource.instances[0].url).toBe("/api/events?user_id=demo-user&after=12");
    act(() => pushNotification({ ...CANCELLED, id: 12 }));
    expect(screen.queryByText(CANCELLED.text)).not.toBeInTheDocument();
    act(() => pushNotification({ ...CANCELLED, id: 13 }));
    act(() => pushNotification({ ...CANCELLED, id: 13 }));
    expect(within(chatRegion()).getAllByText(CANCELLED.text)).toHaveLength(1);
  });

  it("ignores routine news from another chat, and offers to open a provider's cancel", async () => {
    localStorage.setItem(
      "errand.chat.v1",
      JSON.stringify({ sessionId: "session-2", messages: [] }),
    );
    render(<App />);
    act(() =>
      pushNotification({
        id: 19,
        kind: "booking_verified",
        text: "Booked.",
        data: {},
      }),
    );
    expect(screen.queryByText("Booked.")).not.toBeInTheDocument();

    act(() => pushNotification(CANCELLED));
    await userEvent.click(await screen.findByRole("button", { name: "Open that chat" }));

    expect(within(chatRegion()).getByText(CANCELLED.text)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Open that chat" })).not.toBeInTheDocument();
    expect(screen.getByText("Session session-")).toBeInTheDocument();
  });

  it("closes a card that a provider's cancel made useless", async () => {
    const card = { ...CARD, approval_id: "approval-7" };
    localStorage.setItem(
      "errand.chat.v1",
      JSON.stringify({
        sessionId: "session-1",
        messages: [
          {
            id: "c",
            role: "agent",
            text: "Check.",
            approval: card,
            approvalOpen: true,
          },
        ],
      }),
    );
    render(<App />);
    act(() =>
      pushNotification({
        ...CANCELLED,
        data: { proactive: true, closed_cards: ["approval-7"] },
      }),
    );
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Confirm" })).not.toBeInTheDocument(),
    );
  });
});

describe("memory panel", () => {
  const SAID = {
    id: "m1",
    key: "area",
    value: "Indiranagar",
    scope: "all",
    source: "explicit",
    quote: "I live in Indiranagar",
    confirmed: true,
    updated_at: "2026-11-02T12:00:00+00:00",
  };
  const GUESS = {
    ...SAID,
    id: "m2",
    key: "language",
    value: "Hindi",
    source: "inferred",
    confirmed: false,
  };
  const LIST = "GET /api/memories?user_id=demo-user";

  it("shows what is known, and marks what is not used", async () => {
    fakeApi({
      ...STATUS_OK,
      [LIST]: () => ({ json: { memories: [SAID, GUESS] } }),
    });
    render(<App />);

    await openPanel("Memory");
    const panel = screen.getByRole("region", { name: "Memory" });
    expect(await within(panel).findByText("Indiranagar")).toBeInTheDocument();
    expect(within(panel).getByText("You said this")).toBeInTheDocument();
    expect(within(panel).getByText("Not confirmed, not used")).toBeInTheDocument();
    expect(within(panel).getAllByRole("button", { name: "Confirm" })).toHaveLength(1);
  });

  it("confirms, corrects and forgets through the server, then reads again", async () => {
    const calls = fakeApi({
      ...STATUS_OK,
      [LIST]: () => ({ json: { memories: [SAID, GUESS] } }),
      "POST /api/memories/m2/confirm": () => ({
        json: { ...GUESS, confirmed: true },
      }),
      "PUT /api/memories/m1": () => ({
        json: { ...SAID, value: "Whitefield" },
      }),
      "DELETE /api/memories/m1?user_id=demo-user": () => ({
        json: null,
        status: 200,
      }),
    });
    render(<App />);
    await openPanel("Memory");
    const panel = screen.getByRole("region", { name: "Memory" });
    await within(panel).findByText("Indiranagar");

    await userEvent.click(within(panel).getByRole("button", { name: "Confirm" }));
    await waitFor(() =>
      expect(calls.find((call) => call.key === "POST /api/memories/m2/confirm")?.body).toEqual({
        user_id: "demo-user",
      }),
    );
    await waitFor(() => expect(calls.filter((call) => call.key === LIST)).toHaveLength(2));

    // What waits for an answer comes first, so the trusted memory is second.
    await userEvent.click(within(panel).getAllByRole("button", { name: "Correct" })[1]);
    const input = within(panel).getByLabelText("New value for area");
    await userEvent.clear(input);
    await userEvent.type(input, "Whitefield");
    await userEvent.click(within(panel).getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(calls.find((call) => call.key === "PUT /api/memories/m1")?.body).toEqual({
        user_id: "demo-user",
        value: "Whitefield",
      }),
    );
    await waitFor(() => expect(within(panel).queryByRole("button", { name: "Save" })).toBeNull());

    await userEvent.click(within(panel).getAllByRole("button", { name: "Forget" })[1]);
    await waitFor(() =>
      expect(calls.some((call) => call.key === "DELETE /api/memories/m1?user_id=demo-user")).toBe(
        true,
      ),
    );
  });

  it("reads again after a chat turn, which may have changed a memory", async () => {
    const calls = fakeApi({
      ...STATUS_OK,
      [LIST]: () => ({ json: { memories: [] } }),
      "POST /api/chat": reply("run-1", "Noted."),
      "GET /api/runs/run-1/trace": () => ({ json: traceFor("run-1") }),
    });
    render(<App />);

    await say("I live in Indiranagar");
    await screen.findByText("Noted.");
    await waitFor(() =>
      expect(calls.filter((call) => call.key === LIST).length).toBeGreaterThanOrEqual(2),
    );
  });

  it("shows what was saved in the chat, and asks about a guess there", async () => {
    const calls = fakeApi({
      ...STATUS_OK,
      [LIST]: () => ({ json: { memories: [] } }),
      "POST /api/chat": reply("run-1", "What do you need?", {
        memory: { saved: [SAID], to_confirm: [GUESS] },
      }),
      "GET /api/runs/run-1/trace": () => ({ json: traceFor("run-1") }),
      "POST /api/memories/m2/confirm": () => ({
        json: { ...GUESS, confirmed: true },
      }),
    });
    render(<App />);

    await say("I live in Indiranagar");
    const chat = screen.getByRole("region", { name: "Chat" });
    expect(await within(chat).findByText("Remembered area: Indiranagar")).toBeInTheDocument();
    const question = within(chat).getByRole("region", {
      name: "Remember this?",
    });
    expect(within(question).getByText("Hindi")).toBeInTheDocument();

    const before = calls.filter((call) => call.key === LIST).length;
    await userEvent.click(within(question).getByRole("button", { name: "Remember" }));

    expect(await within(chat).findByText("Remembered language: Hindi")).toBeInTheDocument();
    expect(within(chat).queryByRole("region", { name: "Remember this?" })).toBeNull();
    await waitFor(() =>
      expect(calls.filter((call) => call.key === LIST).length).toBeGreaterThan(before),
    );
  });

  it("drops the question when the user says no, or moves on", async () => {
    let turn = 0;
    const calls = fakeApi({
      ...STATUS_OK,
      [LIST]: () => ({ json: { memories: [] } }),
      "POST /api/chat": () => {
        turn += 1;
        return reply("run-1", `Reply ${turn}.`, {
          memory: { saved: [], to_confirm: turn === 1 ? [GUESS] : [] },
        })();
      },
      "GET /api/runs/run-1/trace": () => ({ json: traceFor("run-1") }),
      "DELETE /api/memories/m2?user_id=demo-user": () => ({ json: null }),
    });
    render(<App />);
    const chat = screen.getByRole("region", { name: "Chat" });

    await say("first");
    await within(chat).findByRole("region", { name: "Remember this?" });
    await say("second");
    await within(chat).findByText("Reply 2.");
    expect(within(chat).queryByRole("region", { name: "Remember this?" })).toBeNull();
    expect(calls.some((call) => call.key.startsWith("DELETE"))).toBe(false);
  });
});

describe("panels in the top bar", () => {
  it("keeps both closed until asked, and shows one at a time", async () => {
    fakeApi({ ...STATUS_OK, "GET /api/memories?user_id=demo-user": () => ({ json: { memories: [] } }) });
    render(<App />);
    expect(screen.queryByRole("region", { name: "Run trace" })).toBeNull();
    expect(screen.queryByRole("region", { name: "Memory" })).toBeNull();

    await openPanel("Trace");
    expect(screen.getByRole("region", { name: "Run trace" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Trace" })).toHaveAttribute("aria-expanded", "true");

    await openPanel("Memory");
    expect(screen.getByRole("region", { name: "Memory" })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Run trace" })).toBeNull();

    await openPanel("Memory");
    expect(screen.queryByRole("region", { name: "Memory" })).toBeNull();

    await openPanel("Trace");
    await userEvent.click(screen.getByRole("button", { name: "Close panel" }));
    expect(screen.queryByRole("region", { name: "Run trace" })).toBeNull();
  });

  it("opens the trace from a reply", async () => {
    fakeApi({
      ...STATUS_OK,
      "POST /api/chat": reply("run-1", "Done."),
      "GET /api/runs/run-1/trace": () => ({ json: traceFor("run-1") }),
    });
    render(<App />);
    await say("hello");
    await screen.findByText("Done.");
    expect(screen.queryByRole("region", { name: "Run trace" })).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "View trace" }));

    expect(await screen.findByText("Run run-1")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "View trace" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("shows how many memories there are, without opening the panel", async () => {
    fakeApi({
      ...STATUS_OK,
      "GET /api/memories?user_id=demo-user": () => ({
        json: {
          memories: [
            { id: "m1", key: "area", value: "HSR", scope: "all", source: "explicit", confirmed: true },
            { id: "m2", key: "pet", value: "Dog", scope: "all", source: "inferred", confirmed: false },
          ],
        },
      }),
    });
    render(<App />);
    const button = screen.getByRole("button", { name: "Memory" });
    expect(await within(button).findByText("2")).toHaveClass("attention");
  });
});

describe("agent replies", () => {
  it("renders formatting, but never a link or an image from the model", async () => {
    fakeApi({
      ...STATUS_OK,
      "POST /api/chat": reply(
        "run-1",
        "**Sharma Plumbing** is best.\n\n[Pay here](https://evil.example) ![x](https://evil.example/a.png)",
      ),
      "GET /api/runs/run-1/trace": () => ({ json: traceFor("run-1") }),
    });
    render(<App />);
    await say("find one");

    const name = await screen.findByText("Sharma Plumbing");
    expect(name.tagName).toBe("STRONG");
    expect(screen.getByText(/Pay here/)).toBeInTheDocument();
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.queryByRole("img")).toBeNull();
  });

  it("does not format what the user typed", async () => {
    fakeApi({
      ...STATUS_OK,
      "POST /api/chat": reply("run-1", "Done."),
      "GET /api/runs/run-1/trace": () => ({ json: traceFor("run-1") }),
    });
    render(<App />);
    await say("**plain**");
    expect(await within(chatRegion()).findByText("**plain**")).toBeInTheDocument();
  });
});

describe("previous chats", () => {
  function twoChats() {
    let turn = 0;
    return fakeApi({
      ...STATUS_OK,
      "POST /api/chat": () => {
        turn += 1;
        return { json: { run_id: `run-${turn}`, session_id: `session-${turn}`, outcome: "done", reason: null, reply: `Reply ${turn}` } };
      },
      "GET /api/runs/run-1/trace": () => ({ json: traceFor("run-1") }),
      "GET /api/runs/run-2/trace": () => ({ json: traceFor("run-2") }),
    });
  }

  async function startTwoChats() {
    render(<App />);
    await say("first chat");
    await screen.findByText("Reply 1");
    await userEvent.click(screen.getByRole("button", { name: "New chat" }));
    await say("second chat");
    await screen.findByText("Reply 2");
    return screen.getByRole("navigation", { name: "Previous chats" });
  }

  it("lists each chat, newest first, and marks the open one", async () => {
    twoChats();
    const recents = await startTwoChats();
    const rows = within(recents).getAllByRole("button");
    expect(rows.map((row) => row.textContent)).toEqual(["second chat", "first chat"]);
    expect(rows[0]).toHaveAttribute("aria-current", "page");
  });

  it("reopens a chat with its messages and carries on in its own session", async () => {
    const calls = twoChats();
    const recents = await startTwoChats();

    await userEvent.click(within(recents).getByRole("button", { name: "first chat" }));

    expect(within(chatRegion()).getByText("Reply 1")).toBeInTheDocument();
    expect(within(chatRegion()).queryByText("Reply 2")).toBeNull();
    expect(screen.getByRole("heading", { name: "first chat" })).toBeInTheDocument();
    // This browser's copy may be behind, so the server says which card is open.
    await waitFor(() =>
      expect(
        calls.some(
          (call) =>
            call.key === "GET /api/bookings/current?user_id=demo-user&session_id=session-1",
        ),
      ).toBe(true),
    );

    await say("more");
    await waitFor(() => {
      const last = calls.filter((call) => call.key === "POST /api/chat").at(-1);
      expect(last?.body).toEqual({ user_id: "demo-user", session_id: "session-1", message: "more" });
    });
  });

  it("survives a reload", async () => {
    twoChats();
    await startTwoChats();
    cleanup();

    render(<App />);
    const recents = screen.getByRole("navigation", { name: "Previous chats" });
    expect(within(recents).getByRole("button", { name: "first chat" })).toBeInTheDocument();
    expect(within(recents).getByRole("button", { name: "second chat" })).toBeInTheDocument();
  });

  it("adds a chat stored before the list existed", () => {
    fakeApi(STATUS_OK);
    localStorage.setItem(
      "errand.chat.v1",
      JSON.stringify({
        sessionId: "session-old",
        messages: [{ id: "a", role: "user", text: "old chat" }],
      }),
    );
    render(<App />);
    const recents = screen.getByRole("navigation", { name: "Previous chats" });
    expect(within(recents).getByRole("button", { name: "old chat" })).toBeInTheDocument();
  });

  it("does not switch chats while a reply is on its way", async () => {
    let finish: (value: unknown) => void = () => undefined;
    const pending = new Promise((resolve) => {
      finish = resolve;
    });
    let turn = 0;
    fakeApi({
      ...STATUS_OK,
      "POST /api/chat": () => {
        turn += 1;
        const body = { json: { run_id: `run-${turn}`, session_id: `session-${turn}`, outcome: "done", reason: null, reply: `Reply ${turn}` } };
        return turn === 1 ? body : (pending.then(() => body) as never);
      },
      "GET /api/runs/run-1/trace": () => ({ json: traceFor("run-1") }),
    });
    render(<App />);
    await say("first chat");
    await screen.findByText("Reply 1");
    await userEvent.click(screen.getByRole("button", { name: "New chat" }));
    await say("second chat");

    const recents = screen.getByRole("navigation", { name: "Previous chats" });
    expect(within(recents).getByRole("button", { name: "first chat" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "New chat" })).toBeDisabled();
    await act(async () => finish(null));
  });

  it("keeps routine news with its own chat, for when it is opened", async () => {
    twoChats();
    const recents = await startTwoChats();

    act(() =>
      pushNotification({ id: 30, session_id: "session-1", kind: "booking_verified", text: "Booked." }),
    );
    expect(screen.queryByText("Booked.")).toBeNull();

    await userEvent.click(within(recents).getByRole("button", { name: "first chat" }));
    expect(within(chatRegion()).getByText("Booked.")).toBeInTheDocument();
  });

  it("opens a stored chat in full from a provider's cancel", async () => {
    twoChats();
    await startTwoChats();

    act(() =>
      pushNotification({
        id: 31,
        session_id: "session-1",
        kind: "provider_cancelled",
        text: "The provider cancelled.",
        data: { proactive: true },
      }),
    );
    await userEvent.click(await screen.findByRole("button", { name: "Open that chat" }));

    expect(within(chatRegion()).getByText("Reply 1")).toBeInTheDocument();
    expect(within(chatRegion()).getByText("The provider cancelled.")).toBeInTheDocument();
  });
});
