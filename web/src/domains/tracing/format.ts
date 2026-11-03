export function formatLatency(ms: number | null): string {
  if (ms === null) return "n/a";
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;
}

export function formatCost(usd: number | null): string {
  if (usd === null) return "n/a";
  return `$${usd.toFixed(6)}`;
}

export function runDurationMs(startedAt: string, finishedAt: string | null): number | null {
  if (!finishedAt) return null;
  return new Date(finishedAt).getTime() - new Date(startedAt).getTime();
}

const KIND_LABELS: Record<string, string> = {
  model_call: "Model call",
  model_error: "Model error",
  model_unavailable: "Model unavailable",
  tool_call: "Tool call",
  tool_refused: "Tool refused",
  tool_error: "Tool error",
  state_transition: "State change",
  dates_proposed: "Dates proposed",
  dates_confirmed: "Dates confirmed",
  dates_rejected: "Dates rejected",
  dates_unclear: "Answer not clear",
  model_rate_limited: "Rate limited",
  unexpected_error: "Unexpected error",
  outcome: "Outcome",
};

export function kindLabel(kind: string): string {
  return KIND_LABELS[kind] ?? kind;
}
