import { request } from "../../lib/api";
import type { ApprovalDecision, BookingState, ChatResponse } from "./types";

export function sendMessage(
  userId: string,
  sessionId: string | null,
  message: string,
): Promise<ChatResponse> {
  return request<ChatResponse>("/chat", {
    method: "POST",
    body: JSON.stringify({
      user_id: userId,
      message,
      ...(sessionId ? { session_id: sessionId } : {}),
    }),
  });
}

export function fetchBookingState(userId: string, sessionId: string): Promise<BookingState> {
  const query = new URLSearchParams({ user_id: userId, session_id: sessionId });
  return request<BookingState>(`/bookings/current?${query}`);
}

export function answerApproval(
  approvalId: string,
  userId: string,
  answer: "confirm" | "reject",
): Promise<ApprovalDecision> {
  return request<ApprovalDecision>(`/approvals/${approvalId}/${answer}`, {
    method: "POST",
    body: JSON.stringify({ user_id: userId }),
  });
}

export function eventsUrl(userId: string, after: number): string {
  const query = new URLSearchParams({ user_id: userId, after: String(after) });
  return `/api/events?${query}`;
}
