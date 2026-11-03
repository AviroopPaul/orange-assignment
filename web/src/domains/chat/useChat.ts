import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ApiError } from "../../lib/api";
import { USER_ID } from "../../lib/user";
import { confirmMemory, forgetMemory } from "../memory/api";
import type { MemoryItem } from "../memory/types";
import { answerApproval, eventsUrl, fetchBookingState, sendMessage } from "./api";
import {
  archiveChat,
  chatTitle,
  loadArchive,
  loadChat,
  saveArchive,
  saveChat,
  type ArchivedChat,
  type StoredChat,
} from "./chatStore";
import type { ApprovalCard, ChatMessage, NotificationItem, Outcome } from "./types";

// The browser reconnects by itself after a dropped stream, but not after a refused one.
const RECONNECT_MS = 3000;

interface ChatState extends StoredChat {
  archive: ArchivedChat[];
}

function initialState(): ChatState {
  const chat = loadChat();
  // A chat stored before the list existed joins it here.
  const archive = chat.sessionId && chat.messages.length > 0
    ? archiveChat(loadArchive(), chat.sessionId, chat.messages)
    : loadArchive();
  return { ...chat, archive };
}

export interface SendResult {
  sent: boolean;
}

function closeCards(messages: ChatMessage[]): ChatMessage[] {
  return messages.map((message) =>
    message.approvalOpen || message.memoryQuestions?.length
      ? { ...message, approvalOpen: false, memoryQuestions: [] }
      : message,
  );
}

function closeCardsById(messages: ChatMessage[], ids: string[]): ChatMessage[] {
  return messages.map((message) =>
    message.approvalOpen && message.approval && ids.includes(message.approval.approval_id)
      ? { ...message, approvalOpen: false }
      : message,
  );
}

/** News about the chat these messages belong to. A replay can repeat a row. */
function withNews(messages: ChatMessage[], item: NotificationItem): ChatMessage[] {
  const id = `notification-${item.id}`;
  if (messages.some((message) => message.id === id)) return messages;
  let next = item.data.approval ? closeCards(messages) : messages;
  if (item.data.closed_cards) next = closeCardsById(next, item.data.closed_cards);
  return [
    ...next,
    {
      id,
      role: "agent",
      text: item.text,
      runId: item.data.run_id,
      ...(item.data.approval ? { approval: item.data.approval, approvalOpen: true } : {}),
    },
  ];
}

/** Each chat keeps its own news. Only a provider's own cancel also reaches the open chat. */
function withNotifications(chat: ChatState, items: NotificationItem[]): ChatState {
  let { messages, archive } = chat;
  let last = chat.lastNotificationId ?? 0;
  for (const item of items) {
    if (item.id <= last) continue;
    last = item.id;
    if (item.session_id === chat.sessionId) {
      messages = withNews(messages, item);
      continue;
    }
    archive = archive.map((stored) => {
      if (stored.sessionId !== item.session_id) return stored;
      const updated = withNews(stored.messages, item);
      return updated === stored.messages
        ? stored
        : { ...stored, messages: updated, updatedAt: Date.now() };
    });
    const id = `notification-${item.id}`;
    if (item.data.proactive && !messages.some((message) => message.id === id)) {
      messages = [
        ...messages,
        {
          id,
          role: "agent",
          text: item.text,
          runId: item.data.run_id,
          otherSession: item.session_id,
        },
      ];
    }
  }
  return { ...chat, messages, archive, lastNotificationId: last };
}

