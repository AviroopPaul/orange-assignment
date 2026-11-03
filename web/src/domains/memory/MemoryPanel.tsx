import { Brain, Check, Pencil, Trash2 } from "lucide-react";
import { useState } from "react";

import { Button, Chip, EmptyState } from "../../components/ui";
import { USER_ID } from "../../lib/user";
import { confirmMemory, correctMemory, forgetMemory } from "./api";
import type { MemoryItem } from "./types";
import { memoryErrorText, type useMemories } from "./useMemories";

function sourceLabel(item: MemoryItem): string {
  if (!item.confirmed) return "Not confirmed, not used";
  return item.source === "explicit" ? "You said this" : "You confirmed this";
}

export function MemoryPanel({ store }: { store: ReturnType<typeof useMemories> }) {
  const { memories, error, setError, load } = store;
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);

  const act = async (action: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await action();
      setEditing(null);
      await load();
    } catch (reason) {
      setError(memoryErrorText(reason));
    } finally {
      setBusy(false);
    }
  };

  const waiting = memories?.filter((item) => !item.confirmed) ?? [];
  const trusted = memories?.filter((item) => item.confirmed) ?? [];

  const renderItem = (item: MemoryItem) => (
    <li key={item.id} className={item.confirmed ? "memory-item" : "memory-item waiting"}>
      <div className="memory-key">
        {item.key.replaceAll("_", " ")}
        {item.scope !== "all" && <Chip>{item.scope}</Chip>}
      </div>
      {editing === item.id ? (
        <form
          className="memory-edit"
          onSubmit={(event) => {
            event.preventDefault();
            const value = draft.trim();
            if (value) void act(() => correctMemory(item.id, USER_ID, value));
          }}
        >
          <input
            aria-label={`New value for ${item.key}`}
            value={draft}
            maxLength={300}
            onChange={(event) => setDraft(event.target.value)}
          />
          <div className="memory-edit-actions">
            <Button size="sm" onClick={() => setEditing(null)}>
              Cancel
            </Button>
            <Button size="sm" type="submit" variant="primary" disabled={busy}>
              Save
            </Button>
          </div>
        </form>
      ) : (
        <div className="memory-value">{item.value}</div>
      )}
      {editing !== item.id && (
        <div className="memory-foot">
          <span className="memory-source">{sourceLabel(item)}</span>
          <div className="memory-actions">
            {!item.confirmed && (
              <Button
                size="sm"
                icon={Check}
                disabled={busy}
                onClick={() => void act(() => confirmMemory(item.id, USER_ID))}
              >
                Confirm
              </Button>
            )}
            <Button
              size="sm"
              variant="ghost"
              icon={Pencil}
              disabled={busy}
              onClick={() => {
                setDraft(item.value);
                setEditing(item.id);
              }}
            >
              Correct
            </Button>
            <Button
              size="sm"
              variant="ghost"
              icon={Trash2}
              disabled={busy}
              onClick={() => void act(() => forgetMemory(item.id, USER_ID))}
            >
              Forget
            </Button>
          </div>
        </div>
      )}
    </li>
  );

  return (
    <>
      {error && (
        <p className="panel-error" role="alert">
          {error}
        </p>
      )}
      {memories?.length === 0 && (
        <EmptyState icon={Brain}>
          Nothing saved yet. Tell the agent a lasting preference, such as your usual area.
        </EmptyState>
      )}
      {waiting.length > 0 && (
        <>
          <h3 className="memory-heading">Waiting for your answer ({waiting.length})</h3>
          <ul className="memory-list">{waiting.map(renderItem)}</ul>
        </>
      )}
      {trusted.length > 0 && (
        <>
          <h3 className="memory-heading">Used by the agent ({trusted.length})</h3>
          <ul className="memory-list">{trusted.map(renderItem)}</ul>
        </>
      )}
    </>
  );
}
