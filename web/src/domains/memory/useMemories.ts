import { useCallback, useEffect, useState } from "react";

import { ApiError } from "../../lib/api";
import { USER_ID } from "../../lib/user";
import { fetchMemories } from "./api";
import type { MemoryItem } from "./types";

export function memoryErrorText(error: unknown): string {
  return error instanceof ApiError ? error.message : "Could not reach the server.";
}

export function useMemories(refreshKey: string) {
  const [memories, setMemories] = useState<MemoryItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setMemories(await fetchMemories(USER_ID));
      setError(null);
    } catch (reason) {
      setError(memoryErrorText(reason));
    }
  }, []);

  // A chat turn can add or forget a memory, so read again after each one.
  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  return { memories, error, setError, load };
}
