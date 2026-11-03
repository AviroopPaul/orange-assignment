import { Brain, CircleCheck } from "lucide-react";

import { Button } from "../../components/ui";
import type { MemoryItem } from "./types";

interface MemoryChatNoteProps {
  saved: MemoryItem[];
  questions: MemoryItem[];
  busy: boolean;
  onAnswer: (item: MemoryItem, remember: boolean) => void;
}

/** Every value here comes from the server's memory row, never from the model's reply. */
export function MemoryChatNote({ saved, questions, busy, onAnswer }: MemoryChatNoteProps) {
  return (
    <>
      {saved.length > 0 && (
        <ul className="memory-saved" aria-label="Saved to memory">
          {saved.map((item) => (
            <li key={item.id}>
              <CircleCheck size={14} aria-hidden="true" />
              Remembered {item.key.replaceAll("_", " ")}: {item.value}
            </li>
          ))}
        </ul>
      )}
      {questions.map((item) => (
        <section key={item.id} className="memory-question" aria-label="Remember this?">
          <header>
            <Brain size={15} aria-hidden="true" />
            <h3>Remember this?</h3>
          </header>
          <p>{item.value}</p>
          <div className="approval-actions">
            <Button size="sm" disabled={busy} onClick={() => onAnswer(item, false)}>
              Don't remember
            </Button>
            <Button
              size="sm"
              variant="primary"
              disabled={busy}
              onClick={() => onAnswer(item, true)}
            >
              Remember
            </Button>
          </div>
        </section>
      ))}
    </>
  );
}
