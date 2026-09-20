import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { ensureP04Campaign } from "../../apps/control-plane/src/campaign/p04-bootstrap.js";
import { P04Repository } from "../../apps/control-plane/src/campaign/p04-repository.js";
import { CampaignRepository } from "../../apps/control-plane/src/campaign/repository.js";
import { openDatabase } from "../../apps/control-plane/src/db/migrations.js";
import { WorkspaceRepository } from "../../apps/control-plane/src/db/repository.js";

const roots: string[] = [];

afterEach(async () => {
  await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
});

async function repositories() {
  const root = await mkdtemp(path.join(os.tmpdir(), "qf-p04-test-"));
  roots.push(root);
  const opened = openDatabase(path.join(root, "p04.sqlite3"), path.resolve("infra/sqlite"));
  const workspace = new WorkspaceRepository(opened.database);
  const campaigns = new CampaignRepository(opened.database);
  const p04 = new P04Repository(opened.database);
  const campaign = ensureP04Campaign(workspace, campaigns);
  const runs = p04.ensureRuns(campaign.campaignId);
  return { workspace, campaigns, p04, campaign, runs };
}

describe("P04 concurrent Campaign repository", () => {
  it("isolates three Run workspaces and keeps event sequences monotonic", async () => {
    const { workspace, p04, campaign, runs } = await repositories();
    expect(runs).toHaveLength(3);
    expect(new Set(runs.map((run) => run.relativeWorkspace)).size).toBe(3);
    for (const run of runs) {
      for (let index = 0; index < 25; index += 1) {
        expect(p04.appendEvent(run.runId, "CONCURRENT_PROBE", { index })).toBe(index + 1);
      }
    }
    const snapshot = p04.concurrencySnapshot(campaign.campaignId);
    expect(snapshot.journalMode).toBe("wal");
    expect(snapshot.integrityCheck).toBe("ok");
    expect(snapshot.eventSequences).toEqual(expect.arrayContaining([
      expect.objectContaining({ eventCount: 25, minimumSequence: 1, maximumSequence: 25, distinctSequences: 25 }),
    ]));
    workspace.close();
  });

  it("rejects competing Run and hardware leases while allowing same-Run recovery", async () => {
    const { workspace, p04, campaign, runs } = await repositories();
    const finance = runs.find((run) => run.lane === "finance")!;
    const tool = runs.find((run) => run.lane === "tool_factory")!;
    p04.acquireRunLease(finance.runId, "worker-a", 10_001, false);
    expect(() => p04.acquireRunLease(finance.runId, "worker-b", 10_002, false)).toThrow(/live lease/i);
    expect(() => p04.acquireRunLease(finance.runId, "worker-b", 10_002, true)).not.toThrow();
    p04.acquireHardwareLease({ campaignId: campaign.campaignId, runId: finance.runId, workerId: "worker-b", recovery: false });
    expect(() => p04.acquireHardwareLease({
      campaignId: campaign.campaignId,
      runId: tool.runId,
      workerId: "worker-tool",
      recovery: false,
    })).toThrow(/active P04 submission lease/i);
    expect(() => p04.acquireHardwareLease({
      campaignId: campaign.campaignId,
      runId: finance.runId,
      workerId: "worker-c",
      recovery: true,
    })).not.toThrow();
    workspace.close();
  });
});
