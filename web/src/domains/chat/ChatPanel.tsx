import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";

import { ApprovalCardView } from "./ApprovalCardView";
import type { ApprovalCard, ChatMessage, Outcome } from "./types";

const MAX_LENGTH = 4000;

const EXAMPLES = [
  "Find a plumber for my kitchen tap this week, mornings only, under ₹1,500.",
  "Move my AC servicing to Saturday.",
  "Book the same electrician as last time, but not before 10 a.m.",
];

const OUTCOME_LABELS: Record<Outcome, string> = {
  done: "Done",
  needs_input: "Needs input",
  awaiting_approval: "Awaiting approval",
  verifying: "Verifying",
  limit_reached: "Limit reached",
  tool_failed: "Failed",
};

// Normal ends of a turn. Asking the user a question is not worth flagging.
const QUIET_OUTCOMES = new Set<Outcome>(["done", "needs_input"]);

interface ChatPanelProps {
  messages: ChatMessage[];
  sending: boolean;
  sessionId: string | null;
  selectedRunId: string | null;
  onSend: (text: string) => Promise<{ sent: boolean }>;
  onAnswer: (card: ApprovalCard, choice: "confirm" | "reject") => void;
  onNewChat: () => void;
  onSelectRun: (runId: string) => void;
}

export function ChatPanel({
  messages,
  sending,
  sessionId,
  selectedRunId,
  onSend,
  onAnswer,
  onNewChat,
  onSelectRun,
}: ChatPanelProps) {
  const [draft, setDraft] = useState("");
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView?.({ block: "end" });
  }, [messages.length, sending]);

  const submit = async () => {
    const text = draft;
    if (!text.trim() || sending) return;
    setDraft("");
    const { sent } = await onSend(text);
    // Keep the words if they never reached the agent, so nothing has to be retyped.
    if (!sent) setDraft((current) => current || text);
  };

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    void submit();
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void submit();
    }
  };

  return (
    <section className="panel chat" aria-label="Chat">
      <header className="panel-head">
        <h2>Chat</h2>
        <span className="session-id" title={sessionId ?? undefined}>
          {sessionId ? `Session ${sessionId.slice(0, 8)}` : "New session"}
        </span>
        <button
          type="button"
          className="button quiet"
          onClick={onNewChat}
          disabled={sending || messages.length === 0}
        >
          New chat
        </button>
      </header>

      <div className="messages" aria-live="polite">
        {messages.length === 0 && (
          <div className="empty">
            <p className="empty-title">Tell the agent what you need done at home.</p>
            <p className="notice">
              The agent can search and compare providers, show your bookings and book a slot once
              you confirm it. Cancelling, rescheduling and memory are not connected yet.
            </p>
            <p className="examples-label">Try one of these</p>
            <div className="examples">
              {EXAMPLES.map((example) => (
                <button
                  type="button"
                  key={example}
                  className="example"
                  onClick={() => setDraft(example)}
                >
                  {example}
                </button>
              ))}
            </div>
          </div>
        )}

        {messages.map((message) => (
          <article key={message.id} className={`message ${message.role}`}>
            <p>{message.text}</p>
            {message.approval && (
              <ApprovalCardView
                card={message.approval}
                open={message.approvalOpen === true}
                busy={sending}
                onAnswer={onAnswer}
              />
            )}
            {message.role === "agent" && message.runId && (
              <button
                type="button"
                className={`run-link ${message.runId === selectedRunId ? "selected" : ""}`}
                aria-pressed={message.runId === selectedRunId}
                onClick={() => onSelectRun(message.runId as string)}
              >
                {message.outcome && !QUIET_OUTCOMES.has(message.outcome) && (
                  <span className={`chip outcome-${message.outcome}`}>
                    {OUTCOME_LABELS[message.outcome]}
                  </span>
                )}
                View trace
              </button>
            )}
          </article>
        ))}

        {sending && (
          <div className="message agent thinking" role="status">
            <p>Thinking…</p>
          </div>
        )}
        <div ref={endRef} />
      </div>

      <form className="composer" onSubmit={onSubmit}>
        <label htmlFor="chat-input" className="visually-hidden">
          Your message
        </label>
        <textarea
          id="chat-input"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Type a message. Press Enter to send, Shift+Enter for a new line."
          maxLength={MAX_LENGTH}
          rows={2}
        />
        <button type="submit" className="button primary" disabled={sending || !draft.trim()}>
          Send
        </button>
      </form>
    </section>
  );
}
