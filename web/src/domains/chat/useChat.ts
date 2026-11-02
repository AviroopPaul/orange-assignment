import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "../../lib/api";
import { sendMessage } from "./api";
import type { ChatMessage } from "./types";

// The server has no history endpoint yet, so this browser keeps the visible chat.
// The model's own memory of the conversation lives on the server, in the ADK session.
const STORAGE_KEY = "errand.chat.v1";
const USER_ID = "demo-user";

interface StoredChat {
  sessionId: string | null;
  messages: ChatMessage[];
}

function loadChat(): StoredChat {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) return JSON.parse(raw) as StoredChat;
  } catch {
    // Blocked or corrupt storage just means a fresh chat.
  }
  return { sessionId: null, messages: [] };
}

function saveChat(chat: StoredChat): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(chat));
  } catch {
    // The chat still works for this page load.
  }
}

export interface SendResult {
  sent: boolean;
}

export function useChat(onRunFinished: (runId: string) => void) {
  const [chat, setChat] = useState<StoredChat>(loadChat);
  const [sending, setSending] = useState(false);
  const inFlight = useRef(false);
  const nextId = useRef(0);

  useEffect(() => saveChat(chat), [chat]);

  const makeId = () => `${Date.now()}-${nextId.current++}`;

  const addMessage = useCallback((message: ChatMessage, sessionId?: string) => {
    setChat((current) => ({
      sessionId: sessionId ?? current.sessionId,
      messages: [...current.messages, message],
    }));
  }, []);

  const send = useCallback(
    async (text: string): Promise<SendResult> => {
      const trimmed = text.trim();
      if (!trimmed || inFlight.current) return { sent: false };

      inFlight.current = true;
      setSending(true);
      addMessage({ id: makeId(), role: "user", text: trimmed });
      try {
        const response = await sendMessage(USER_ID, chat.sessionId, trimmed);
        addMessage(
          {
            id: makeId(),
            role: "agent",
            text: response.reply,
            runId: response.run_id,
            outcome: response.outcome,
            reason: response.reason,
          },
          response.session_id,
        );
        onRunFinished(response.run_id);
        return { sent: true };
      } catch (error) {
        const text =
          error instanceof ApiError ? error.message : "Something went wrong. Please try again.";
        addMessage({ id: makeId(), role: "error", text });
        return { sent: false };
      } finally {
        inFlight.current = false;
        setSending(false);
      }
    },
    [addMessage, chat.sessionId, onRunFinished],
  );

  const reset = useCallback(() => setChat({ sessionId: null, messages: [] }), []);

  return { messages: chat.messages, sessionId: chat.sessionId, sending, send, reset };
}
