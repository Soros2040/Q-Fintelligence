import path from "node:path";
import { describe, expect, it } from "vitest";

import { resolveProjectRoot } from "../../apps/control-plane/src/config.js";
import { getPortRegistry, RESERVED_PORTS } from "../../scripts/port-registry.mjs";

describe("runtime port boundary", () => {
  it("uses the dedicated q-fintelligence range", () => {
    expect(getPortRegistry({})).toEqual({
      web: 27_871,
      api: 27_872,
      workerHealth: 27_873,
      debug: 27_874,
      preview: 27_875,
    });
  });

  it("never overlaps the q-fintelligence reserved ports", () => {
    const qfPorts = Object.values(getPortRegistry({}));
    expect(qfPorts.some((port) => RESERVED_PORTS.includes(port))).toBe(false);
  });

  it("rejects local port drift instead of auto-selecting another port", () => {
    expect(() => getPortRegistry({ QF_WEB_PORT: "43187" })).toThrow(/fixed at 27871/);
    expect(() => getPortRegistry({ QF_API_PORT: "27899" })).toThrow(/fixed at 27872/);
  });

  it("resolves the repository root even when npm starts inside the workspace", () => {
    expect(resolveProjectRoot(path.join(process.cwd(), "apps", "control-plane", "src")))
      .toBe(process.cwd());
  });
});
