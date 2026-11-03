import { request } from "../../lib/api";
import type { MemoryItem } from "./types";

export async function fetchMemories(userId: string): Promise<MemoryItem[]> {
  const query = new URLSearchParams({ user_id: userId });
  const result = await request<{ memories: MemoryItem[] }>(`/memories?${query}`);
  return result.memories;
}

export function confirmMemory(id: string, userId: string): Promise<MemoryItem> {
  return request<MemoryItem>(`/memories/${id}/confirm`, {
    method: "POST",
    body: JSON.stringify({ user_id: userId }),
  });
}

export function correctMemory(id: string, userId: string, value: string): Promise<MemoryItem> {
  return request<MemoryItem>(`/memories/${id}`, {
    method: "PUT",
    body: JSON.stringify({ user_id: userId, value }),
  });
}

export async function forgetMemory(id: string, userId: string): Promise<void> {
  const query = new URLSearchParams({ user_id: userId });
  await request<void>(`/memories/${id}?${query}`, { method: "DELETE" });
}
