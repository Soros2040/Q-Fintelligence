import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import {
  mapOpenHandsEvent,
  OPENHANDS_ADAPTER_VERSION,
  OPENHANDS_PROTOCOL_VERSION,
  OPENHANDS_SDK_VERSION,
  OPENHANDS_WHEEL_SHA256,
  type SidecarFrame,
} from "../../apps/control-plane/src/agent/openhands-runtime.js";
import { ProviderReadinessError, ProviderRegistry } from "../../apps/control-plane/src/agent/providers.js";
import {
  assertExactToolNames,
  buildNoiseAwareLocalSimulationRequest,
  buildP06TianyanSubmitRequest,
  createQfToolSpecs,
  QF_TOOL_NAMES,
  QfToolHost,
  validateQfToolArguments,
} from "../../apps/control-plane/src/agent/tools.js";
import { storeArtifact } from "../../apps/control-plane/src/artifact-store.js";
import { loadRuntimeConfig } from "../../apps/control-plane/src/config.js";
import { openDatabase } from "../../apps/control-plane/src/db/migrations.js";
import { WorkspaceRepository } from "../../apps/control-plane/src/db/repository.js";

const temporaryRoots: string[] = [];

afterEach(async () => {
  await Promise.all(temporaryRoots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
});

describe("OpenHands product boundary", () => {
  it("simulates the six-qubit logical circuit before physical TianYan mapping", () => {
    const logicalQcis = "H Q0\nCZ Q0 Q1\nM Q0\nM Q1";
    expect(buildNoiseAwareLocalSimulationRequest(logicalQcis)).toEqual({
      action: "local_simulate",
      qcis: logicalQcis,
      shots: 4096,
      seed: 20260725,
    });
    expect(() => buildNoiseAwareLocalSimulationRequest("   ")).toThrow("requires logical QCIS");
  });

  it("binds the P06 TianYan child request to the current authorization basis", () => {
    expect(buildP06TianyanSubmitRequest({
      approvalHash: "a".repeat(64),
      authorizationBasis: "P17_CURRENT_EXECUTION_AUTHORIZATION",
      qcis: "M Q0",
    })).toMatchObject({
      action: "submit",
      approval_hash: "a".repeat(64),
      authorization_basis: "P17_CURRENT_EXECUTION_AUTHORIZATION",
      machine_name: "tianyan176",
      shots: 100,
      qcis: "M Q0",
    });
  });

  it("maps only legacy runtime names one-way to OpenHands in production", () => {
    const previousAgentMode = process.env.QF_AGENT_MODE;
    const previousNodeEnv = process.env.NODE_ENV;
    try {
      process.env.NODE_ENV = "production";
      for (const legacyMode of ["pi", "harness"]) {
        process.env.QF_AGENT_MODE = legacyMode;
        expect(loadRuntimeConfig().agentMode).toBe("openhands");
      }
      process.env.QF_AGENT_MODE = "hybrid";
      expect(loadRuntimeConfig().agentMode).toBe("hybrid");
      process.env.QF_AGENT_MODE = "unknown-runtime";
      expect(() => loadRuntimeConfig()).toThrow(/must be mock, openhands, or hybrid/);
    } finally {
      if (previousAgentMode === undefined) delete process.env.QF_AGENT_MODE;
      else process.env.QF_AGENT_MODE = previousAgentMode;
      if (previousNodeEnv === undefined) delete process.env.NODE_ENV;
      else process.env.NODE_ENV = previousNodeEnv;
    }
  });

  it("maps isolated OpenHands events into the shared AgentUiEvent vocabulary", () => {
    expect(mapOpenHandsEvent({
      protocol: OPENHANDS_PROTOCOL_VERSION,
      type: "event",
      eventId: "oh-event-1",
      eventType: "agent.started",
      payload: {},
    } as SidecarFrame)).toEqual({
      type: "agent.started",
      payload: { backendEventId: "oh-event-1" },
      persistent: true,
    });
    expect(mapOpenHandsEvent({
      protocol: OPENHANDS_PROTOCOL_VERSION,
      type: "event",
      eventId: "token-1",
      eventType: "assistant.delta",
      payload: { text: "hello" },
    } as SidecarFrame)).toEqual({
      type: "assistant.delta",
      payload: { text: "hello" },
      persistent: false,
    });
    expect(mapOpenHandsEvent({
      protocol: OPENHANDS_PROTOCOL_VERSION,
      type: "event",
      eventId: "message-1",
      eventType: "assistant.delta",
      payload: {
        text: "persisted assistant message",
        backendEventId: "message-1",
        streamMode: "persisted-message",
      },
    } as SidecarFrame)).toEqual({
      type: "assistant.delta",
      payload: {
        text: "persisted assistant message",
        backendEventId: "message-1",
        streamMode: "persisted-message",
      },
      persistent: true,
    });
    const lifecycleCases: Array<[string, string]> = [
      ["agent.completed", "agent.completed"],
      ["agent.aborted", "agent.aborted"],
      ["turn.started", "turn.started"],
      ["turn.completed", "turn.completed"],
      ["tool.started", "tool.started"],
      ["tool.progress", "tool.progress"],
      ["tool.completed", "tool.completed"],
      ["queue.changed", "queue.changed"],
      ["run.failed", "run.failed"],
    ];
    for (const [eventType, expected] of lifecycleCases) {
      expect(mapOpenHandsEvent({
        protocol: OPENHANDS_PROTOCOL_VERSION,
        type: "event",
        eventId: `event-${eventType}`,
        eventType,
        payload: {},
      } as SidecarFrame)?.type).toBe(expected);
    }
    expect(OPENHANDS_SDK_VERSION).toBe("1.39.0+qf.noobservability.1");
    expect(OPENHANDS_ADAPTER_VERSION).toBe("qf-openhands-adapter.v2");
    expect(OPENHANDS_WHEEL_SHA256).toMatch(/^[a-f0-9]{64}$/u);
  });

  it("exports only canonical QF specs and revalidates the complete original schema", async () => {
    const directory = await mkdtemp(path.join(os.tmpdir(), "qf-openhands-boundary-"));
    temporaryRoots.push(directory);
    const opened = openDatabase(path.join(directory, "state.sqlite3"), path.resolve("infra/sqlite"));
    const repository = new WorkspaceRepository(opened.database);
    const project = repository.createProject("OpenHands boundary");
    const conversation = repository.createConversation({
      projectId: project.projectId,
      title: "isolated",
      mode: "OPENHANDS",
      provider: "deepseek",
      modelId: "deepseek-v4-pro",
    });
    const host = new QfToolHost(repository, conversation.conversationId, path.join(directory, "artifacts"));
    const specs = createQfToolSpecs(host);
    expect(specs.map((spec) => spec.name)).toEqual(QF_TOOL_NAMES);
    const validateSpec = specs.find((spec) => spec.name === "validate_task_spec")!;
    expect(validateQfToolArguments(validateSpec, {
      scientificQuestion: "Is the fixture valid?",
      asOfDate: "2026-07-29",
    })).toMatchObject({ asOfDate: "2026-07-29" });
    expect(() => validateQfToolArguments(validateSpec, {
      scientificQuestion: "",
      asOfDate: "not-a-date",
      extra: true,
    })).toThrow(/canonical schema/);
    expect(() => assertExactToolNames([...QF_TOOL_NAMES, "bash"])).toThrow(/boundary mismatch/);
    repository.close();
  });

  it("rejects unsupported and unconfigured providers without choosing a fallback", () => {
    const registry = new ProviderRegistry();
    expect(() => registry.runtimeConfiguration("not-configured")).toThrow(ProviderReadinessError);
    const previousKey = process.env.OPENAI_API_KEY;
    const previousBaseUrl = process.env.OPENAI_BASE_URL;
    delete process.env.OPENAI_API_KEY;
    delete process.env.OPENAI_BASE_URL;
    try {
      expect(() => registry.runtimeConfiguration("openai")).toThrow(ProviderReadinessError);
    } finally {
      if (previousKey !== undefined) process.env.OPENAI_API_KEY = previousKey;
      if (previousBaseUrl !== undefined) process.env.OPENAI_BASE_URL = previousBaseUrl;
    }
  });

  it("allows the P06 science host to read only registered QF JSON media types", async () => {
    const directory = await mkdtemp(path.join(os.tmpdir(), "qf-p06-tool-boundary-"));
    temporaryRoots.push(directory);
    const opened = openDatabase(path.join(directory, "state.sqlite3"), path.resolve("infra/sqlite"));
    const repository = new WorkspaceRepository(opened.database);
    const project = repository.createProject("P06 tool boundary");
    const conversation = repository.createConversation({
      projectId: project.projectId,
      title: "registered validation data",
      mode: "OPENHANDS",
      provider: "deepseek",
      modelId: "deepseek-v4-pro",
    });
    const artifactRoot = path.join(directory, "artifacts");
    const payload = {
      parsed: {
        content: {
          third_party_validator: "pandera==0.32.1",
          rows: 2,
          columns: ["asset", "return"],
          missing: { asset: 0, return: 0 },
          duplicate_rows: 0,
          dtypes: { asset: "object", return: "float64" },
          preview: [{ asset: "600036.SH", return: 0.01 }],
        },
      },
    };
    const manifest = await storeArtifact({
      root: artifactRoot,
      data: new TextEncoder().encode(JSON.stringify(payload)),
      mediaType: "application/vnd.qf.validation-data+json",
      producer: "test.p06-upload",
    });
    repository.registerArtifact(conversation.conversationId, manifest);
    const host = new QfToolHost(repository, conversation.conversationId, artifactRoot, process.cwd());
    const result = await host.execute("inspect_validation_dataset", { artifactSha256: manifest.sha256 });
    expect(result.data.quality_gate).toBe("PASS");
    expect(result.artifact?.mediaType).toBe("application/vnd.qf.data-quality+json");
    const hardwareResult = await storeArtifact({
      root: artifactRoot,
      data: new TextEncoder().encode(JSON.stringify({
        machine_name: "tianyan176",
        query_id: "query-test",
        result: [{ probability: "{\"011010\":1.0}" }],
      })),
      mediaType: "application/vnd.qf.tianyan-result+json",
      producer: "test.p06-hardware-result",
    });
    repository.registerArtifact(conversation.conversationId, hardwareResult);
    expect((await host.execute("read_artifact_excerpt", { sha256: hardwareResult.sha256 })).data.excerpt)
      .toContain("\"query_id\":\"query-test\"");
    repository.close();
  }, 20_000);

  it("binds a P06 hardware approval to the exact circuit artifact hash", async () => {
    const directory = await mkdtemp(path.join(os.tmpdir(), "qf-p06-approval-boundary-"));
    temporaryRoots.push(directory);
    const opened = openDatabase(path.join(directory, "state.sqlite3"), path.resolve("infra/sqlite"));
    const repository = new WorkspaceRepository(opened.database);
    const project = repository.createProject("P06 approval boundary");
    const conversation = repository.createConversation({
      projectId: project.projectId,
      title: "exact circuit approval",
      mode: "OPENHANDS",
      provider: "deepseek",
      modelId: "deepseek-v4-pro",
    });
    const artifactRoot = path.join(directory, "artifacts");
    const manifest = await storeArtifact({
      root: artifactRoot,
      data: new TextEncoder().encode(JSON.stringify({ qcis: "M Q0", manifest: {} })),
      mediaType: "application/vnd.qf.quantum-circuit+json",
      producer: "test.p06-circuit",
    });
    repository.registerArtifact(conversation.conversationId, manifest);
    const approval = repository.createApprovalRequest({
      conversationId: conversation.conversationId,
      action: "SUBMIT_HARDWARE",
      subjectHash: "f".repeat(64),
      rationale: "mismatched artifact must not authorize submission",
    });
    repository.decideApproval(approval.approvalId, "APPROVE", "HUMAN");
    const host = new QfToolHost(
      repository,
      conversation.conversationId,
      artifactRoot,
      process.cwd(),
      undefined,
      undefined,
      undefined,
      {
        mode: "ONE_JOB",
        target: "tianyan176",
        maxNewHardwareJobs: 1,
        shotsPerJob: 100,
        authorizationBasis: "TEST_CURRENT_P06_AUTHORIZATION",
      },
    );
    await expect(host.execute("submit_tianyan176_100_shot", {
      authorizationBasis: "TEST_CURRENT_P06_AUTHORIZATION",
      circuitArtifactSha256: manifest.sha256,
    })).rejects.toThrow(/matching approved front-end/);
    repository.close();
  });
});
