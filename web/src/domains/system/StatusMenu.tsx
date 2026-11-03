import { ChevronDown, Play } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { Button, Popover } from "../../components/ui";
import { ApiError, request } from "../../lib/api";

export interface SystemStatus {
  ready: boolean;
  postgres: boolean;
  redis: boolean;
  worker: boolean;
  scheduler: boolean;
  provider: boolean;
  model_configured: boolean;
}

interface Ping {
  id: string;
  status: string;
  attempts: number;
  last_error: string | null;
}

const POLL_INTERVAL_MS = 5000;
const STATUS_TIMEOUT_MS = 4000;
const PING_POLL_MS = 300;
const PING_LIMIT_MS = 15_000;

const SERVICES: { key: keyof SystemStatus; label: string }[] = [
  { key: "postgres", label: "PostgreSQL" },
  { key: "redis", label: "Redis" },
  { key: "worker", label: "Worker" },
  { key: "scheduler", label: "Scheduler" },
  { key: "provider", label: "Mock provider" },
  { key: "model_configured", label: "Gemini key" },
];

function useSystemStatus(): SystemStatus | "unreachable" | null {
  const [status, setStatus] = useState<SystemStatus | "unreachable" | null>(null);

  useEffect(() => {
    let cancelled = false;
    const check = async () => {
      try {
        const next = await request<SystemStatus>("/system/status", {}, STATUS_TIMEOUT_MS);
        if (!cancelled) setStatus(next);
      } catch {
        if (!cancelled) setStatus("unreachable");
      }
    };
    void check();
    const timer = setInterval(() => void check(), POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  return status;
}

async function runTestJob(): Promise<string> {
  const startedAt = performance.now();
  let ping = await request<Ping>("/system/ping", { method: "POST" });
  while (performance.now() - startedAt < PING_LIMIT_MS) {
    if (ping.status === "completed") {
      const seconds = ((performance.now() - startedAt) / 1000).toFixed(1);
      return `Test job finished through Redis and the worker in ${seconds} s.`;
    }
    if (ping.status === "failed") {
      return `Test job failed: ${ping.last_error ?? "no details"}`;
    }
    await new Promise((resolve) => setTimeout(resolve, PING_POLL_MS));
    ping = await request<Ping>(`/system/ping/${ping.id}`);
  }
  return `Test job is still ${ping.status} after 15 s. It will finish when the worker recovers.`;
}

/** The top bar names what is wrong, so a failure shows without opening the list. */
function summarize(status: SystemStatus | "unreachable" | null): { text: string; down: boolean } {
  if (status === null) return { text: "Checking services", down: false };
  if (status === "unreachable") return { text: "API unreachable", down: true };
  const down = SERVICES.filter(({ key }) => !status[key]);
  if (down.length === 0) return { text: "All services up", down: false };
  if (down.length === 1) return { text: `${down[0].label} is down`, down: true };
  return { text: `${down.length} services down`, down: true };
}

export function StatusMenu() {
  const status = useSystemStatus();
  const [jobResult, setJobResult] = useState<string | null>(null);
  const [jobRunning, setJobRunning] = useState(false);
  const summary = summarize(status);

  const onRunJob = useCallback(async () => {
    setJobRunning(true);
    setJobResult(null);
    try {
      setJobResult(await runTestJob());
    } catch (error) {
      setJobResult(error instanceof ApiError ? error.message : "The test job could not start.");
    } finally {
      setJobRunning(false);
    }
  }, []);

  return (
    <Popover
      trigger={({ open, toggle }) => (
        <button
          type="button"
          className={`status-trigger ${summary.down ? "down" : "up"}`}
          aria-label={summary.text}
          aria-expanded={open}
          onClick={toggle}
        >
          <span className="dot" aria-hidden="true" />
          <span className="status-text">{summary.text}</span>
          <ChevronDown size={14} aria-hidden="true" />
        </button>
      )}
    >
      <h3 className="popover-title">Services</h3>
      {status === null && <p className="popover-note">Checking services…</p>}
      <ul className="services" aria-label="Service status">
        {status === "unreachable" && (
          <li className="service down">
            <span className="dot" aria-hidden="true" />
            API unreachable
          </li>
        )}
        {status && status !== "unreachable" && (
          <>
            <li className="service up">
              <span className="dot" aria-hidden="true" />
              API
            </li>
            {SERVICES.map(({ key, label }) => (
              <li key={key} className={`service ${status[key] ? "up" : "down"}`}>
                <span className="dot" aria-hidden="true" />
                {label}
                <span className="visually-hidden">{status[key] ? " is up" : " is down"}</span>
              </li>
            ))}
          </>
        )}
      </ul>
      <div className="job-check">
        <Button size="sm" icon={Play} onClick={() => void onRunJob()} disabled={jobRunning}>
          {jobRunning ? "Running…" : "Run test job"}
        </Button>
        {jobResult && (
          <p className="job-result" role="status">
            {jobResult}
          </p>
        )}
      </div>
    </Popover>
  );
}