export function useChat(onRunFinished: (runId: string) => void) {
  const [chat, setChat] = useState<ChatState>(initialState);
  const [sending, setSending] = useState(false);
  const [answering, setAnswering] = useState(false);
  // Bumped when a click in the chat changes a memory, so the panel reads again.
  const [memoryVersion, setMemoryVersion] = useState(0);
  const inFlight = useRef(false);
  const nextId = useRef(0);
  // News that arrives during a request waits for its reply, so the chat reads in order.
  const heldNews = useRef<NotificationItem[]>([]);
  const cursor = useRef(chat.lastNotificationId ?? 0);

  const { sessionId: activeSession, messages: activeMessages, lastNotificationId } = chat;
  useEffect(
    () =>
      saveChat({
        sessionId: activeSession,
        messages: activeMessages,
        lastNotificationId,
      }),
    [activeSession, activeMessages, lastNotificationId],
  );
  useEffect(() => saveArchive(chat.archive), [chat.archive]);

  // Every change goes through here, so the open chat is always in the list too.
  const update = useCallback((change: (current: ChatState) => ChatState) => {
    setChat((current) => {
      const next = change(current);
      return next.sessionId && next.messages.length > 0
        ? { ...next, archive: archiveChat(next.archive, next.sessionId, next.messages) }
        : next;
    });
  }, []);

  const makeId = () => `${Date.now()}-${nextId.current++}`;

  // Ask the server what is open. This page may have missed a card, or a booking may be running,
  // and the visible chat is only this browser's copy.
  const syncOpenCard = useCallback(
    (sessionId: string) => {
      void fetchBookingState(USER_ID, sessionId)
        .then(({ approval }) => {
          update((current) => {
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
            return { ...current, messages };
          });
        })
        .catch(() => {
          // The chat still works, and typing "yes" still answers an open card.
        });
    },
    [update],
  );

  const loadedSession = useRef(chat.sessionId);
  useEffect(() => {
    if (loadedSession.current) syncOpenCard(loadedSession.current);
  }, [syncOpenCard]);

  const requestDone = useCallback(() => {
    inFlight.current = false;
    const items = heldNews.current;
    heldNews.current = [];
    if (items.length > 0) update((current) => withNotifications(current, items));
  }, [update]);

  const addMessage = useCallback((message: ChatMessage, sessionId?: string) => {
    update((current) => ({
      ...current,
      sessionId: sessionId ?? current.sessionId,
      messages: [...current.messages, message],
    }));
  }, [update]);

  /** A reply from the server. Whatever it says, only its own card can still be answered. */
  const addReply = useCallback(
    (
      reply: {
        text: string;
        runId: string;
        outcome: Outcome;
        reason?: string | null;
        approval?: ApprovalCard | null;
        memory?: { saved: MemoryItem[]; to_confirm: MemoryItem[] };
      },
      sessionId: string,
    ) => {
      update((current) => ({
        ...current,
        sessionId,
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
            memorySaved: reply.memory?.saved ?? [],
            memoryQuestions: reply.memory?.to_confirm ?? [],
          },
        ],
      }));
    },
    [update],
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
            memory: response.memory,
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
        requestDone();
        setSending(false);
      }
    },
    [addMessage, addReply, chat.sessionId, onRunFinished, requestDone],
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
        requestDone();
        setAnswering(false);
      }
    },
    [addMessage, addReply, onRunFinished, requestDone],
  );

  const answerMemory = useCallback(
    async (item: MemoryItem, remember: boolean): Promise<void> => {
      if (inFlight.current) return;
      inFlight.current = true;
      setAnswering(true);
      try {
        const saved = remember
          ? await confirmMemory(item.id, USER_ID)
          : (await forgetMemory(item.id, USER_ID), null);
        update((current) => ({
          ...current,
          messages: current.messages.map((message) =>
            message.memoryQuestions?.some((question) => question.id === item.id)
              ? {
                  ...message,
                  memoryQuestions: message.memoryQuestions.filter(
                    (question) => question.id !== item.id,
                  ),
                  memorySaved: saved
                    ? [...(message.memorySaved ?? []), saved]
                    : message.memorySaved,
                }
              : message,
          ),
        }));
        setMemoryVersion((version) => version + 1);
      } catch (error) {
        const text =
          error instanceof ApiError ? error.message : "Something went wrong. Please try again.";
        addMessage({ id: makeId(), role: "error", text });
      } finally {
        requestDone();
        setAnswering(false);
      }
    },
    [addMessage, requestDone, update],
  );

  // Stored first on the server, so whatever this page misses comes back on the next connection.
  useEffect(() => {
    let source: EventSource | null = null;
    let retry: ReturnType<typeof setTimeout> | undefined;
    const connect = () => {
      source = new EventSource(eventsUrl(USER_ID, cursor.current));
      source.addEventListener("notification", (event) => {
        const item = JSON.parse((event as MessageEvent<string>).data) as NotificationItem;
        cursor.current = Math.max(cursor.current, item.id);
        if (inFlight.current) heldNews.current.push(item);
        else update((current) => withNotifications(current, [item]));
      });
      source.onerror = () => {
        if (source?.readyState === EventSource.CLOSED) retry = setTimeout(connect, RECONNECT_MS);
      };
    };
    connect();
    return () => {
      clearTimeout(retry);
      source?.close();
    };
  }, [update]);

  const openChat = useCallback(
    (sessionId: string) => {
      update((current) => {
        const stored = current.archive.find((other) => other.sessionId === sessionId);
        return {
          ...current,
          sessionId,
          // News about a chat this browser has no copy of is all there is to show.
          messages:
            stored?.messages ??
            current.messages
              .filter((message) => message.otherSession === sessionId)
              .map((message) => ({ ...message, otherSession: undefined })),
        };
      });
      syncOpenCard(sessionId);
    },
    [syncOpenCard, update],
  );

  const reset = useCallback(
    () => update((current) => ({ ...current, sessionId: null, messages: [] })),
    [update],
  );

  const chats = useMemo(
    () =>
      chat.archive.map((stored) => ({
        sessionId: stored.sessionId,
        title: chatTitle(stored.messages),
      })),
    [chat.archive],
  );

  return {
    messages: chat.messages,
    sessionId: chat.sessionId,
    sending: sending || answering,
    send,
    answer,
    answerMemory,
    memoryVersion,
    chats,
    title: chatTitle(chat.messages),
    openChat,
    reset,
  };
}
