import { useCallback, useEffect, useState } from "react";

import { ApiError } from "../../lib/api";
import { USER_ID } from "../../lib/user";
import { confirmMemory, correctMemory, fetchMemories, forgetMemory } from "./api";
import type { MemoryItem } from "./types";

function sourceLabel(item: MemoryItem): string {
  if (!item.confirmed) return "Not confirmed, not used";
  return item.source === "explicit" ? "You said this" : "You confirmed this";
}

function errorText(error: unknown): string {
  return error instanceof ApiError ? error.message : "Could not reach the server.";
}

export function MemoryPanel({ refreshKey }: { refreshKey: string | null }) {
  const [memories, setMemories] = useState<MemoryItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setMemories(await fetchMemories(USER_ID));
      setError(null);
    } catch (reason) {
      setError(errorText(reason));
    }
  }, []);

  // A chat turn can add or forget a memory, so read again after each one.
  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  const act = async (action: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await action();
      setEditing(null);
      await load();
    } catch (reason) {
      setError(errorText(reason));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="panel memory" aria-label="Memory">
      <header className="panel-head">
        <h2>Memory</h2>
      </header>
      <div className="memory-body">
        {error && <p className="trace-error">{error}</p>}
        {memories?.length === 0 && (
          <p className="notice">
            Nothing saved yet. Tell the agent a lasting preference, such as your usual area.
          </p>
        )}
        <ul className="memory-list">
          {memories?.map((item) => (
            <li key={item.id} className={item.confirmed ? "memory-item" : "memory-item waiting"}>
              <div className="memory-key">
                {item.key}
                {item.scope !== "all" && <span className="memory-scope">{item.scope}</span>}
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
                  <button type="submit" className="button primary" disabled={busy}>
                    Save
                  </button>
                  <button
                    type="button"
                    className="button quiet"
                    onClick={() => setEditing(null)}
                  >
                    Cancel
                  </button>
                </form>
              ) : (
                <div className="memory-value">{item.value}</div>
              )}
              <div className="memory-source">{sourceLabel(item)}</div>
              {editing !== item.id && (
                <div className="memory-actions">
                  {!item.confirmed && (
                    <button
                      type="button"
                      className="button primary"
                      disabled={busy}
                      onClick={() => void act(() => confirmMemory(item.id, USER_ID))}
                    >
                      Confirm
                    </button>
                  )}
                  <button
                    type="button"
                    className="button quiet"
                    disabled={busy}
                    onClick={() => {
                      setDraft(item.value);
                      setEditing(item.id);
                    }}
                  >
                    Correct
                  </button>
                  <button
                    type="button"
                    className="button quiet"
                    disabled={busy}
                    onClick={() => void act(() => forgetMemory(item.id, USER_ID))}
                  >
                    Forget
                  </button>
                </div>
              )}
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}
