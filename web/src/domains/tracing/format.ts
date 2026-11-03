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
  dates_saved: "Dates saved",
  approval_created: "Approval created",
  approval_answer: "User answered",
  approval_consumed: "Approval consumed",
  approval_rejected: "Approval rejected",
  approval_voided: "Approval voided",
  approval_expired: "Approval expired",
  approval_mismatch: "Approval mismatch",
  saga_claimed: "Saga started",
  booking_status_checked: "Status checked",
  booking_verified: "Booking verified",
  booking_not_confirmed: "Not confirmed yet",
  booking_slot_taken: "Slot taken",
  booking_hold_expired: "Hold expired",
  booking_price_changed: "Price changed",
  booking_failed: "Booking failed",
  booking_cancelled: "Booking cancelled",
  booking_rescheduled: "Booking moved",
  reschedule_conflict: "Slot conflict",
  change_failed: "Change failed",
  false_claim_blocked: "False claim blocked",
  model_rate_limited: "Rate limited",
  unexpected_error: "Unexpected error",
  outcome: "Outcome",
};

export function kindLabel(kind: string): string {
  return KIND_LABELS[kind] ?? kind;
}
