import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { storeArtifact } from "../../apps/control-plane/src/artifact-store.js";
import { openDatabase } from "../../apps/control-plane/src/db/migrations.js";
import { WorkspaceRepository } from "../../apps/control-plane/src/db/repository.js";
import { normalizeP07OpenHandsUsage } from "../../apps/control-plane/src/p07/p07-orchestrator.js";
import { P07Repository } from "../../apps/control-plane/src/p07/p07-repository.js";

const temporaryRoots: string[] = [];

afterEach(async () => {
  await Promise.all(temporaryRoots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
});

describe("P07 durable campaign repository", () => {
  it("normalizes OpenHands cache usage without double-counting Provider prompt tokens", () => {
    expect(normalizeP07OpenHandsUsage({
      input: 120,
      output: 30,
      cacheRead: 10,
      cacheWrite: 5,
      totalTokens: 150,
    })).toEqual({ promptTokens: 105, completionTokens: 30, cachedTokens: 15, totalTokens: 150 });
    expect(() => normalizeP07OpenHandsUsage({
      input: 10,
      output: 2,
      cacheRead: 11,
      cacheWrite: 0,
      totalTokens: 12,
    })).toThrow(/cache usage exceeded prompt usage/u);
    expect(() => normalizeP07OpenHandsUsage({
      input: 10,
      output: 2,
      cacheRead: 0,
      cacheWrite: 0,
      totalTokens: 13,
    })).toThrow(/not verifiable/u);
  });

  it("locks the provider/model, keeps stable events and counts only verified usage once", async () => {
    const directory = await mkdtemp(path.join(os.tmpdir(), "qf-p07-test-"));
    temporaryRoots.push(directory);
    const opened = openDatabase(path.join(directory, "p07.sqlite3"), path.resolve("infra/sqlite"));
    const workspace = new WorkspaceRepository(opened.database);
    const project = workspace.createProject("P07", "durable acceptance");
    const conversation = workspace.createConversation({
      projectId: project.projectId,
      title: "P07",
      mode: "OPENHANDS",
      provider: "deepseek",
      modelId: "deepseek-v4-pro",
    });
    const repository = new P07Repository(opened.database);
    const campaignId = repository.createCampaign({ conversationId: conversation.conversationId, taskId: conversation.taskId, objective: "six choose three" });
    expect(repository.getCampaign(campaignId)).toMatchObject({
      provider: "deepseek/getoken",
      modelId: "deepseek-v4-pro",
      status: "AWAITING_AUTHORIZATION",
      minimumRuntimeSeconds: 21_600,
      minimumVerifiedTokens: 20_000_000,
      targetVerifiedTokens: 50_000_000,
      formalTestSealed: true,
    });
    repository.authorize(campaignId, "a".repeat(64));
    const runId = repository.acquireRun(campaignId, "deepseek_agents", "worker", process.pid);
    const first = repository.appendEvent({ campaignId, lane: "deepseek_agents", eventType: "model.usage", idempotencyKey: "stable", payload: { summary: "one" } });
    const duplicate = repository.appendEvent({ campaignId, lane: "deepseek_agents", eventType: "model.usage", idempotencyKey: "stable", payload: { summary: "different" } });
    expect(duplicate).toEqual(first);
    const ledger = repository.beginModelCall({ campaignId, runId, role: "scientific_critic", purpose: "audit", idempotencyKey: "model-once", promptSha256: "b".repeat(64) });
    expect(repository.beginModelCall({ campaignId, runId, role: "scientific_critic", purpose: "audit", idempotencyKey: "model-once", promptSha256: "b".repeat(64) }).callId).toBe(ledger.callId);
    const manifest = await storeArtifact({ root: path.join(directory, "artifacts"), data: new TextEncoder().encode("evidence"), mediaType: "text/plain", producer: "test" });
    const artifact = workspace.registerArtifact(conversation.conversationId, manifest);
    repository.completeModelCall({ callId: ledger.callId, responseSha256: "c".repeat(64), promptTokens: 120, completionTokens: 30, cachedTokens: 10, totalTokens: 160, responseArtifactSha256: artifact.sha256 });
    repository.completeModelCall({ callId: ledger.callId, responseSha256: "c".repeat(64), promptTokens: 120, completionTokens: 30, cachedTokens: 10, totalTokens: 160, responseArtifactSha256: artifact.sha256 });
    expect(repository.getCampaign(campaignId)).toMatchObject({ verifiedPromptTokens: 130, verifiedCompletionTokens: 30, verifiedTotalTokens: 160 });
    const compatible = repository.beginModelCall({ campaignId, runId, role: "scientific_critic", purpose: "audit", idempotencyKey: "stable-wave-key", promptSha256: "d".repeat(64) });
    expect(compatible.callId).toBe(ledger.callId);
    expect(compatible.status).toBe("COMPLETED");
    const failed = repository.beginModelCall({ campaignId, runId, role: "scientific_critic", purpose: "retry", idempotencyKey: "retry-wave-key", promptSha256: "e".repeat(64) });
    repository.failModelCall(failed.callId, "FAILED", "INJECTED_RATE_LIMIT");
    const retried = repository.beginModelCall({ campaignId, runId, role: "scientific_critic", purpose: "retry", idempotencyKey: "retry-wave-key", promptSha256: "f".repeat(64) });
    expect(retried).toMatchObject({ callId: failed.callId, status: "STARTED", promptSha256: "f".repeat(64) });
    expect(() => repository.createHardwareBatch({ campaignId, idempotencyKey: "too-many", circuitHashes: Array.from({ length: 51 }, (_, index) => String(index)), shots: 100 })).toThrow(/1-50/);
    const continuationConversation = workspace.createConversation({
      projectId: project.projectId,
      title: "P07 gpt-5.6 continuation",
      mode: "OPENHANDS",
      provider: "openai",
      modelId: "gpt-5.6",
    });
    const continuationId = repository.createCampaign({
      conversationId: continuationConversation.conversationId,
      taskId: continuationConversation.taskId,
      objective: "continue without hardware resubmission",
      provider: "openai/getoken",
      modelId: "gpt-5.6",
      predecessorCampaignId: campaignId,
    });
    expect(repository.getCampaign(continuationId)).toMatchObject({
      predecessorCampaignId: campaignId,
      provider: "openai/getoken",
      modelId: "gpt-5.6",
      verifiedTotalTokens: 0,
    });
    const predecessorBatchId = repository.createHardwareBatch({
      campaignId,
      idempotencyKey: "predecessor-hardware",
      circuitHashes: Array.from({ length: 50 }, (_, index) => `circuit-${index}`),
      shots: 100,
    });
    repository.updateHardwareBatch({
      batchId: predecessorBatchId,
      status: "COMPLETED",
      queryIds: Array.from({ length: 50 }, (_, index) => `query-${index}`),
      resultArtifactSha256: artifact.sha256,
    });
    const reusedBatchId = repository.reuseCompletedHardwareBatch(continuationId, campaignId);
    expect(repository.reuseCompletedHardwareBatch(continuationId, campaignId)).toBe(reusedBatchId);
    expect(repository.getDetail(continuationId).hardwareBatches).toEqual([
      expect.objectContaining({
        batchId: reusedBatchId,
        reusedFromBatchId: predecessorBatchId,
        status: "COMPLETED",
        circuitCount: 50,
        queryIds: expect.arrayContaining(["query-0", "query-49"]),
      }),
    ]);
    repository.authorize(continuationId, "d".repeat(64));
    const continuationRunId = repository.acquireRun(continuationId, "deepseek_agents", "worker", process.pid);
    const continuationLedger = repository.beginModelCall({
      campaignId: continuationId,
      runId: continuationRunId,
      role: "research_supervisor",
      purpose: "successor audit",
      idempotencyKey: "successor-model-once",
      promptSha256: "e".repeat(64),
    });
    expect(continuationLedger).toMatchObject({ provider: "openai/getoken", modelId: "gpt-5.6" });
    workspace.close();
  });
});
