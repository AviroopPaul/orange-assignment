import { PanelLeftClose, Plus } from "lucide-react";

import { BrandMark, Button, IconButton } from "../../components/ui";
import { USER_ID } from "../../lib/user";

interface ChatSidebarProps {
  chats: { sessionId: string; title: string }[];
  activeSession: string | null;
  // A reply belongs to the chat that asked, so nothing switches while one is on its way.
  busy: boolean;
  canStartNew: boolean;
  onNewChat: () => void;
  onOpenChat: (sessionId: string) => void;
  onClose: () => void;
}

export function ChatSidebar({
  chats,
  activeSession,
  busy,
  canStartNew,
  onNewChat,
  onOpenChat,
  onClose,
}: ChatSidebarProps) {
  return (
    <aside className="sidebar">
      <div className="sidebar-brand">
        <BrandMark />
        <span>Errand Agent</span>
        <IconButton label="Close sidebar" icon={PanelLeftClose} onClick={onClose} />
      </div>

      <Button
        icon={Plus}
        className="new-chat"
        onClick={onNewChat}
        disabled={busy || !canStartNew}
      >
        New chat
      </Button>

      <nav className="recents" aria-label="Previous chats">
        <h2>Recents</h2>
        {chats.length === 0 && <p className="recents-empty">Your chats will appear here.</p>}
        <ul>
          {chats.map((chat) => (
            <li key={chat.sessionId}>
              <button
                type="button"
                className="recent"
                aria-current={chat.sessionId === activeSession ? "page" : undefined}
                disabled={busy}
                title={chat.title}
                onClick={() => onOpenChat(chat.sessionId)}
              >
                {chat.title}
              </button>
            </li>
          ))}
        </ul>
      </nav>

      <div className="sidebar-user">
        <span className="avatar" aria-hidden="true">
          {USER_ID.slice(0, 1).toUpperCase()}
        </span>
        <div>
          <strong>{USER_ID}</strong>
          <span>Demo account</span>
        </div>
      </div>
    </aside>
  );
}
