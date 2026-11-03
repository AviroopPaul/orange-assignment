import { useEffect, useState } from "react";

import { ApiError, request } from "../../lib/api";
import { formatCost, formatLatency, kindLabel, runDurationMs } from "./format";
import type { RunTrace, TraceEvent } from "./types";

type TraceState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "loaded"; trace: RunTrace }
  | { status: "error"; message: string };

function useRunTrace(runId: string | null): TraceState {
  const [state, setState] = useState<TraceState>({ status: "idle" });

  useEffect(() => {
    if (!runId) {
      setState({ status: "idle" });
      return;
    }
    let cancelled = false;
    setState({ status: "loading" });
    request<RunTrace>(`/runs/${runId}/trace`)
      .then((trace) => {
        if (!cancelled) setState({ status: "loaded", trace });
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        const message =
          error instanceof ApiError ? error.message : "Could not load this trace.";
        setState({ status: "error", message });
      });
    return () => {
      cancelled = true;
    };
  }, [runId]);

  return state;
}

function EventDetails({ event }: { event: TraceEvent }) {
  const tools = event.data.requested_tools;
  const hasTools = Array.isArray(tools) && tools.length > 0;
  const flags = event.data.injection_flags;
  const hasFlags = Array.isArray(flags) && flags.length > 0;
  const attempts = Number(event.data.attempts ?? 0);
  return (
    <dl className="event-details">
      {event.data.from != null && event.data.to != null && (
        <div>
          <dt>Moved</dt>
          <dd>
            {String(event.data.from)} to {String(event.data.to)}
          </dd>
        </div>
      )}
      {event.data.dates != null && (
        <div>
          <dt>Dates</dt>
          <dd>{String(event.data.dates)}</dd>
        </div>
      )}
      {event.data.wait_s != null && (
        <div>
          <dt>Waited</dt>
          <dd>{String(event.data.wait_s)} s</dd>
        </div>
      )}
      {event.data.retry_after_s != null && (
        <div>
          <dt>Try again in</dt>
          <dd>{String(event.data.retry_after_s)} s</dd>
        </div>
      )}
      {event.data.status != null && (
        <div>
          <dt>Status</dt>
          <dd>{String(event.data.error_code ?? event.data.status)}</dd>
        </div>
      )}
      {event.data.caller != null && (
        <div>
          <dt>Caller</dt>
          <dd>{String(event.data.caller)}</dd>
        </div>
      )}
      {attempts > 1 && (
        <div>
          <dt>Attempts</dt>
          <dd>{attempts}</dd>
        </div>
      )}
      {event.data.ambiguous === true && (
        <div>
          <dt>Outcome</dt>
          <dd>Unknown, check the source of truth</dd>
        </div>
      )}
      {hasFlags && (
        <div className="flagged">
          <dt>Suspicious text</dt>
          <dd>{(flags as string[]).join(", ")}</dd>
        </div>
      )}
      {event.data.args != null && Object.keys(event.data.args as object).length > 0 && (
        <div>
          <dt>Arguments</dt>
          <dd>
            <code>{JSON.stringify(event.data.args)}</code>
          </dd>
        </div>
      )}
      {event.latency_ms !== null && (
        <div>
          <dt>Latency</dt>
          <dd>{formatLatency(event.latency_ms)}</dd>
        </div>
      )}
      {event.input_tokens !== null && (
        <div>
          <dt>Tokens</dt>
          <dd>
            {event.input_tokens} in, {event.output_tokens ?? 0} out
          </dd>
        </div>
      )}
      {event.estimated_cost_usd !== null && (
        <div>
          <dt>Cost</dt>
          <dd>{formatCost(event.estimated_cost_usd)}</dd>
        </div>
      )}
      {hasTools && (
        <div>
          <dt>Tools asked for</dt>
          <dd>{(tools as string[]).join(", ")}</dd>
        </div>
      )}
      {event.data.reason != null && (
        <div>
          <dt>Reason</dt>
          <dd>{String(event.data.reason)}</dd>
        </div>
      )}
      {event.data.error != null && (
        <div>
          <dt>Error</dt>
          <dd>{String(event.data.error)}</dd>
        </div>
      )}
    </dl>
  );
}

function TraceView({ trace }: { trace: RunTrace }) {
  return (
    <>
      <dl className="trace-summary">
        <div>
          <dt>Outcome</dt>
          <dd>{trace.outcome ?? "running"}</dd>
        </div>
        <div>
          <dt>Duration</dt>
          <dd>{formatLatency(runDurationMs(trace.started_at, trace.finished_at))}</dd>
        </div>
        <div>
          <dt>Model calls</dt>
          <dd>{trace.model_calls}</dd>
        </div>
        <div>
          <dt>Tokens</dt>
          <dd>
            {trace.input_tokens} in, {trace.output_tokens} out
          </dd>
        </div>
        <div>
          <dt>Est. cost</dt>
          <dd>{formatCost(trace.estimated_cost_usd)}</dd>
        </div>
      </dl>

      <ol className="events">
        {trace.events.map((event) => (
          <li key={event.seq} className={`event kind-${event.kind}`}>
            <div className="event-head">
              <span className="event-seq">{event.seq}</span>
              <span className="event-kind">{kindLabel(event.kind)}</span>
              {event.name && <code className="event-name">{event.name}</code>}
            </div>
            <EventDetails event={event} />
          </li>
        ))}
      </ol>

      <p className="trace-id">Run {trace.id}</p>
    </>
  );
}

export function TracePanel({ runId }: { runId: string | null }) {
  const state = useRunTrace(runId);

  return (
    <section className="panel trace" aria-label="Run trace">
      <header className="panel-head">
        <h2>Run trace</h2>
      </header>
      <div className="trace-body">
        {state.status === "idle" && (
          <p className="placeholder">Send a message to see what the agent did for that turn.</p>
        )}
        {state.status === "loading" && <p className="placeholder">Loading trace…</p>}
        {state.status === "error" && (
          <p className="trace-error" role="alert">
            {state.message}
          </p>
        )}
        {state.status === "loaded" && <TraceView trace={state.trace} />}
      </div>
    </section>
  );
}
