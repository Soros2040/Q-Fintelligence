import { describe, expect, it } from "vitest";

import { p15BatchWithoutQueryIdsMustRemainQueryOnly } from "../../apps/control-plane/src/p15/p15-orchestrator.js";

describe("P15 hardware submission recovery", () => {
  it("never resubmits COMMITTING or UNKNOWN batches that lack Query IDs", () => {
    expect(p15BatchWithoutQueryIdsMustRemainQueryOnly("COMMITTING")).toBe(true);
    expect(p15BatchWithoutQueryIdsMustRemainQueryOnly("UNKNOWN")).toBe(true);
  });

  it("allows only never-attempted PREPARED batches to enter the submission path", () => {
    expect(p15BatchWithoutQueryIdsMustRemainQueryOnly("PREPARED")).toBe(false);
  });
});
