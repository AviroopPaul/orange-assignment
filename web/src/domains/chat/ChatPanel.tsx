import {
  AlertCircle,
  ArrowUp,
  CalendarClock,
  ExternalLink,
  ListTree,
  Wrench,
  Zap,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";

import { BrandMark, Button, Chip } from "../../components/ui";
import { MemoryChatNote } from "../memory/MemoryChatNote";
import type { MemoryItem } from "../memory/types";
import { AgentReply } from "./AgentReply";
import { ApprovalCardView } from "./ApprovalCardView";
import type { ApprovalCard, ChatMessage, Outcome } from "./types";

const MAX_LENGTH = 4000;
const MAX_INPUT_PX = 220;

const EXAMPLES: { icon: LucideIcon; text: string }[] = [
  { icon: Wrench, text: "Find a plumber for my kitchen tap this week, mornings only, under ₹1,500." },
  { icon: CalendarClock, text: "Move my AC servicing to Saturday." },
  { icon: Zap, text: "Book the same electrician as last time, but not before 10 a.m." },
];

const OUTCOME_LABELS: Record<Outcome, string> = {
  done: "Done",
  needs_input: "Needs input",
  awaiting_approval: "Awaiting approval",
  verifying: "Verifying",
  limit_reached: "Limit reached",
  tool_failed: "Failed",
};

const OUTCOME_TONES: Partial<Record<Outcome, "accent" | "warn" | "danger">> = {
  awaiting_approval: "accent",
  verifying: "accent",
  limit_reached: "warn",
  tool_failed: "danger",
};

// Normal ends of a turn. Asking the user a question is not worth flagging.
const QUIET_OUTCOMES = new Set<Outcome>(["done", "needs_input"]);

interface ChatPanelProps {
  messages: ChatMessage[];
  sending: boolean;
  selectedRunId: string | null;
  onSend: (text: string) => Promise<{ sent: boolean }>;
  onAnswer: (card: ApprovalCard, choice: "confirm" | "reject") => void;
  onAnswerMemory: (item: MemoryItem, remember: boolean) => void;
  onOpenOtherChat: (sessionId: string) => void;
  onSelectRun: (runId: string) => void;
}

export function ChatPanel({
  messages,
  sending,
  selectedRunId,
  onSend,
  onAnswer,
  onAnswerMemory,
  onOpenOtherChat,
  onSelectRun,
}: ChatPanelProps) {
  const [draft, setDraft] = useState("");
  const endRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const isEmpty = messages.length === 0;

  useEffect(() => {
    endRef.current?.scrollIntoView?.({ block: "end" });
  }, [messages.length, sending]);

  useEffect(() => {
    if (isEmpty) inputRef.current?.focus();
  }, [isEmpty]);

  useEffect(() => {
    const input = inputRef.current;
    if (!input) return;
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, MAX_INPUT_PX)}px`;
  }, [draft]);

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
    <section className={`chat ${isEmpty ? "chat-empty" : ""}`} aria-label="Chat">
      {isEmpty ? (
        <div className="hero">
          <BrandMark />
          <h2>What needs doing at home?</h2>
          <p>
            The agent can search and compare providers, show your bookings, and book, cancel or
            move a booking once you confirm it. It also remembers lasting preferences, which you
            can check under Memory.
          </p>
        </div>
      ) : (
        <div className="messages" aria-live="polite">
          <div className="messages-column">
            {messages.map((message) => (
              <MessageView
                key={message.id}
                message={message}
                sending={sending}
                selected={message.runId === selectedRunId}
                onAnswer={onAnswer}
                onAnswerMemory={onAnswerMemory}
                onOpenOtherChat={onOpenOtherChat}
                onSelectRun={onSelectRun}
              />
            ))}
            {sending && (
              <div className="thinking" role="status">
                <span className="thinking-dot" aria-hidden="true" />
                <span>Thinking…</span>
              </div>
            )}
            <div ref={endRef} />
          </div>
        </div>
      )}

      <div className="composer-wrap">
        <form className="composer" onSubmit={onSubmit}>
          <label htmlFor="chat-input" className="visually-hidden">
            Your message
          </label>
          <textarea
            id="chat-input"
            ref={inputRef}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={onKeyDown}
            placeholder={isEmpty ? "Describe an errand" : "Reply to the agent"}
            maxLength={MAX_LENGTH}
            rows={1}
          />
          <div className="composer-foot">
            <span>Shift + Enter for a new line</span>
            <button
              type="submit"
              className="send"
              aria-label="Send"
              title="Send"
              disabled={sending || !draft.trim()}
            >
              <ArrowUp size={17} strokeWidth={2.4} aria-hidden="true" />
            </button>
          </div>
        </form>
        {!isEmpty && <p className="composer-note">Nothing is booked until you confirm it.</p>}
      </div>

      {isEmpty && (
        <div className="examples">
          <h3>Try one of these</h3>
          {EXAMPLES.map(({ icon: Icon, text }) => (
            <button
              type="button"
              key={text}
              className="example"
              onClick={() => {
                setDraft(text);
                inputRef.current?.focus();
              }}
            >
              <Icon size={15} aria-hidden="true" />
              {text}
            </button>
          ))}
        </div>
      )}
    </section>
  );
}

interface MessageViewProps {
  message: ChatMessage;
  sending: boolean;
  selected: boolean;
  onAnswer: ChatPanelProps["onAnswer"];
  onAnswerMemory: ChatPanelProps["onAnswerMemory"];
  onOpenOtherChat: ChatPanelProps["onOpenOtherChat"];
  onSelectRun: ChatPanelProps["onSelectRun"];
}

function MessageView({
  message,
  sending,
  selected,
  onAnswer,
  onAnswerMemory,
  onOpenOtherChat,
  onSelectRun,
}: MessageViewProps) {
  if (message.role === "user") {
    return (
      <article className="message user">
        <p>{message.text}</p>
      </article>
    );
  }
  if (message.role === "error") {
    return (
      <article className="message error">
        <AlertCircle size={16} aria-hidden="true" />
        <p>{message.text}</p>
      </article>
    );
  }
  const flagged = message.outcome && !QUIET_OUTCOMES.has(message.outcome) ? message.outcome : null;
  return (
    <article className="message agent">
      <AgentReply text={message.text} />
      {message.otherSession && (
        <Button
          size="sm"
          icon={ExternalLink}
          disabled={sending}
          onClick={() => onOpenOtherChat(message.otherSession as string)}
        >
          Open that chat
        </Button>
      )}
      {message.approval && (
        <ApprovalCardView
          card={message.approval}
          open={message.approvalOpen === true}
          busy={sending}
          onAnswer={onAnswer}
        />
      )}
      <MemoryChatNote
        saved={message.memorySaved ?? []}
        questions={message.memoryQuestions ?? []}
        busy={sending}
        onAnswer={onAnswerMemory}
      />
      {(flagged || message.runId) && (
        <div className="message-foot">
          {flagged && <Chip tone={OUTCOME_TONES[flagged]}>{OUTCOME_LABELS[flagged]}</Chip>}
          {message.runId && (
            <button
              type="button"
              className={`run-link ${selected ? "selected" : ""}`}
              aria-pressed={selected}
              onClick={() => onSelectRun(message.runId as string)}
            >
              <ListTree size={13} aria-hidden="true" />
              View trace
            </button>
          )}
        </div>
      )}
    </article>
  );
}
