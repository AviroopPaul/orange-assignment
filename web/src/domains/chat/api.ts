import { request } from "../../lib/api";
import type { ChatResponse } from "./types";

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
