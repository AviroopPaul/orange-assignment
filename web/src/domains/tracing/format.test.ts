import { describe, expect, it } from "vitest";

import { formatCost, formatLatency, kindLabel, runDurationMs } from "./format";

describe("trace formatting", () => {
  it("formats latency in the unit people expect", () => {
    expect(formatLatency(null)).toBe("n/a");
    expect(formatLatency(842.4)).toBe("842 ms");
    expect(formatLatency(1500)).toBe("1.5 s");
  });

  it("shows small costs without rounding them to zero", () => {
    expect(formatCost(0.0001039)).toBe("$0.000104");
    expect(formatCost(null)).toBe("n/a");
  });

  it("measures a run only when it finished", () => {
    expect(runDurationMs("2026-11-02T12:00:00Z", "2026-11-02T12:00:02Z")).toBe(2000);
    expect(runDurationMs("2026-11-02T12:00:00Z", null)).toBeNull();
  });

  it("falls back to the raw kind for events it does not know", () => {
    expect(kindLabel("model_call")).toBe("Model call");
    expect(kindLabel("state_transition")).toBe("state_transition");
  });
});
