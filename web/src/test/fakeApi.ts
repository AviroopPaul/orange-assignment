import { vi } from "vitest";

/** The only fake in UI tests: the HTTP boundary. Everything above it is the real app. */
type Handler = (body: unknown) => { status?: number; json: unknown } | Promise<never>;

export function fakeApi(routes: Record<string, Handler>) {
  const calls: { key: string; body: unknown }[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const key = `${init?.method ?? "GET"} ${String(input)}`;
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ key, body });
    const handler = routes[key];
    if (!handler) {
      return new Response(JSON.stringify({ detail: "not found" }), { status: 404 });
    }
    const result = await handler(body);
    return new Response(JSON.stringify(result.json), { status: result.status ?? 200 });
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}

export const HEALTHY_STATUS = {
  ready: true,
  postgres: true,
  redis: true,
  worker: true,
  scheduler: true,
  provider: true,
  model_configured: true,
};

export function traceFor(runId: string, overrides: Record<string, unknown> = {}) {
  return {
    id: runId,
    user_id: "demo-user",
    session_id: "session-1",
    outcome: "done",
    reason: null,
    reply: "ok",
    model_calls: 1,
    input_tokens: 263,
    output_tokens: 10,
    estimated_cost_usd: 0.0001039,
    started_at: "2026-11-02T12:00:00+00:00",
    finished_at: "2026-11-02T12:00:01.500000+00:00",
    events: [
      {
        seq: 1,
        kind: "model_call",
        name: "gemini-3.5-flash-lite",
        data: { requested_tools: [] },
        latency_ms: 842,
        input_tokens: 263,
        output_tokens: 10,
        estimated_cost_usd: 0.0001039,
        created_at: "2026-11-02T12:00:01+00:00",
      },
      {
        seq: 2,
        kind: "outcome",
        name: "done",
        data: { reason: null },
        latency_ms: null,
        input_tokens: null,
        output_tokens: null,
        estimated_cost_usd: null,
        created_at: "2026-11-02T12:00:01+00:00",
      },
    ],
    ...overrides,
  };
}
