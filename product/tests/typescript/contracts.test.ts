import { readFile } from "node:fs/promises";

import Ajv2020 from "ajv/dist/2020.js";
import addFormats from "ajv-formats";
import { describe, expect, it } from "vitest";

import { CONTRACT_SCHEMA_VERSION, RUNTIME_MODES, toAgentWorkspaceEvent } from "../../packages/contracts/src/index.js";

const schemaUrl = new URL("../../packages/contracts/schemas/protocol-spec.schema.json", import.meta.url);
const schema = JSON.parse(await readFile(schemaUrl, "utf8"));
const ajv = new Ajv2020({ allErrors: true, strict: true });
addFormats(ajv);
const validate = ajv.compile(schema);
const workspaceSchemaUrl = new URL("../../packages/contracts/schemas/agent-workspace.schema.json", import.meta.url);
const workspaceSchema = JSON.parse(await readFile(workspaceSchemaUrl, "utf8"));
const validateWorkspace = ajv.compile(workspaceSchema);

const validProtocol = {
  schemaVersion: "qf.v1",
  protocolId: "protocol-demo-001",
  taskId: "task-demo-001",
  asOfDate: "2024-12-31",
  universe: {
    assetCount: 6,
    selectionPeriod: "2019",
    selectionRule: "historical-hs300-cross-industry-liquidity",
  },
  split: {
    strategy: "time",
    train: { start: "2020-01-01", end: "2022-12-31" },
    validation: { start: "2023-01-01", end: "2023-12-31" },
    test: { start: "2024-01-01", end: "2024-12-31" },
  },
  label: {
    name: "future_5d_downside_realized_risk",
    horizonTradingDays: 5,
    nonNegative: true,
  },
  riskGraph: {
    method: "generalized-fevd",
    varOrder: 1,
    windowTradingDays: 120,
    regularization: "ridge",
    directed: true,
  },
  portfolio: { assetCount: 6, selectedCount: 3, weighting: "equal" },
  researchIterationLimit: 5,
  testPolicy: "sealed-single-execution",
};

describe("ProtocolSpec qf.v1", () => {
  it("accepts the frozen Day 1-15 baseline", () => {
    expect(validate(validProtocol), JSON.stringify(validate.errors)).toBe(true);
  });

  it("rejects random splitting", () => {
    expect(validate({ ...validProtocol, split: { ...validProtocol.split, strategy: "random" } })).toBe(false);
  });

  it("rejects a portfolio that is not 6 choose 3", () => {
    expect(validate({ ...validProtocol, portfolio: { ...validProtocol.portfolio, selectedCount: 4 } })).toBe(false);
  });
});

describe("Agent workspace qf.v1", () => {
  it("accepts a persisted ProjectSummary", () => {
    expect(validateWorkspace({
      schemaVersion: "qf.v1",
      projectId: "project-demo",
      name: "六股票风险图原型",
      description: "P0",
      archived: false,
      createdAt: "2026-07-21T03:00:00.000Z",
      updatedAt: "2026-07-21T03:00:00.000Z",
    }), JSON.stringify(validateWorkspace.errors)).toBe(true);
  });

  it("creates only Mock or OpenHands conversations while retaining Pi summary compatibility", () => {
    expect(RUNTIME_MODES).toEqual(["MOCK", "OPENHANDS", "PI"]);
    expect(validateWorkspace({ title: "real", mode: "OPENHANDS", provider: "deepseek" })).toBe(false);
    expect(validateWorkspace({ title: "real", mode: "OPENHANDS", provider: "deepseek", modelId: "deepseek-v4-pro" })).toBe(true);
    expect(validateWorkspace({ title: "legacy", mode: "PI", provider: "deepseek", modelId: "deepseek-v4-pro" })).toBe(false);
    expect(validateWorkspace({
      schemaVersion: "qf.v1",
      conversationId: "legacy-pi",
      projectId: "project-demo",
      taskId: "task-demo",
      title: "Legacy Pi",
      mode: "PI",
      state: "COMPLETED",
      provider: "deepseek",
      modelId: "deepseek-v4-pro",
      archived: true,
      lastActivityAt: "2026-07-21T03:00:00.000Z",
      createdAt: "2026-07-21T03:00:00.000Z",
    })).toBe(true);
  });

  it("validates OpenHands runtime revisions and durable tool-call inbox entries", () => {
    expect(validateWorkspace({
      schemaVersion: "qf.v1",
      sessionId: "openhands-conversation-1",
      runtimeKind: "OPENHANDS",
      runtimeRevision: "openhands-sdk==1.39.0+qf.noobservability.1",
      configHash: "a".repeat(64),
      recoveryCursor: 7,
      provider: "deepseek",
      modelId: "deepseek-v4-pro",
      promptVersion: "qf-openhands.v1",
      promptHash: "b".repeat(64),
      lifecycleState: "READY",
      relativeSessionFile: "openhands/conversation-1",
    }), JSON.stringify(validateWorkspace.errors)).toBe(true);
    expect(validateWorkspace({
      schemaVersion: "qf.v1",
      conversationId: "conversation-openhands",
      runtimeSessionId: "openhands-conversation-1",
      toolCallId: "tool-call-1",
      toolName: "get_task_context",
      requestHash: "c".repeat(64),
      status: "COMPLETED",
      result: { formalTest: "SEALED" },
      error: null,
      claimedAt: "2026-07-29T00:00:00.000Z",
      finishedAt: "2026-07-29T00:00:01.000Z",
      updatedAt: "2026-07-29T00:00:01.000Z",
    }), JSON.stringify(validateWorkspace.errors)).toBe(true);
  });

  it("accepts only the two human-selectable conversation model pairs", () => {
    expect(validateWorkspace({ provider: "openai", modelId: "gpt-5.6", actor: "HUMAN" })).toBe(true);
    expect(validateWorkspace({ provider: "deepseek", modelId: "deepseek-v4-pro", actor: "HUMAN" })).toBe(true);
    expect(validateWorkspace({ provider: "openai", modelId: "deepseek-v4-pro", actor: "HUMAN" })).toBe(false);
    expect(validateWorkspace({ provider: "deepseek", modelId: "deepseek-v3", actor: "HUMAN" })).toBe(false);
  });

  it("accepts an AgentUiEvent carrying the P06 central-workspace projection", () => {
    const source = {
      schemaVersion: CONTRACT_SCHEMA_VERSION,
      eventId: "event-p06-1",
      conversationId: "conversation-p06",
      taskId: "task-p06",
      runId: "run-p06",
      sequence: 1,
      persistent: true,
      type: "run.heartbeat" as const,
      payload: { summary: "等待固定 Provider 返回" },
      createdAt: "2026-07-22T10:00:00.000Z",
    };
    expect(validateWorkspace({ ...source, workspace: toAgentWorkspaceEvent(source) }), JSON.stringify(validateWorkspace.errors)).toBe(true);
  });
});
