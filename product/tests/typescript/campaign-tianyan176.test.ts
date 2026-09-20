import { describe, expect, it } from "vitest";

import {
  findSixQubitCycle,
  remapSixQubits,
  representativeQgnnSubcircuit,
} from "../../apps/control-plane/src/campaign/tianyan176-hardware.js";

describe("tianyan176 physical mapping", () => {
  it("finds a deterministic six-qubit coupling cycle and remaps the QAOA ring", () => {
    const cycle = findSixQubitCycle({
      G0: ["Q0", "Q6"],
      G1: ["Q6", "Q12"],
      G2: ["Q12", "Q19"],
      G3: ["Q19", "Q13"],
      G4: ["Q13", "Q7"],
      G5: ["Q7", "Q0"],
    });
    expect(cycle).toEqual(["Q0", "Q6", "Q12", "Q19", "Q13", "Q7"]);
    expect(remapSixQubits("CZ Q0 Q1\nCZ Q5 Q0", cycle)).toBe("CZ Q0 Q6\nCZ Q7 Q0");
  });

  it("extracts a bounded Q0-Q1 message block from the parent QGNN circuit", () => {
    const subcircuit = representativeQgnnSubcircuit([
      "RY Q0 0.1",
      "RY Q1 0.2",
      "RY Q2 0.3",
      "Y2M Q0",
      "RZ Q0 1.0",
      "Y2P Q0",
      "CZ Q1 Q0",
      "Y2M Q0",
      "RZ Q0 2.0",
      "Y2P Q0",
      "CZ Q1 Q0",
      "RZ Q0 1.5",
      "Y2P Q0",
      "Y2M Q0",
      "CZ Q5 Q0",
    ].join("\n"));
    expect(subcircuit.match(/CZ Q1 Q0/gu)).toHaveLength(2);
    expect(subcircuit).not.toContain("Q2");
    expect(subcircuit).not.toContain("Q5");
    expect(subcircuit).toMatch(/M Q0\nM Q1$/u);
  });
});
