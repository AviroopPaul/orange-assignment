import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { App } from "./App";
import { fakeApi, HEALTHY_STATUS, traceFor } from "./test/fakeApi";

const STATUS_OK = { "GET /api/system/status": () => ({ json: HEALTHY_STATUS }) };

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
    expect(screen.getByText("I need a plumber")).toBeInTheDocument();
    const posted = calls.find((call) => call.key === "POST /api/chat");
    expect(posted?.body).toEqual({ user_id: "demo-user", message: "I need a plumber" });

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
        json: traceFor("run-1", { outcome: "tool_failed", reason: "model_unavailable" }),
      }),
    });
    render(<App />);

    await say("hello");

    expect(await screen.findByText("Failed")).toBeInTheDocument();
    expect(screen.getByText("The AI model is not responding right now.")).toBeInTheDocument();
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
    expect(screen.getByText("remember me")).toBeInTheDocument();
    expect(screen.getByText("Session session-")).toBeInTheDocument();
    expect(await screen.findByText("Run run-1")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "New chat" }));
    expect(screen.queryByText("remember me")).not.toBeInTheDocument();
    expect(screen.getByText("New session")).toBeInTheDocument();

    await say("fresh");
    await waitFor(() => {
      const last = calls.filter((call) => call.key === "POST /api/chat").at(-1);
      expect(last?.body).toEqual({ user_id: "demo-user", message: "fresh" });
    });
  });

  it("says plainly that booking is not connected yet", () => {
    fakeApi(STATUS_OK);
    render(<App />);
    expect(
      screen.getByText(/Holding, booking and memory are not connected yet/),
    ).toBeInTheDocument();
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
              data: { caller: "model", reason: "not_allowed", args: { hold_id: "h1" } },
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

    await say("tell me about them");

    expect(await screen.findByText("Tool call")).toBeInTheDocument();
    expect(screen.getByText("get_provider")).toBeInTheDocument();
    expect(screen.getByText("ignore_instructions, skip_approval")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
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
              data: { caller: "saga", status: "timeout", error_code: "timeout", ambiguous: true },
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

    await say("hello");

    expect(await screen.findByText(/check the source of truth/)).toBeInTheDocument();
  });

  it("explains when a trace cannot be loaded", async () => {
    fakeApi({
      ...STATUS_OK,
      "POST /api/chat": reply("run-1", "Noted."),
      "GET /api/runs/run-1/trace": () => ({ status: 404, json: { detail: "Run not found" } }),
    });
    render(<App />);

    await say("hello");

    expect(await screen.findByRole("alert")).toHaveTextContent("not found");
  });
});

describe("status bar", () => {
  it("shows every service as up", async () => {
    fakeApi(STATUS_OK);
    render(<App />);

    const services = await screen.findByRole("list", { name: "Service status" });
    await waitFor(() => expect(within(services).getAllByRole("listitem")).toHaveLength(7));
    expect(within(services).getByText("Mock provider")).toBeInTheDocument();
    expect(within(services).getByText("Worker")).toBeInTheDocument();
    expect(within(services).queryByText(/is down/)).not.toBeInTheDocument();
  });

  it("names the service that is down", async () => {
    fakeApi({
      "GET /api/system/status": () => ({ json: { ...HEALTHY_STATUS, redis: false } }),
    });
    render(<App />);

    expect(await screen.findByText("is down", { exact: false })).toHaveTextContent("is down");
    const redis = screen.getByText("Redis");
    expect(redis).toHaveClass("down");
  });

  it("says when the API cannot be reached", async () => {
    fakeApi({ "GET /api/system/status": () => Promise.reject(new TypeError("down")) });
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

    await userEvent.click(screen.getByRole("button", { name: "Run test job" }));

    expect(await screen.findByText(/Test job finished/, undefined, { timeout: 3000 })).toBeVisible();
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

    await userEvent.click(screen.getByRole("button", { name: "Run test job" }));

    expect(await screen.findByText("Test job failed: boom")).toBeInTheDocument();
  });
});
