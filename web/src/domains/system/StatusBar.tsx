import { useCallback, useEffect, useState } from "react";

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

export function StatusBar() {
  const status = useSystemStatus();
  const [jobResult, setJobResult] = useState<string | null>(null);
  const [jobRunning, setJobRunning] = useState(false);

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
    <header className="status-bar">
      <h1>Errand Agent</h1>
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
        {jobResult && (
          <span className="job-result" role="status">
            {jobResult}
          </span>
        )}
        <button
          type="button"
          className="button quiet"
          onClick={() => void onRunJob()}
          disabled={jobRunning}
        >
          {jobRunning ? "Running…" : "Run test job"}
        </button>
      </div>
    </header>
  );
}
