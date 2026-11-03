import { request } from "../../lib/api";
import type { ApprovalDecision, BookingState, ChatResponse, NotificationItem } from "./types";

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

export async function fetchNotifications(
  userId: string,
  sessionId: string,
  after: number,
): Promise<NotificationItem[]> {
  const query = new URLSearchParams({
    user_id: userId,
    session_id: sessionId,
    after: String(after),
  });
  const result = await request<{ notifications: NotificationItem[] }>(`/notifications?${query}`);
  return result.notifications;
}
