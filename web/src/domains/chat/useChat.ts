import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "../../lib/api";
import { answerApproval, fetchBookingState, fetchNotifications, sendMessage } from "./api";
import type { ApprovalCard, ChatMessage, Outcome } from "./types";

// The server has no history endpoint yet, so this browser keeps the visible chat.
// The model's own memory of the conversation lives on the server, in the ADK session.
const STORAGE_KEY = "errand.chat.v1";
const USER_ID = "demo-user";
const POLL_MS = 2000;
// The saga said it is still trying, so more news will come.
const STILL_RUNNING = "booking_not_confirmed";

interface StoredChat {
  sessionId: string | null;
  messages: ChatMessage[];
  // A booking is being carried out, so news can arrive without a user turn.
  watching?: boolean;
  lastNotificationId?: number;
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

function closeCards(messages: ChatMessage[]): ChatMessage[] {
  return messages.map((message) =>
    message.approvalOpen ? { ...message, approvalOpen: false } : message,
  );
}

export function useChat(onRunFinished: (runId: string) => void) {
  const [chat, setChat] = useState<StoredChat>(loadChat);
  const [sending, setSending] = useState(false);
  const [answering, setAnswering] = useState(false);
  const inFlight = useRef(false);
  const nextId = useRef(0);

  useEffect(() => saveChat(chat), [chat]);

  const makeId = () => `${Date.now()}-${nextId.current++}`;

  // On load, ask the server what is open. This page may have missed a card, or a booking may be
  // running, and the visible chat is only this browser's copy.
  const loadedSession = useRef(chat.sessionId);
  useEffect(() => {
    const sessionId = loadedSession.current;
    if (!sessionId) return;
    void fetchBookingState(USER_ID, sessionId)
      .then(({ approval, attempt_open: attemptOpen }) => {
        setChat((current) => {
          if (current.sessionId !== sessionId) return current;
          const shown = current.messages.some(
            (message) =>
              message.approvalOpen && message.approval?.approval_id === approval?.approval_id,
          );
          const messages = !approval
            ? closeCards(current.messages)
            : shown
              ? current.messages
              : [
                  ...closeCards(current.messages),
                  {
                    id: `card-${approval.approval_id}`,
                    role: "agent" as const,
                    text: "This booking is still waiting for your answer.",
                    outcome: "awaiting_approval" as const,
                    approval,
                    approvalOpen: true,
                  },
                ];
          return { ...current, messages, watching: current.watching || attemptOpen };
        });
      })
      .catch(() => {
        // The chat still works, and typing "yes" still answers an open card.
      });
  }, []);

  const addMessage = useCallback((message: ChatMessage, sessionId?: string) => {
    setChat((current) => ({
      ...current,
      sessionId: sessionId ?? current.sessionId,
      messages: [...current.messages, message],
    }));
  }, []);

  /** A reply from the server. Whatever it says, only its own card can still be answered. */
  const addReply = useCallback(
    (
      reply: {
        text: string;
        runId: string;
        outcome: Outcome;
        reason?: string | null;
        approval?: ApprovalCard | null;
      },
      sessionId: string,
    ) => {
      setChat((current) => ({
        ...current,
        sessionId,
        watching: current.watching || reply.outcome === "verifying",
        messages: [
          ...closeCards(current.messages),
          {
            id: makeId(),
            role: "agent",
            text: reply.text,
            runId: reply.runId,
            outcome: reply.outcome,
            reason: reply.reason,
            ...(reply.approval ? { approval: reply.approval, approvalOpen: true } : {}),
          },
        ],
      }));
    },
    [],
  );

  const send = useCallback(
    async (text: string): Promise<SendResult> => {
      const trimmed = text.trim();
      if (!trimmed || inFlight.current) return { sent: false };

      inFlight.current = true;
      setSending(true);
      addMessage({ id: makeId(), role: "user", text: trimmed });
      try {
        const response = await sendMessage(USER_ID, chat.sessionId, trimmed);
        addReply(
          {
            text: response.reply,
            runId: response.run_id,
            outcome: response.outcome,
            reason: response.reason,
            approval: response.approval,
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
    [addMessage, addReply, chat.sessionId, onRunFinished],
  );

  const answer = useCallback(
    async (card: ApprovalCard, choice: "confirm" | "reject"): Promise<void> => {
      if (inFlight.current) return;
      inFlight.current = true;
      setAnswering(true);
      try {
        const decision = await answerApproval(card.approval_id, USER_ID, choice);
        addReply(
          {
            text: decision.reply,
            runId: decision.run_id,
            outcome: decision.outcome,
            approval: decision.approval,
          },
          decision.session_id,
        );
        onRunFinished(decision.run_id);
      } catch (error) {
        const text =
          error instanceof ApiError ? error.message : "Something went wrong. Please try again.";
        addMessage({ id: makeId(), role: "error", text });
      } finally {
        inFlight.current = false;
        setAnswering(false);
      }
    },
    [addMessage, addReply, onRunFinished],
  );

  const { watching, sessionId, lastNotificationId } = chat;
  useEffect(() => {
    if (!watching || !sessionId) return;
    let stopped = false;
    const timer = setInterval(() => {
      void fetchNotifications(USER_ID, sessionId, lastNotificationId ?? 0)
        .then((items) => {
          if (stopped || items.length === 0) return;
          const last = items[items.length - 1];
          setChat((current) => ({
            ...current,
            lastNotificationId: last.id,
            watching: last.kind === STILL_RUNNING,
            messages: [
              ...(items.some((item) => item.data.approval)
                ? closeCards(current.messages)
                : current.messages),
              ...items.map(
                (item): ChatMessage => ({
                  id: `notification-${item.id}`,
                  role: "agent",
                  text: item.text,
                  runId: item.data.run_id,
                  ...(item.data.approval
                    ? { approval: item.data.approval, approvalOpen: true }
                    : {}),
                }),
              ),
            ],
          }));
        })
        .catch(() => {
          // The next tick tries again. The booking does not depend on this page.
        });
    }, POLL_MS);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [watching, sessionId, lastNotificationId]);

  const reset = useCallback(() => setChat({ sessionId: null, messages: [] }), []);

  return {
    messages: chat.messages,
    sessionId: chat.sessionId,
    sending: sending || answering,
    send,
    answer,
    reset,
  };
}
