export interface TraceEvent {
  seq: number;
  kind: string;
  name: string | null;
  data: Record<string, unknown>;
  latency_ms: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  estimated_cost_usd: number | null;
  created_at: string;
}

export interface RunTrace {
  id: string;
  user_id: string;
  session_id: string;
  outcome: string | null;
  reason: string | null;
  reply: string | null;
  model_calls: number;
  input_tokens: number;
  output_tokens: number;
  estimated_cost_usd: number;
  started_at: string;
  finished_at: string | null;
  events: TraceEvent[];
}
