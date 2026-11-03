import type { MemoryItem } from "../memory/types";

export type Outcome =
  "done" | "needs_input" | "awaiting_approval" | "verifying" | "limit_reached" | "tool_failed";

export interface ApprovalCard {
  approval_id: string;
  action: string;
  provider_name: string;
  service: string;
  slot: string;
  slot_label: string;
  old_slot_label?: string | null;
  price: number;
  currency: string;
  notes: string[];
  expires_at: string;
  status: string;
}

export interface ApprovalDecision {
  status: string;
  outcome: Outcome;
  reply: string;
  run_id: string;
  session_id: string;
  approval: ApprovalCard | null;
}

export interface BookingState {
  approval: ApprovalCard | null;
  attempt_open: boolean;
}

export interface NotificationItem {
  id: number;
  session_id: string;
  kind: string;
  text: string;
  data: {
    run_id?: string;
    approval?: ApprovalCard;
    // The user must hear this even in another chat, such as a provider's own cancel.
    proactive?: boolean;
    closed_cards?: string[];
  };
  created_at: string;
}

export interface ChatResponse {
  run_id: string;
  session_id: string;
  outcome: Outcome;
  reason: string | null;
  reply: string;
  approval?: ApprovalCard | null;
  memory?: { saved: MemoryItem[]; to_confirm: MemoryItem[] };
}

export type MessageRole = "user" | "agent" | "error";

export interface ChatMessage {
  // News about another chat. The user can open that chat to answer it.
  otherSession?: string;
  id: string;
  role: MessageRole;
  text: string;
  runId?: string;
  outcome?: Outcome;
  reason?: string | null;
  approval?: ApprovalCard;
  // A card can be answered only while it is the latest question.
  approvalOpen?: boolean;
  memorySaved?: MemoryItem[];
  // Shown only until the user's next message, like the server's own question.
  memoryQuestions?: MemoryItem[];
}
