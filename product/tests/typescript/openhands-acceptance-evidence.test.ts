import { describe, expect, it } from "vitest";

import { analyzeAcceptanceEvidence } from "../../apps/control-plane/src/campaign/openhands-acceptance-evidence.js";

function completedTool(sequence: number, toolCallId: string, toolName: string, result = "{}") {
  return [
    {
      sequence,
      type: "tool.started",
      payload: { toolCallId, toolName, arguments: {} },
      createdAt: "2026-07-30T00:00:00.000Z",
    },
    {
      sequence: sequence + 1,
      type: "tool.completed",
      payload: { toolCallId, toolName, isError: false, result },
      createdAt: "2026-07-30T00:00:01.000Z",
    },
  ];
}

describe("OpenHands acceptance scientific evidence", () => {
  it("recognizes the sealed P06 controlled-circuit artifact schema", () => {
    const controlledHash = "c".repeat(64);
    const result = analyzeAcceptanceEvidence({
      events: [
        ...completedTool(1, "source", "list_project_sources"),
        ...completedTool(3, "fetch", "fetch_tushare_six_stock_bundle"),
        ...completedTool(5, "inspect", "inspect_validation_dataset"),
        ...completedTool(7, "classical", "execute_controlled_python"),
        ...completedTool(9, "controlled", "generate_controlled_qaoa_circuit", JSON.stringify({ artifactSha256: controlledHash })),
      ],
      artifacts: [{
        sha256: controlledHash,
        mediaType: "application/vnd.qf.p06-controlled-qaoa+json",
        producer: "qf-tool-host",
        parentHashes: [],
        content: {
          schema_version: "qf.p06.controlled-qaoa.v1",
          status: "COMPLETED",
          qcis: "X Q0",
          manifest: {
            backend: "tianyan176",
            qubits: 6,
            selected_count: 3,
            depth: 2,
            warm_start: "011010",
            local_preflight: { feasible_probability: 1 },
          },
        },
      }],
      baselineEventSequence: 0,
      hardwareStable: true,
      historyStable: true,
      formalTestSealed: true,
    });

    expect(result).toMatchObject({
      stages: { C: { complete: true, sequence: 10 } },
    });
  });
});
