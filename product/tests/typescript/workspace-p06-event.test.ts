import type { AgentUiEvent, ConversationSnapshot } from "@q-fintelligence/contracts";
import { CONTRACT_SCHEMA_VERSION, toAgentWorkspaceEvent } from "@q-fintelligence/contracts";
import { describe, expect, it } from "vitest";

import {
  compactWorkspaceEvents,
  currentStage,
  elapsedLabel,
  mergeWorkspaceEvent,
  resultKind,
  runPresentation,
  runSurfaceLabel,
  shouldRefreshConversationSummary,
} from "../../apps/web/src/workspace-model.js";

function event(
  sequence: number,
  type: AgentUiEvent["type"],
  payload: AgentUiEvent["payload"] = {},
): AgentUiEvent {
  return {
    schemaVersion: CONTRACT_SCHEMA_VERSION,
    eventId: `event_${sequence}`,
    conversationId: "conversation-p06",
    taskId: "task-p06",
    runId: "run-p06",
    sequence,
    persistent: true,
    type,
    payload,
    createdAt: `2026-07-22T10:00:${String(sequence).padStart(2, "0")}.000Z`,
  };
}

describe("P06 workspace event projection", () => {
  it("keeps complete detail references while exposing a concise central summary", () => {
    const source = event(3, "hardware.batch", {
      stage: "等待真机终态",
      queryId: "QID-P06-001",
      resultArtifactSha256: "a".repeat(64),
      stdout: "full raw result remains in the right inspector",
    });
    const projected = toAgentWorkspaceEvent(source);
    expect(projected.kind).toBe("agent_activity");
    expect(projected.stage).toBe("等待真机终态");
    expect(projected.detailRefs.queryId).toBe("QID-P06-001");
    expect(projected.detailRefs.artifactHashes).toEqual(["a".repeat(64)]);
    expect(projected.sensitivity).toBe("INTERNAL");
  });

  it("marks model requests as redacted-sensitive without losing the event locator", () => {
    const projected = toAgentWorkspaceEvent(event(4, "model.request", {
      provider: "openai",
      modelId: "getoken/gpt-5.6-sol",
      authorization: "Bearer secret",
    }));
    expect(projected.sensitivity).toBe("SENSITIVE_REDACTED");
    expect(projected.detailRefs.eventId).toBe("event_4");
  });

  it("replaces heartbeat noise with one live status row and deduplicates replay", () => {
    const started = event(1, "run.started");
    const heartbeatA = event(2, "run.heartbeat", { summary: "等待模型 5 秒" });
    const heartbeatB = event(3, "run.heartbeat", { summary: "等待模型 10 秒" });
    const merged = compactWorkspaceEvents([started, heartbeatA, heartbeatB, heartbeatB]);
    expect(merged.filter((item) => item.type === "run.heartbeat")).toEqual([heartbeatB]);
    expect(mergeWorkspaceEvent(merged, heartbeatB)).toBe(merged);
  });

  it("maps rich results, stage and elapsed time deterministically", () => {
    const source = event(2, "artifact.created", { resultKind: "quantum_circuit", stage: "受控 QAOA" });
    const snapshot = {
      conversation: { state: "RUNNING" },
      steps: [],
    } as unknown as ConversationSnapshot;
    expect(resultKind(source)).toBe("quantum_circuit");
    expect(currentStage(snapshot, [source])).toBe("受控 QAOA");
    expect(elapsedLabel("2026-07-22T10:00:00.000Z", Date.parse("2026-07-22T10:01:05.000Z"))).toBe("01:05");
  });

  it("does not start the run timer for a newly created empty conversation", () => {
    const snapshot = {
      conversation: { state: "DRAFT", createdAt: "2026-07-22T10:00:00.000Z" },
      messages: [],
      steps: [],
    } as unknown as ConversationSnapshot;
    expect(runPresentation(snapshot, [])).toEqual({
      hasStarted: false,
      startedAt: null,
      label: "等待任务",
      ticking: false,
    });
  });

  it("starts timing from the first real run event instead of conversation creation", () => {
    const started = event(5, "run.started");
    const snapshot = {
      conversation: { state: "RUNNING", createdAt: "2026-07-22T09:00:00.000Z" },
      messages: [],
      steps: [],
    } as unknown as ConversationSnapshot;
    expect(runPresentation(snapshot, [started])).toEqual({
      hasStarted: true,
      startedAt: started.createdAt,
      label: "正在处理",
      ticking: true,
    });
  });

  it("keeps the existing run surface but labels Chat turns truthfully and refreshes terminal summaries", () => {
    const chatStarted = event(6, "run.started", { promptIntent: "CHAT" });
    const workStarted = event(7, "run.started", { promptIntent: "WORK" });
    expect(runSurfaceLabel([chatStarted])).toBe("CHAT TURN");
    expect(runSurfaceLabel([workStarted])).toBe("WORK RUN");
    expect(shouldRefreshConversationSummary(event(8, "run.completed"))).toBe(true);
    expect(shouldRefreshConversationSummary(event(9, "artifact.created"))).toBe(false);
  });
});
