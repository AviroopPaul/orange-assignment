import { Brain, ListTree, Menu, type LucideIcon } from "lucide-react";
import { useCallback, useState } from "react";

import { IconButton, SidePanel } from "./components/ui";
import { ChatPanel } from "./domains/chat/ChatPanel";
import { ChatSidebar } from "./domains/chat/ChatSidebar";
import { useChat } from "./domains/chat/useChat";
import { MemoryPanel } from "./domains/memory/MemoryPanel";
import { useMemories } from "./domains/memory/useMemories";
import { StatusMenu } from "./domains/system/StatusMenu";
import { TracePanel } from "./domains/tracing/TracePanel";

type PanelName = "trace" | "memory";

const NARROW_SCREEN = "(max-width: 820px)";
const isNarrow = () => window.matchMedia?.(NARROW_SCREEN).matches === true;

interface PanelToggleProps {
  label: string;
  icon: LucideIcon;
  open: boolean;
  count?: number;
  attention?: boolean;
  onToggle: () => void;
}

function PanelToggle({ label, icon: Icon, open, count, attention, onToggle }: PanelToggleProps) {
  return (
    <button
      type="button"
      className={`panel-toggle ${open ? "open" : ""}`}
      aria-label={label}
      aria-expanded={open}
      onClick={onToggle}
    >
      <Icon size={15} aria-hidden="true" />
      <span className="panel-toggle-label">{label}</span>
      {count !== undefined && count > 0 && (
        <span className={`count ${attention ? "attention" : ""}`}>{count}</span>
      )}
    </button>
  );
}

export function App() {
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [panel, setPanel] = useState<PanelName | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(() => !isNarrow());
  const onRunFinished = useCallback((runId: string) => setSelectedRunId(runId), []);
  const chat = useChat(onRunFinished);

  // After a reload, show the latest run instead of an empty trace panel.
  const latestRunId = [...chat.messages].reverse().find((message) => message.runId)?.runId ?? null;
  const shownRunId = selectedRunId ?? latestRunId;
  const memories = useMemories(`${latestRunId}:${chat.memoryVersion}`);
  const waitingMemories = memories.memories?.some((item) => !item.confirmed) === true;

  const leaveChat = () => {
    setSelectedRunId(null);
    if (isNarrow()) setSidebarOpen(false);
  };
  const onNewChat = () => {
    chat.reset();
    leaveChat();
  };
  const onOpenChat = (sessionId: string) => {
    if (sessionId !== chat.sessionId) chat.openChat(sessionId);
    leaveChat();
  };
  const togglePanel = (name: PanelName) =>
    setPanel((current) => (current === name ? null : name));

  return (
    <div className={`app ${sidebarOpen ? "" : "sidebar-closed"}`}>
      {sidebarOpen && (
        <>
          <ChatSidebar
            chats={chat.chats}
            activeSession={chat.sessionId}
            busy={chat.sending}
            canStartNew={chat.messages.length > 0}
            onNewChat={onNewChat}
            onOpenChat={onOpenChat}
            onClose={() => setSidebarOpen(false)}
          />
          <div className="scrim" aria-hidden="true" onClick={() => setSidebarOpen(false)} />
        </>
      )}

      <div className="main">
        <header className="topbar">
          {!sidebarOpen && (
            <IconButton label="Open sidebar" icon={Menu} onClick={() => setSidebarOpen(true)} />
          )}
          <h1 className="chat-title">{chat.title}</h1>
          {chat.sessionId && (
            <span className="session-id" title={chat.sessionId}>
              Session {chat.sessionId.slice(0, 8)}
            </span>
          )}
          <div className="topbar-actions">
            <StatusMenu />
            <PanelToggle
              label="Trace"
              icon={ListTree}
              open={panel === "trace"}
              onToggle={() => togglePanel("trace")}
            />
            <PanelToggle
              label="Memory"
              icon={Brain}
              open={panel === "memory"}
              count={memories.memories?.length}
              attention={waitingMemories}
              onToggle={() => togglePanel("memory")}
            />
          </div>
        </header>

        <main className="workspace">
          <ChatPanel
            messages={chat.messages}
            sending={chat.sending}
            selectedRunId={panel === "trace" ? shownRunId : null}
            onSend={chat.send}
            onAnswer={(card, choice) => void chat.answer(card, choice)}
            onAnswerMemory={(item, remember) => void chat.answerMemory(item, remember)}
            onOpenOtherChat={onOpenChat}
            onSelectRun={(runId) => {
              setSelectedRunId(runId);
              setPanel("trace");
            }}
          />
          {panel === "trace" && (
            <SidePanel
              title="Run trace"
              subtitle="What the agent did for this turn"
              onClose={() => setPanel(null)}
            >
              <TracePanel runId={shownRunId} />
            </SidePanel>
          )}
          {panel === "memory" && (
            <SidePanel
              title="Memory"
              subtitle="What the agent knows about you"
              onClose={() => setPanel(null)}
            >
              <MemoryPanel store={memories} />
            </SidePanel>
          )}
        </main>
      </div>
    </div>
  );
}
