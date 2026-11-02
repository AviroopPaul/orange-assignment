export type Outcome =
  | "done"
  | "needs_input"
  | "awaiting_approval"
  | "verifying"
  | "limit_reached"
  | "tool_failed";

export interface ChatResponse {
  run_id: string;
  session_id: string;
  outcome: Outcome;
  reason: string | null;
  reply: string;
}

export type MessageRole = "user" | "agent" | "error";

export interface ChatMessage {
  id: string;
  role: MessageRole;
  text: string;
  runId?: string;
  outcome?: Outcome;
  reason?: string | null;
}
