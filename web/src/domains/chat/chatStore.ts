import { USER_ID } from "../../lib/user";
import type { ChatMessage } from "./types";

// The server has no history endpoint yet, so this browser keeps the visible chats.
// The model reads the conversation from the server, in `turn_view`.
const SUFFIX = USER_ID === "demo-user" ? "" : `.${USER_ID}`;
const CHAT_KEY = `errand.chat.v1${SUFFIX}`;
const ARCHIVE_KEY = `errand.chats.v1${SUFFIX}`;
const MAX_CHATS = 50;
const NEW_CHAT_TITLE = "New chat";

export interface StoredChat {
  sessionId: string | null;
  messages: ChatMessage[];
  // Survives a new chat, so old news is not shown again.
  lastNotificationId?: number;
}

export interface ArchivedChat {
  sessionId: string;
  updatedAt: number;
  messages: ChatMessage[];
}

function read<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    if (raw) return JSON.parse(raw) as T;
  } catch {
    // Blocked or corrupt storage just means starting empty.
  }
  return fallback;
}

function write(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // The chat still works for this page load.
  }
}

export const loadChat = () => read<StoredChat>(CHAT_KEY, { sessionId: null, messages: [] });
export const saveChat = (chat: StoredChat) => write(CHAT_KEY, chat);
export const loadArchive = () => read<ArchivedChat[]>(ARCHIVE_KEY, []);
export const saveArchive = (archive: ArchivedChat[]) => write(ARCHIVE_KEY, archive);

/** Only a new message moves a chat up the list. Closing a card does not. */
export function archiveChat(
  archive: ArchivedChat[],
  sessionId: string,
  messages: ChatMessage[],
): ArchivedChat[] {
  const stored = archive.find((chat) => chat.sessionId === sessionId);
  if (stored?.messages === messages) return archive;
  const grew = !stored || messages.length > stored.messages.length;
  const updated = { sessionId, messages, updatedAt: grew ? Date.now() : stored.updatedAt };
  return [updated, ...archive.filter((chat) => chat.sessionId !== sessionId)]
    .sort((a, b) => b.updatedAt - a.updatedAt)
    .slice(0, MAX_CHATS);
}

export function chatTitle(messages: ChatMessage[]): string {
  const first = messages.find((message) => message.role === "user") ?? messages[0];
  return first?.text.split("\n")[0].slice(0, 80) || NEW_CHAT_TITLE;
}
