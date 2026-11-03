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
              Remembered {item.key.replaceAll("_", " ")}: {item.value}
            </li>
          ))}
        </ul>
      )}
      {questions.map((item) => (
        <section key={item.id} className="memory-question" aria-label="Remember this?">
          <h3>Remember this?</h3>
          <p>{item.value}</p>
          <div className="approval-actions">
            <button
              type="button"
              className="button primary"
              disabled={busy}
              onClick={() => onAnswer(item, true)}
            >
              Remember
            </button>
            <button
              type="button"
              className="button quiet"
              disabled={busy}
              onClick={() => onAnswer(item, false)}
            >
              Don't remember
            </button>
          </div>
        </section>
      ))}
    </>
  );
}
