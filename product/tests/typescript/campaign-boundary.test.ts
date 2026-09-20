import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { P02_AUTHORIZATION, assertStandingAuthorization, authorizationHash } from "../../apps/control-plane/src/campaign/authorization.js";
import type { AgentRuntime } from "../../apps/control-plane/src/agent/runtime.js";
import { ensureP02Campaign } from "../../apps/control-plane/src/campaign/bootstrap.js";
import { CampaignRepository } from "../../apps/control-plane/src/campaign/repository.js";
import {
  campaignRoleToolExecutor,
  campaignRoleToolSpecs,
  probeCampaignRoleSession,
} from "../../apps/control-plane/src/campaign/role-session.js";
import { assertCampaignRoleToolNames, campaignRoleDefinitions } from "../../apps/control-plane/src/campaign/roles.js";
import { openDatabase } from "../../apps/control-plane/src/db/migrations.js";
import { WorkspaceRepository } from "../../apps/control-plane/src/db/repository.js";

const temporaryRoots: string[] = [];

afterEach(async () => {
  await Promise.all(temporaryRoots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
});

async function repositories(): Promise<{
  workspace: WorkspaceRepository;
  campaign: CampaignRepository;
}> {
  const root = await mkdtemp(path.join(os.tmpdir(), "qf-campaign-test-"));
  temporaryRoots.push(root);
  const opened = openDatabase(path.join(root, "campaign.sqlite3"), path.resolve("infra/sqlite"));
  return {
    workspace: new WorkspaceRepository(opened.database),
    campaign: new CampaignRepository(opened.database),
  };
}

describe("P02 standing authorization and role isolation", () => {
  it("locks the provider, formal-test seal, hardware limits, and Git boundary", () => {
    expect(authorizationHash()).toMatch(/^[a-f0-9]{64}$/u);
    expect(() => assertStandingAuthorization(P02_AUTHORIZATION)).not.toThrow();
    expect(() => assertStandingAuthorization({ ...P02_AUTHORIZATION, formalTestSealed: false })).toThrow(/formal test/i);
    expect(() => assertStandingAuthorization({ ...P02_AUTHORIZATION, maxHardwareJobs: 3 })).toThrow(/hardware/i);
    expect(() => assertStandingAuthorization({ ...P02_AUTHORIZATION, authorizeGitCommit: true })).toThrow(/Git/i);
  });

  it("gives all six roles exact and distinct tool allowlists", () => {
    const definitions = campaignRoleDefinitions();
    expect(definitions).toHaveLength(6);
    const allTools = definitions.flatMap((definition) => [...definition.toolNames]);
    expect(new Set(allTools).size).toBe(allTools.length);
    for (const definition of definitions) {
      expect(() => assertCampaignRoleToolNames(definition.role, definition.toolNames)).not.toThrow();
      expect(() => assertCampaignRoleToolNames(definition.role, [...definition.toolNames, "unregistered_tool"])).toThrow(/boundary/i);
    }
  });

  it("probes a role only through the framework-neutral runtime boundary", async () => {
    const definition = campaignRoleDefinitions()[0]!;
    const specs = campaignRoleToolSpecs(definition);
    expect(specs.map((spec) => spec.name)).toEqual(definition.toolNames);
    const controller = new AbortController();
    await expect(campaignRoleToolExecutor(definition)(definition.toolNames[0]!, {}, {
      runtimeSessionId: "openhands-role-session",
      toolCallId: "role-probe-tool",
      signal: controller.signal,
    })).resolves.toEqual({ status: "READ_ONLY_PROBE", role: definition.role, tool: definition.toolNames[0] });
    await expect(campaignRoleToolExecutor(definition)("unregistered_tool", {}, {
      runtimeSessionId: "openhands-role-session",
      toolCallId: "role-probe-tool-rejected",
      signal: controller.signal,
    })).rejects.toThrow(/out-of-scope tool/u);
    let disposed = false;
    const runtime: AgentRuntime = {
      kind: "OPENHANDS",
      toolNames: definition.toolNames,
      async prompt(_content, sink, options) {
        expect(options?.toolNames).toEqual(definition.toolNames);
        await sink({
          type: "tool.started",
          payload: { toolName: definition.toolNames[0]!, toolCallId: "role-probe-tool" },
          persistent: true,
        });
        await sink({ type: "assistant.delta", payload: { text: "{\"status\":\"READY\"}" }, persistent: false });
        return {
          assistantContent: "{\"role\":\"intake_guard\",\"status\":\"READY\"}",
          provider: "deepseek",
          modelId: "deepseek-v4-pro",
          runtimeMessageId: "message-role-probe",
        };
      },
      async steer() {},
      async followUp() {},
      async abort() {},
      dispose() { disposed = true; },
    };
    await expect(probeCampaignRoleSession({
      runtime,
      sessionId: "openhands-role-session",
      sessionFile: "persistence",
      definition,
    })).resolves.toEqual({
      role: "intake_guard",
      sessionId: "openhands-role-session",
      sessionFile: "persistence",
      toolNames: definition.toolNames,
      stopReason: "stop",
      toolCallCount: 1,
      textReceived: true,
    });
    expect(disposed).toBe(true);

    let mismatchDisposed = false;
    await expect(probeCampaignRoleSession({
      runtime: {
        ...runtime,
        toolNames: [...definition.toolNames, "unregistered_tool"],
        dispose() { mismatchDisposed = true; },
      },
      sessionId: "openhands-role-session-mismatch",
      sessionFile: null,
      definition,
    })).rejects.toThrow(/tool boundary mismatch/u);
    expect(mismatchDisposed).toBe(true);
  });
});

describe("P02 durable campaign safety", () => {
  it("is idempotent and permits lease recovery only after release", async () => {
    const { workspace, campaign } = await repositories();
    const created = ensureP02Campaign(workspace, campaign);
    expect(ensureP02Campaign(workspace, campaign).campaignId).toBe(created.campaignId);
    campaign.acquireLease(created.campaignId, "worker-a", 90);
    expect(() => campaign.acquireLease(created.campaignId, "worker-b", 90)).toThrow(/live lease/i);
    campaign.releaseLease(created.campaignId, "worker-a");
    expect(() => campaign.acquireLease(created.campaignId, "worker-b", 90)).not.toThrow();
    workspace.close();
  });

  it("enforces two hardware jobs, 10000 shots, and 600 cumulative seconds transactionally", async () => {
    const { workspace, campaign } = await repositories();
    const created = ensureP02Campaign(workspace, campaign);

    function hardwareRequest(index: number): string {
      return campaign.prepareExternalRequest({
        campaignId: created.campaignId,
        provider: "tianyan",
        requestKind: "submit_experiment",
        target: "hardware-test",
        idempotencyKey: `hardware-${index}`,
        approvalHash: authorizationHash(),
        requestHash: `${index}`.repeat(64).slice(0, 64),
      });
    }

    const first = campaign.createQuantumJob({
      campaignId: created.campaignId,
      externalRequestId: hardwareRequest(1),
      purpose: "representative_qgnn_subcircuit",
      backend: "hardware-test",
      targetType: "HARDWARE",
      circuitHash: "a".repeat(64),
      shots: 5_000,
      estimatedExecutionSeconds: 300,
    });
    const second = campaign.createQuantumJob({
      campaignId: created.campaignId,
      externalRequestId: hardwareRequest(2),
      purpose: "representative_financial_qaoa",
      backend: "hardware-test",
      targetType: "HARDWARE",
      circuitHash: "b".repeat(64),
      shots: 5_000,
      estimatedExecutionSeconds: 300,
    });
    expect(() => campaign.createQuantumJob({
      campaignId: created.campaignId,
      externalRequestId: hardwareRequest(3),
      purpose: "representative_financial_qaoa",
      backend: "hardware-test",
      targetType: "HARDWARE",
      circuitHash: "c".repeat(64),
      shots: 1,
      estimatedExecutionSeconds: 1,
    })).toThrow(/budget/i);
    expect(campaign.markQuantumJobSubmitted(first.quantumJobId, "query-1").queryId).toBe("query-1");
    expect(() => campaign.markQuantumJobSubmitted(first.quantumJobId, "query-other")).toThrow(/resubmission/i);
    campaign.completeQuantumJob(first.quantumJobId, "query-1", "COMPLETED", "d".repeat(64), 500);
    expect(() => campaign.completeQuantumJob(second.quantumJobId, "query-2", "COMPLETED", "e".repeat(64), 101)).toThrow(/600-second/i);
    expect(campaign.getCampaign(created.campaignId)).toMatchObject({
      hardwareJobs: 2,
      hardwareExecutionSeconds: 500,
    });
    workspace.close();
  });
});
