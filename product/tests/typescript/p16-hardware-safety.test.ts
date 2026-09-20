// Authorship category: supervisor_infrastructure
// Seals P16 hardware idempotency, UNKNOWN recovery, and P15 isolation.

import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { openDatabase } from "../../apps/control-plane/src/db/migrations.js";
import { WorkspaceRepository } from "../../apps/control-plane/src/db/repository.js";
import {
  P16HardwareRepository,
  p16BatchWithoutQueryIdsMustRemainQueryOnly,
} from "../../apps/control-plane/src/p16/p16-hardware-repository.js";

const temporaryRoots: string[] = [];

afterEach(async () => {
  await Promise.all(temporaryRoots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
});

async function fixture() {
  const directory = await mkdtemp(path.join(os.tmpdir(), "qf-p16-hardware-"));
  temporaryRoots.push(directory);
  const opened = openDatabase(path.join(directory, "workspace.sqlite3"), path.resolve("infra/sqlite"));
  const workspace = new WorkspaceRepository(opened.database);
  const project = workspace.createProject("P16", "hardware safety");
  const conversation = workspace.createConversation({
    projectId: project.projectId,
    title: "P16 DeepSeek",
    mode: "OPENHANDS",
    provider: "deepseek",
    modelId: "deepseek-v4-pro",
  });
  const calibrationSnapshotId = workspace.registerP15Calibration({
    projectId: project.projectId,
    conversationId: conversation.conversationId,
    machineStatus: "running",
    retrievedAt: "2026-07-25T16:00:00.000Z",
    calibrationAt: "2026-07-25 23:59:00",
    cqlibVersion: "1.3.11",
    rawSha256: "1".repeat(64),
    normalizedSha256: "2".repeat(64),
    manifestSha256: "3".repeat(64),
    diffSha256: "4".repeat(64),
    csvSha256: "5".repeat(64),
    topologySha256: "6".repeat(64),
    completeness: "COMPLETE",
    missingFields: [],
    warnings: [],
    sourceIds: [],
    previousSnapshotId: null,
  });
  const approval = workspace.decideApproval(
    workspace.createApprovalRequest({
      conversationId: conversation.conversationId,
      action: "SUBMIT_HARDWARE",
      subjectHash: "7".repeat(64),
      rationale: "explicit frozen P16 batch",
      requestedBy: "USER",
    }).approvalId,
    "APPROVE",
    "HUMAN",
  );
  return {
    workspace,
    repository: new P16HardwareRepository(opened.database),
    project,
    conversation,
    calibrationSnapshotId,
    approvalId: approval.approvalId,
  };
}

describe("P16 durable hardware recovery", () => {
  it("makes COMMITTING and UNKNOWN batches permanently query-only", () => {
    expect(p16BatchWithoutQueryIdsMustRemainQueryOnly("COMMITTING")).toBe(true);
    expect(p16BatchWithoutQueryIdsMustRemainQueryOnly("UNKNOWN")).toBe(true);
    expect(p16BatchWithoutQueryIdsMustRemainQueryOnly("PREPARED")).toBe(false);
  });

  it("persists one immutable request and refuses automatic resubmission after UNKNOWN", async () => {
    const item = await fixture();
    const batch = item.repository.prepareBatch({
      projectId: item.project.projectId,
      conversationId: item.conversation.conversationId,
      taskId: item.conversation.taskId,
      protocolSha256: "a".repeat(64),
      searchSpaceSha256: "b".repeat(64),
      quboSha256: "c".repeat(64),
      calibrationSnapshotId: item.calibrationSnapshotId,
      generationIndex: 0,
      batchIndex: 0,
      strategy: "preregistered_qaoa_statevector",
      circuitSha256s: ["d".repeat(64)],
      mappingReportSha256: "e".repeat(64),
      validationReportSha256: "f".repeat(64),
      approvalId: item.approvalId,
      requestSha256: "7".repeat(64),
      requestArtifactSha256: "8".repeat(64),
    });
    expect(batch.status).toBe("PREPARED");
    expect(item.repository.markCommitting(batch.batchId).submitAttempts).toBe(1);
    const unknown = item.repository.markUnknown(batch.batchId, {
      code: "P16_UNKNOWN_SUBMISSION",
      queryIds: [],
      resubmissionAllowed: false,
    });
    expect(unknown.status).toBe("UNKNOWN");
    expect(() => item.repository.markCommitting(batch.batchId)).toThrow(/query-only|resubmission/iu);
    item.workspace.close();
  });

  it("persists Query IDs before result artifacts and queries only the same handle", async () => {
    const item = await fixture();
    const batch = item.repository.prepareBatch({
      projectId: item.project.projectId,
      conversationId: item.conversation.conversationId,
      taskId: item.conversation.taskId,
      protocolSha256: "a".repeat(64),
      searchSpaceSha256: "b".repeat(64),
      quboSha256: "c".repeat(64),
      calibrationSnapshotId: item.calibrationSnapshotId,
      generationIndex: 1,
      batchIndex: 1,
      strategy: "preregistered_qaoa_statevector",
      circuitSha256s: ["d".repeat(64), "e".repeat(64)],
      mappingReportSha256: "f".repeat(64),
      validationReportSha256: "0".repeat(64),
      approvalId: item.approvalId,
      requestSha256: "9".repeat(64),
      requestArtifactSha256: "8".repeat(64),
    });
    item.repository.markCommitting(batch.batchId);
    const submitted = item.repository.markSubmitted(batch.batchId, ["qid-1", "qid-2"]);
    expect(submitted.status).toBe("SUBMITTED");
    expect(submitted.queryIds).toEqual(["qid-1", "qid-2"]);
    expect(submitted.submissionArtifactSha256).toBeNull();
    const attached = item.repository.attachSubmissionArtifact(batch.batchId, "1".repeat(64));
    expect(attached.submissionArtifactSha256).toBe("1".repeat(64));
    expect(item.repository.markQuerying(batch.batchId).queryIds).toEqual(["qid-1", "qid-2"]);
    const pending = item.repository.markQueryPending(batch.batchId);
    expect(pending.status).toBe("SUBMITTED");
    expect(pending.queryIds).toEqual(["qid-1", "qid-2"]);
    item.workspace.close();
  });
});
