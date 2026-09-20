import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { describe, expect, it } from "vitest";

import {
  ACCEPTANCE_AUDIT_MEDIA_TYPE,
  ACTIVE_AUDIT_WINDOW_MS,
  AUDIT_REPORT_INTERVAL_MS,
  AcceptanceArtifactEvidenceCache,
  type AcceptanceArtifactCandidate,
  isGeneratedAcceptanceAuditArtifact,
  persistentSequenceContinuous,
  shouldPersistAuditReport,
} from "../../apps/control-plane/src/campaign/openhands-acceptance-lane-worker.js";

async function candidate(root: string, index: number): Promise<AcceptanceArtifactCandidate> {
  const bytes = new TextEncoder().encode(JSON.stringify({ index, payload: `evidence-${index}` }));
  const sha256 = createHash("sha256").update(bytes).digest("hex");
  const relativePath = path.posix.join("sha256", sha256.slice(0, 2), sha256);
  const destination = path.join(root, ...relativePath.split("/"));
  await mkdir(path.dirname(destination), { recursive: true });
  await writeFile(destination, bytes);
  return {
    sha256,
    mediaType: "application/json",
    bytes: bytes.byteLength,
    relativePath,
    producer: "qf-load-fixture",
    parentHashes: [],
  };
}

describe("OpenHands acceptance lane-worker resource bounds", () => {
  it("accepts durable global event sequences while rejecting gaps", () => {
    expect(persistentSequenceContinuous([
      { sequence: 13_918, type: "run.started", payload: {}, createdAt: "2026-07-30T00:00:00.000Z" },
      { sequence: 13_919, type: "turn.started", payload: {}, createdAt: "2026-07-30T00:00:01.000Z" },
    ])).toBe(true);
    expect(persistentSequenceContinuous([
      { sequence: 13_918, type: "run.started", payload: {}, createdAt: "2026-07-30T00:00:00.000Z" },
      { sequence: 13_920, type: "turn.started", payload: {}, createdAt: "2026-07-30T00:00:01.000Z" },
    ])).toBe(false);
  });

  it("bounds two-hour report artifacts while preserving immediate attach and final checkpoints", () => {
    const deadline = 7_200_000;
    const reportTimes: number[] = [];
    let firstCycleAfterAttach = true;
    let lastReportAt: number | null = null;
    for (let now = ACTIVE_AUDIT_WINDOW_MS; now <= deadline; now += ACTIVE_AUDIT_WINDOW_MS) {
      if (shouldPersistAuditReport({ firstCycleAfterAttach, now, lastReportAt, deadline })) {
        reportTimes.push(now);
        lastReportAt = now;
      }
      firstCycleAfterAttach = false;
    }

    expect(reportTimes[0]).toBe(ACTIVE_AUDIT_WINDOW_MS);
    expect(reportTimes.at(-1)).toBe(deadline);
    expect(reportTimes.length).toBeLessThanOrEqual(Math.ceil(deadline / AUDIT_REPORT_INTERVAL_MS) + 1);
    expect(reportTimes.slice(1).every((at, index) => at - reportTimes[index]! <= AUDIT_REPORT_INTERVAL_MS)).toBe(true);
    expect(reportTimes.length * 3).toBeLessThanOrEqual(1_443);

    expect(shouldPersistAuditReport({
      firstCycleAfterAttach: true,
      now: 3_607_000,
      lastReportAt: 3_605_000,
      deadline,
    })).toBe(true);
  });

  it("keeps artifact file reads linear and excludes generated audit reports from evidence", async () => {
    const root = await mkdtemp(path.join(os.tmpdir(), "qf-acceptance-artifact-load-"));
    try {
      const evidenceCandidates = await Promise.all(
        Array.from({ length: 96 }, async (_value, index) => candidate(root, index)),
      );
      const generatedAuditCandidates = Array.from({ length: 2_000 }, (_value, index) => ({
        ...evidenceCandidates[index % evidenceCandidates.length]!,
        sha256: `${index.toString(16).padStart(64, "0")}`.slice(-64),
        mediaType: ACCEPTANCE_AUDIT_MEDIA_TYPE,
        producer: `qf-openhands-acceptance.${index % 3}`,
      }));
      const mixedInventory = [...evidenceCandidates, ...generatedAuditCandidates];
      const inScope = mixedInventory.filter((artifact) => !isGeneratedAcceptanceAuditArtifact(artifact));
      expect(inScope).toHaveLength(evidenceCandidates.length);

      let fileReads = 0;
      const cache = new AcceptanceArtifactEvidenceCache(root, async (_artifact, artifactPath) => {
        fileReads += 1;
        return readFile(artifactPath);
      });
      await cache.synchronize(inScope);
      const candidatesByHash = new Map(inScope.map((artifact) => [artifact.sha256, artifact]));
      const projectedTwoHourActivityIntervals = Math.ceil(7_200_000 / ACTIVE_AUDIT_WINDOW_MS);
      for (let interval = 0; interval < projectedTwoHourActivityIntervals; interval += 1) {
        await cache.reverifyNext(candidatesByHash);
        if (interval % 3 === 0) await cache.synchronize(inScope);
      }

      expect(fileReads).toBe(evidenceCandidates.length + projectedTwoHourActivityIntervals);
      expect(cache.stats()).toMatchObject({
        cachedArtifactCount: evidenceCandidates.length,
        fileVerificationCount: fileReads,
        completedReverificationSweeps: projectedTwoHourActivityIntervals / evidenceCandidates.length,
      });
      expect(fileReads).toBeLessThan(mixedInventory.length * 2);

      const nextCandidate = evidenceCandidates[0]!;
      await writeFile(path.join(root, ...nextCandidate.relativePath.split("/")), "corrupted");
      await expect(cache.reverifyNext(candidatesByHash)).rejects.toThrow(`artifact integrity failed for ${nextCandidate.sha256}`);
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });
});
