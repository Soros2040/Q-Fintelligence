import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { ensureP02Campaign } from "../../apps/control-plane/src/campaign/bootstrap.js";
import { p03AuthorizationHash } from "../../apps/control-plane/src/campaign/p03-authorization.js";
import { computeP03BackoffSeconds } from "../../apps/control-plane/src/campaign/p03-operations.js";
import { CampaignRepository } from "../../apps/control-plane/src/campaign/repository.js";
import { openDatabase } from "../../apps/control-plane/src/db/migrations.js";
import { WorkspaceRepository } from "../../apps/control-plane/src/db/repository.js";
import { buildP03StatusRows } from "../../apps/web/src/App.js";

const temporaryRoots: string[] = [];

afterEach(async () => {
  await Promise.all(temporaryRoots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
});

async function repositories(): Promise<{
  workspace: WorkspaceRepository;
  campaign: CampaignRepository;
  campaignId: string;
}> {
  const root = await mkdtemp(path.join(os.tmpdir(), "qf-p03-queue-test-"));
  temporaryRoots.push(root);
  const opened = openDatabase(path.join(root, "campaign.sqlite3"), path.resolve("infra/sqlite"));
  const workspace = new WorkspaceRepository(opened.database);
  const campaign = new CampaignRepository(opened.database);
  return { workspace, campaign, campaignId: ensureP02Campaign(workspace, campaign).campaignId };
}

describe("P03 durable TianYan queue state", () => {
  it("starts polling at 15 seconds, backs off exponentially, jitters, and never exceeds 120 seconds", () => {
    expect(computeP03BackoffSeconds(0, 0)).toBe(15);
    expect(computeP03BackoffSeconds(1, 1)).toBe(33);
    expect(computeP03BackoffSeconds(2, 0.5)).toBe(63);
    expect(computeP03BackoffSeconds(3, 1)).toBe(120);
    expect(computeP03BackoffSeconds(20, 1)).toBe(120);
  });

  it("atomically reserves the independent P03 budget and remains idempotent", async () => {
    const { workspace, campaign, campaignId } = await repositories();
    const run = campaign.ensureP03QueueRun({
      campaignId,
      ordinal: 1,
      authorizationHash: p03AuthorizationHash(),
      backend: "tianyan176",
      shots: 100,
      purpose: "queue_state_machine_validation",
    });
    expect(run.lifecycleState).toBe("DISCOVERING");

    const prepared = campaign.prepareP03QueueSubmission({
      queueRunId: run.queueRunId,
      circuitHash: "a".repeat(64),
      requestHash: "b".repeat(64),
      idempotencyKey: `${campaignId}:P03:first`,
      reservedExecutionSeconds: 240,
    });
    const replay = campaign.prepareP03QueueSubmission({
      queueRunId: run.queueRunId,
      circuitHash: "a".repeat(64),
      requestHash: "b".repeat(64),
      idempotencyKey: `${campaignId}:P03:first`,
      reservedExecutionSeconds: 240,
    });

    expect(prepared.lifecycleState).toBe("READY_TO_SUBMIT");
    expect(replay.quantumJobId).toBe(prepared.quantumJobId);
    expect(campaign.getP03HardwareBudget(campaignId)).toMatchObject({
      p02HistoricalJobs: 0,
      p03Jobs: 1,
      p03Shots: 100,
      p03ReservedExecutionSeconds: 240,
      p03MaxJobs: 2,
      p03MaxShots: 200,
      p03MaxExecutionSeconds: 600,
    });
    workspace.close();
  });

  it("recovers the original Query ID and prohibits resubmission from UNKNOWN", async () => {
    const { workspace, campaign, campaignId } = await repositories();
    const run = campaign.ensureP03QueueRun({
      campaignId,
      ordinal: 1,
      authorizationHash: p03AuthorizationHash(),
      backend: "tianyan176",
      shots: 100,
      purpose: "queue_state_machine_validation",
    });
    campaign.prepareP03QueueSubmission({
      queueRunId: run.queueRunId,
      circuitHash: "c".repeat(64),
      requestHash: "d".repeat(64),
      idempotencyKey: `${campaignId}:P03:recovery`,
      reservedExecutionSeconds: 240,
    });
    campaign.markP03Submitting(run.queueRunId);
    campaign.markP03Submitted(run.queueRunId, "query-original", "UNKNOWN", new Date().toISOString());
    expect(campaign.markP03Submitted(run.queueRunId, "query-original", "UNKNOWN", new Date().toISOString()).queryId)
      .toBe("query-original");
    expect(() => campaign.markP03Submitted(run.queueRunId, "query-replacement", "UNKNOWN", new Date().toISOString()))
      .toThrow(/resubmission/i);

    campaign.markP03SubmissionUnknown(run.queueRunId, null);
    expect(() => campaign.markP03Submitting(run.queueRunId)).toThrow(/query-only|UNKNOWN/i);
    expect(campaign.ensureP03QueueRun({
      campaignId,
      ordinal: 1,
      authorizationHash: p03AuthorizationHash(),
      backend: "tianyan176",
      shots: 100,
      purpose: "queue_state_machine_validation",
    }).queryId).toBe("query-original");
    workspace.close();
  });

  it("blocks a second P03 job without terminal defect evidence", async () => {
    const { workspace, campaign, campaignId } = await repositories();
    expect(() => campaign.ensureP03QueueRun({
      campaignId,
      ordinal: 2,
      authorizationHash: p03AuthorizationHash(),
      backend: "tianyan176",
      shots: 100,
      purpose: "queue_state_machine_regression",
    })).toThrow(/second.*evidence/i);
    expect(campaign.listP03QueueRuns(campaignId)).toHaveLength(0);
    workspace.close();
  });

  it("checkpoints queue recovery and completes idempotently without changing the P02 ledger", async () => {
    const { workspace, campaign, campaignId } = await repositories();
    const run = campaign.ensureP03QueueRun({
      campaignId,
      ordinal: 1,
      authorizationHash: p03AuthorizationHash(),
      backend: "tianyan176",
      shots: 100,
      purpose: "queue_state_machine_validation",
    });
    campaign.prepareP03QueueSubmission({
      queueRunId: run.queueRunId,
      circuitHash: "e".repeat(64),
      requestHash: "f".repeat(64),
      idempotencyKey: `${campaignId}:P03:checkpoint`,
      reservedExecutionSeconds: 240,
    });
    campaign.markP03Submitting(run.queueRunId);
    campaign.markP03Submitted(run.queueRunId, "query-checkpoint", "UNKNOWN", new Date().toISOString());
    expect(campaign.markP03Checkpoint30m(run.queueRunId).checkpoint30mAt).not.toBeNull();
    const first = campaign.completeP03QueueRun({
      queueRunId: run.queueRunId,
      terminalState: "COMPLETED",
      providerStatus: "COMPLETED",
      rawResultArtifactSha256: "1".repeat(64),
    });
    const replay = campaign.completeP03QueueRun({
      queueRunId: run.queueRunId,
      terminalState: "COMPLETED",
      providerStatus: "COMPLETED",
      rawResultArtifactSha256: "1".repeat(64),
    });
    expect(replay.queryId).toBe(first.queryId);
    expect(campaign.getCampaign(campaignId)).toMatchObject({ hardwareJobs: 0, hardwareExecutionSeconds: 0 });
    expect(campaign.getP03HardwareBudget(campaignId).p03ReservedExecutionSeconds).toBe(240);
    expect(buildP03StatusRows(campaign.getCampaign(campaignId))).toContainEqual(["下一次查询", "无（终态）"]);
    workspace.close();
  });

  it("renders only reliable queue facts and explicit unknown platform fields", async () => {
    const { workspace, campaign, campaignId } = await repositories();
    const summary = campaign.getCampaign(campaignId);
    const rows = buildP03StatusRows(summary);
    expect(rows).toContainEqual(["平台状态", "未知"]);
    expect(rows).toContainEqual(["队列位置", "未知"]);
    expect(rows).toContainEqual(["预计开始", "未知"]);
    expect(JSON.stringify(rows)).not.toMatch(/prompt|authorization|secret|token/iu);
    workspace.close();
  });
});
