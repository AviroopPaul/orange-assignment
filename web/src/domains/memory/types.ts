export interface MemoryItem {
  id: string;
  key: string;
  value: string;
  scope: string;
  source: "explicit" | "inferred";
  quote: string | null;
  confirmed: boolean;
  updated_at: string;
}
