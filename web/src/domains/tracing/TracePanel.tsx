import {
  ArrowRight,
  Brain,
  CalendarCheck,
  ChevronRight,
  CircleAlert,
  Flag,
  Layers,
  ListTree,
  Loader,
  ShieldCheck,
  Sparkles,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";

import { Chip, EmptyState } from "../../components/ui";
import { ApiError, request } from "../../lib/api";
import { formatCost, formatLatency, kindLabel, runDurationMs } from "./format";
import type { RunTrace, TraceEvent } from "./types";

type TraceState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "loaded"; trace: RunTrace }
  | { status: "error"; message: string };

const REFRESH_MS = 2000;

function useRunTrace(runId: string | null): TraceState {
  const [state, setState] = useState<TraceState>({ status: "idle" });
  const running =
    state.status === "loaded" && (!state.trace.outcome || state.trace.outcome === "verifying");

  // A worker keeps adding to a run after the first news, so follow it until it settles.
  useEffect(() => {
    if (!runId || !running) return;
    const timer = setInterval(() => {
      request<RunTrace>(`/runs/${runId}/trace`)
        .then((trace) => setState({ status: "loaded", trace }))
        .catch(() => {
          // The next tick tries again.
        });
    }, REFRESH_MS);
    return () => clearInterval(timer);
  }, [runId, running]);

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
        const message = error instanceof ApiError ? error.message : "Could not load this trace.";
        setState({ status: "error", message });
      });
    return () => {
      cancelled = true;
    };
  }, [runId]);

  return state;
}

interface Row {
  label: string;
  value: ReactNode;
  flagged?: boolean;
}

function eventRows(event: TraceEvent): Row[] {
  const { data } = event;
  const rows: Row[] = [];
  const add = (label: string, value: ReactNode, flagged = false) =>
    rows.push({ label, value, flagged });

  if (data.from != null && data.to != null) add("Moved", `${String(data.from)} to ${String(data.to)}`);
  if (data.dates != null) add("Dates", String(data.dates));
  if (data.wait_s != null) add("Waited", `${String(data.wait_s)} s`);
  if (data.retry_after_s != null) add("Try again in", `${String(data.retry_after_s)} s`);
  if (data.answer != null) add("Answer", `${String(data.answer)} (${String(data.source)})`);
  if (data.after_book_result != null) add("After book said", String(data.after_book_result));
  if (data.status != null) add("Status", String(data.error_code ?? data.status));
  if (data.caller != null) add("Caller", String(data.caller));
  if (Number(data.attempts ?? 0) > 1) add("Attempts", Number(data.attempts));
  if (data.ambiguous === true) add("Outcome", "Unknown, check the source of truth", true);
  if (Array.isArray(data.injection_flags) && data.injection_flags.length > 0) {
    add("Suspicious text", (data.injection_flags as string[]).join(", "), true);
  }
  if (data.args != null && Object.keys(data.args as object).length > 0) {
    add("Arguments", <code>{JSON.stringify(data.args)}</code>);
  }
  if (event.input_tokens !== null) {
    add("Tokens", `${event.input_tokens} in, ${event.output_tokens ?? 0} out`);
  }
  if (event.estimated_cost_usd !== null) add("Cost", formatCost(event.estimated_cost_usd));
  if (Array.isArray(data.requested_tools) && data.requested_tools.length > 0) {
    add("Tools asked for", (data.requested_tools as string[]).join(", "));
  }
  if (data.reason != null) add("Reason", String(data.reason));
  if (data.error != null) add("Error", String(data.error));
  return rows;
}

const BAD_KIND = /error|refused|failed|unavailable|mismatch|false_claim|rate_limited|slot_taken/;

function kindIcon(kind: string): LucideIcon {
  if (BAD_KIND.test(kind)) return CircleAlert;
  if (kind.startsWith("model")) return Sparkles;
  if (kind.startsWith("tool")) return Wrench;
  if (kind.includes("memory")) return Brain;
  if (kind.startsWith("approval")) return ShieldCheck;
  if (kind.startsWith("context")) return Layers;
  if (kind === "state_transition") return ArrowRight;
  if (kind === "outcome") return Flag;
  return CalendarCheck;
}

function EventRow({ event }: { event: TraceEvent }) {
  const rows = eventRows(event);
  const bad = BAD_KIND.test(event.kind);
  const Icon = kindIcon(event.kind);
  const head = (
    <>
      <span className="event-icon" title={`Step ${event.seq}`} aria-hidden="true">
        <Icon size={13} />
      </span>
      <span className="event-title">
        <span className="event-kind">{kindLabel(event.kind)}</span>
        {event.name && <code className="event-name">{event.name}</code>}
      </span>
      {event.latency_ms !== null && (
        <span className="event-latency">{formatLatency(event.latency_ms)}</span>
      )}
      {rows.length > 0 && <ChevronRight className="event-chevron" size={14} aria-hidden="true" />}
    </>
  );
  return (
    <li className={`event ${bad ? "bad" : ""}`}>
      {rows.length === 0 ? (
        <div className="event-head">{head}</div>
      ) : (
        // Problems start open, so nobody has to go looking for them.
        <details open={bad || rows.some((row) => row.flagged)}>
          <summary className="event-head">{head}</summary>
          <dl className="event-details">
            {rows.map((row) => (
              <div key={row.label} className={row.flagged ? "flagged" : undefined}>
                <dt>{row.label}</dt>
                <dd>{row.value}</dd>
              </div>
            ))}
          </dl>
        </details>
      )}
    </li>
  );
}

function outcomeLabel(outcome: string | null): string {
  if (!outcome) return "Running";
  const text = outcome.replaceAll("_", " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function TraceView({ trace }: { trace: RunTrace }) {
  const running = !trace.outcome || trace.outcome === "verifying";
  return (
    <>
      <div className="trace-outcome">
        <Chip tone={trace.outcome === "done" ? "good" : running ? "accent" : "warn"}>
          {running && <Loader className="spin" size={12} aria-hidden="true" />}
          {outcomeLabel(trace.outcome)}
        </Chip>
        <span className="trace-id">Run {trace.id}</span>
      </div>
      <dl className="trace-summary">
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

      <h3 className="trace-heading">Steps ({trace.events.length})</h3>
      <ol className="events">
        {trace.events.map((event) => (
          <EventRow key={event.seq} event={event} />
        ))}
      </ol>
    </>
  );
}

export function TracePanel({ runId }: { runId: string | null }) {
  const state = useRunTrace(runId);

  return (
    <>
      {state.status === "idle" && (
        <EmptyState icon={ListTree}>
          Send a message to see what the agent did for that turn.
        </EmptyState>
      )}
      {state.status === "loading" && <p className="panel-note">Loading trace…</p>}
      {state.status === "error" && (
        <p className="panel-error" role="alert">
          {state.message}
        </p>
      )}
      {state.status === "loaded" && <TraceView trace={state.trace} />}
    </>
  );
}
