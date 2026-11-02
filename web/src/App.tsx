import { useCallback, useState } from "react";

import { ChatPanel } from "./domains/chat/ChatPanel";
import { useChat } from "./domains/chat/useChat";
import { StatusBar } from "./domains/system/StatusBar";
import { TracePanel } from "./domains/tracing/TracePanel";

export function App() {
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const onRunFinished = useCallback((runId: string) => setSelectedRunId(runId), []);
  const chat = useChat(onRunFinished);

  // After a reload, show the latest run instead of an empty trace panel.
  const latestRunId = [...chat.messages].reverse().find((message) => message.runId)?.runId ?? null;
  const shownRunId = selectedRunId ?? latestRunId;

  const onNewChat = () => {
    chat.reset();
    setSelectedRunId(null);
  };

  return (
    <div className="app">
      <StatusBar />
      <main className="workspace">
        <ChatPanel
          messages={chat.messages}
          sending={chat.sending}
          sessionId={chat.sessionId}
          selectedRunId={shownRunId}
          onSend={chat.send}
          onNewChat={onNewChat}
          onSelectRun={setSelectedRunId}
        />
        <TracePanel runId={shownRunId} />
      </main>
    </div>
  );
}
