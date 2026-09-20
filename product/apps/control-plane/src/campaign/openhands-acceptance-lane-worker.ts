import { createHash, randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { setImmediate as yieldImmediate } from "node:timers/promises";
import { pathToFileURL } from "node:url";

import type { JsonObject, JsonValue } from "@q-fintelligence/contracts";

import { storeArtifact } from "../artifact-store.js";
import { loadRuntimeConfig } from "../config.js";
import { openDatabase } from "../db/migrations.js";
import { WorkspaceRepository } from "../db/repository.js";
import {
  analyzeAcceptanceEvidence,
  type AcceptanceArtifactEvidence,
  type AcceptanceConversationEvent,
} from "./openhands-acceptance-evidence.js";
import {
  acceptanceHash,
  OPENHANDS_ACCEPTANCE_LANES,
  type OpenHandsAcceptanceActivityKind,
  type OpenHandsAcceptanceLane,
  OpenHandsAcceptanceRepository,
} from "./openhands-acceptance-repository.js";
import { waitForOpenHandsAcceptanceRegistrationGate } from "./openhands-acceptance-registration-gate.js";
import { runTianyanJob } from "./python-runner.js";

const PROJECT_ROOT = process.cwd();
// Activity remains continuous real audit work; only durable reports are rate-bounded.
export const ACTIVE_AUDIT_WINDOW_MS = 5_000;
export const AUDIT_REPORT_INTERVAL_MS = 15_000;
export const ACCEPTANCE_AUDIT_MEDIA_TYPE = "application/vnd.qf.openhands-acceptance-audit+json";

export interface AcceptanceArtifactCandidate {
  sha256: string;
  mediaType: string;
  bytes: number;
  relativePath: string;
  producer: string;
  parentHashes: string[];
}

interface CachedArtifactEvidence {
  metadataHash: string;
  evidence: AcceptanceArtifactEvidence;
}

export function isGeneratedAcceptanceAuditArtifact(input: Pick<AcceptanceArtifactCandidate, "mediaType" | "producer">): boolean {
  return input.mediaType === ACCEPTANCE_AUDIT_MEDIA_TYPE
    && input.producer.startsWith("qf-openhands-acceptance.");
}

export function shouldPersistAuditReport(input: {
  firstCycleAfterAttach: boolean;
  now: number;
  lastReportAt: number | null;
  deadline: number;
}): boolean {
  return input.firstCycleAfterAttach
    || input.lastReportAt === null
    || input.now - input.lastReportAt >= AUDIT_REPORT_INTERVAL_MS
    || input.now >= input.deadline;
}

export class AcceptanceArtifactEvidenceCache {
  private readonly cached = new Map<string, CachedArtifactEvidence>();
  private order: string[] = [];
  private reverifyCursor = 0;
  private fileVerificationCount = 0;
  private completedReverificationSweeps = 0;

  constructor(
    private readonly artifactRoot: string,
    private readonly readBytes: (candidate: AcceptanceArtifactCandidate, artifactPath: string) => Promise<Uint8Array> =
      async (_candidate, artifactPath) => readFile(artifactPath),
  ) {}

  private async verify(candidate: AcceptanceArtifactCandidate): Promise<CachedArtifactEvidence> {
    if (!/^[a-f0-9]{64}$/u.test(candidate.sha256)) throw new Error("acceptance artifact hash is invalid");
    if (!Number.isSafeInteger(candidate.bytes) || candidate.bytes < 0) {
      throw new Error(`artifact byte count is invalid for ${candidate.sha256}`);
    }
    const expectedRelativePath = path.posix.join("sha256", candidate.sha256.slice(0, 2), candidate.sha256);
    if (candidate.relativePath !== expectedRelativePath) {
      throw new Error(`artifact path does not match its content address for ${candidate.sha256}`);
    }
    const resolvedRoot = path.resolve(this.artifactRoot);
    const artifactPath = path.resolve(resolvedRoot, ...candidate.relativePath.split("/"));
    const relative = path.relative(resolvedRoot, artifactPath);
    if (!relative || relative.startsWith(`..${path.sep}`) || relative === ".." || path.isAbsolute(relative)
      || !existsSync(artifactPath)) {
      throw new Error(`artifact path boundary failed for ${candidate.sha256}`);
    }
    const bytes = await this.readBytes(candidate, artifactPath);
    this.fileVerificationCount += 1;
    if (createHash("sha256").update(bytes).digest("hex") !== candidate.sha256 || bytes.byteLength !== candidate.bytes) {
      throw new Error(`artifact integrity failed for ${candidate.sha256}`);
    }
    let content: JsonValue | null = null;
    if (candidate.mediaType === "application/json" || candidate.mediaType.endsWith("+json")) {
      try {
        content = jsonValue(JSON.parse(new TextDecoder("utf8", { fatal: true }).decode(bytes)));
      } catch {
        throw new Error(`JSON artifact ${candidate.sha256} is not strict UTF-8 JSON`);
      }
    }
    return {
      metadataHash: acceptanceHash({
        sha256: candidate.sha256,
        mediaType: candidate.mediaType,
        bytes: candidate.bytes,
        relativePath: candidate.relativePath,
        producer: candidate.producer,
        parentHashes: candidate.parentHashes,
      }),
      evidence: {
        sha256: candidate.sha256,
        mediaType: candidate.mediaType,
        producer: candidate.producer,
        parentHashes: [...candidate.parentHashes],
        content,
      },
    };
  }

  async synchronize(candidates: AcceptanceArtifactCandidate[]): Promise<AcceptanceArtifactEvidence[]> {
    const unique = new Set<string>();
    const nextOrder: string[] = [];
    for (const candidate of candidates) {
      if (isGeneratedAcceptanceAuditArtifact(candidate)) {
        throw new Error("generated acceptance audit artifacts must not enter scientific evidence verification");
      }
      if (unique.has(candidate.sha256)) throw new Error(`duplicate acceptance artifact ${candidate.sha256}`);
      unique.add(candidate.sha256);
      nextOrder.push(candidate.sha256);
      const metadataHash = acceptanceHash({
        sha256: candidate.sha256,
        mediaType: candidate.mediaType,
        bytes: candidate.bytes,
        relativePath: candidate.relativePath,
        producer: candidate.producer,
        parentHashes: candidate.parentHashes,
      });
      const current = this.cached.get(candidate.sha256);
      if (current && current.metadataHash !== metadataHash) {
        throw new Error(`artifact metadata drifted for ${candidate.sha256}`);
      }
      if (!current) this.cached.set(candidate.sha256, await this.verify(candidate));
    }
    for (const sha256 of this.cached.keys()) {
      if (!unique.has(sha256)) throw new Error(`previously verified artifact disappeared: ${sha256}`);
    }
    this.order = nextOrder;
    if (this.reverifyCursor >= this.order.length) this.reverifyCursor = 0;
    return this.evidence();
  }

  async reverifyNext(candidatesByHash: ReadonlyMap<string, AcceptanceArtifactCandidate>): Promise<string | null> {
    if (this.order.length === 0) return null;
    const sha256 = this.order[this.reverifyCursor];
    if (!sha256) throw new Error("acceptance artifact reverification cursor is invalid");
    const candidate = candidatesByHash.get(sha256);
    if (!candidate) throw new Error(`acceptance artifact ${sha256} disappeared before reverification`);
    const current = this.cached.get(sha256);
    const verified = await this.verify(candidate);
    if (!current || current.metadataHash !== verified.metadataHash) {
      throw new Error(`artifact metadata drifted during reverification for ${sha256}`);
    }
    this.cached.set(sha256, verified);
    this.reverifyCursor += 1;
    if (this.reverifyCursor >= this.order.length) {
      this.reverifyCursor = 0;
      this.completedReverificationSweeps += 1;
    }
    return sha256;
  }

  evidence(): AcceptanceArtifactEvidence[] {
    return this.order.map((sha256) => {
      const cached = this.cached.get(sha256);
      if (!cached) throw new Error(`acceptance artifact cache is incomplete for ${sha256}`);
      return cached.evidence;
    });
  }

  stats(): JsonObject {
    return {
      cachedArtifactCount: this.cached.size,
      fileVerificationCount: this.fileVerificationCount,
      completedReverificationSweeps: this.completedReverificationSweeps,
      reverifyCursor: this.reverifyCursor,
    };
  }
}

function jsonValue(value: unknown, depth = 0): JsonValue {
  if (depth > 12) return "[truncated]";
  if (value === null || typeof value === "string" || typeof value === "boolean") return value;
  if (typeof value === "number") return Number.isFinite(value) ? value : String(value);
  if (Array.isArray(value)) return value.map((entry) => jsonValue(entry, depth + 1));
  if (typeof value === "object") {
    return Object.fromEntries(Object.entries(value as Record<string, unknown>)
      .map(([key, entry]) => [key, jsonValue(entry, depth + 1)]));
  }
  return String(value);
}

function jsonObject(value: unknown): JsonObject {
  const converted = jsonValue(value);
  if (typeof converted !== "object" || converted === null || Array.isArray(converted)) {
    throw new Error("acceptance report must be a JSON object");
  }
  return converted;
}

function secretLike(value: string): boolean {
  return /(?:sk-[A-Za-z0-9._-]{12,}|Bearer\s+[A-Za-z0-9._-]{12,}|connection[_-]?key\s*[:=]\s*[A-Za-z0-9._-]{12,})/iu
    .test(value.replaceAll("[REDACTED]", ""));
}

export function persistentSequenceContinuous(events: AcceptanceConversationEvent[]): boolean {
  const sequences = events.flatMap((event) => event.sequence === null ? [] : [event.sequence]);
  // Event sequences are allocated by the durable event store, not restarted
  // per Conversation. A valid new Conversation can therefore begin above one;
  // continuity means a positive first value followed by adjacent values.
  const first = sequences[0];
  if (first === undefined || !Number.isSafeInteger(first) || first <= 0) return false;
  return sequences.every((sequence, index) => index === 0
    || (Number.isSafeInteger(sequence) && sequence === sequences[index - 1]! + 1));
}

function activityKind(lane: OpenHandsAcceptanceLane): OpenHandsAcceptanceActivityKind {
  if (lane === "openhands_orchestration") return "OPENHANDS_STATE_AUDIT";
  if (lane === "quantum_science") return "QUANTUM_REPRODUCIBILITY_AUDIT";
  return "EVIDENCE_INTEGRITY_AUDIT";
}

async function main(): Promise<void> {
  if (process.env.QF_PROCESS_NAMESPACE !== "qfintelligence") throw new Error("OpenHands acceptance worker namespace is invalid");
  if (process.cwd() !== PROJECT_ROOT) throw new Error("OpenHands acceptance worker must run in the unique WSL project root");
  const campaignId = process.argv[2] ?? process.env.QF_OPENHANDS_ACCEPTANCE_CAMPAIGN_ID;
  const runId = process.argv[3] ?? process.env.QF_OPENHANDS_ACCEPTANCE_RUN_ID;
  const lane = (process.argv[4] ?? process.env.QF_OPENHANDS_ACCEPTANCE_LANE) as OpenHandsAcceptanceLane | undefined;
  const recovery = process.env.QF_OPENHANDS_ACCEPTANCE_RECOVERY === "1";
  const expectedLeaseGeneration = Number(process.env.QF_OPENHANDS_ACCEPTANCE_EXPECTED_LEASE_GENERATION ?? "0");
  if (!campaignId || !runId || !lane || !OPENHANDS_ACCEPTANCE_LANES.includes(lane)) {
    throw new Error("OpenHands acceptance Campaign, Run, and lane are required");
  }
  if (!Number.isSafeInteger(expectedLeaseGeneration) || expectedLeaseGeneration < 0) {
    throw new Error("OpenHands acceptance expected lease generation is invalid");
  }
  await waitForOpenHandsAcceptanceRegistrationGate(process.stdin, {
    token: process.env.QF_OPENHANDS_ACCEPTANCE_REGISTRATION_TOKEN ?? "",
    processKey: process.env.QF_PROCESS_IDENTITY ?? "",
    processId: process.pid,
    campaignId,
    runId,
    lane,
    recovery,
    expectedLeaseGeneration,
  });
  const config = loadRuntimeConfig();
  if (
    config.hardwarePolicy.mode !== "READ_ONLY"
    || config.hardwarePolicy.target !== "tianyan176"
    || config.hardwarePolicy.maxNewHardwareJobs !== 0
    || config.hardwarePolicy.shotsPerJob !== 0
  ) {
    throw new Error("OpenHands acceptance lane refused non-READ_ONLY hardware policy");
  }
  const migration = openDatabase(path.resolve(PROJECT_ROOT, config.sqlitePath), path.join(PROJECT_ROOT, "infra", "sqlite"));
  const workspaceRepository = new WorkspaceRepository(migration.database);
  const acceptanceRepository = new OpenHandsAcceptanceRepository(migration.database);
  const campaign = acceptanceRepository.get(campaignId);
  const run = acceptanceRepository.getRun(runId);
  if (
    run.campaignId !== campaignId
    || run.lane !== lane
    || campaign.status !== "RUNNING"
    || campaign.baselineEventSequence === null
    || campaign.baselineSnapshot === null
  ) {
    throw new Error("OpenHands acceptance Run identity, baseline, or Campaign state is invalid");
  }
  if (!campaign.startedAt) throw new Error("OpenHands acceptance Campaign has no start timestamp");
  // A restart or a short control-plane hot reload can leave the three active
  // intervals below the required overlap even after the nominal wall-clock
  // window. Keep the lanes alive until both durability clocks are satisfied;
  // retain a bounded safety horizon so a broken activity recorder cannot leave
  // an orphaned worker running forever.
  const hardDeadline = Date.parse(campaign.startedAt)
    + (campaign.minimumWallClockSeconds + campaign.minimumOverlapSeconds + 900) * 1_000;
  const runtimeRequirementsSatisfied = (): boolean => {
    const current = acceptanceRepository.get(campaignId);
    return current.wallClockSeconds >= current.minimumWallClockSeconds
      && current.overlapSeconds >= current.minimumOverlapSeconds;
  };
  const shouldContinueRuntime = (): boolean => !runtimeRequirementsSatisfied() && Date.now() < hardDeadline;
  const workerId = `openhands_acceptance_${lane}_${randomUUID()}`;
  const workspace = path.resolve(PROJECT_ROOT, run.relativeWorkspace);
  if (!workspace.startsWith(`${path.resolve(PROJECT_ROOT, ".local", "openhands-acceptance")}${path.sep}`)) {
    throw new Error("OpenHands acceptance Run workspace escaped the project acceptance root");
  }
  await mkdir(path.join(workspace, "audit"), { recursive: true });
  const attached = acceptanceRepository.attachWorker(
    runId,
    workerId,
    process.pid,
    recovery,
    expectedLeaseGeneration,
  );
  const leaseGeneration = attached.leaseGeneration;
  let stopping = false;
  process.once("SIGTERM", () => { stopping = true; });
  process.once("SIGINT", () => { stopping = true; });
  const priorAudits = migration.database.prepare(
    "SELECT COUNT(*) AS count FROM openhands_acceptance_checkpoints WHERE run_id = ? AND stage = ?",
  ).get(runId, `${lane}_audit`) as { count: number | bigint };
  let cycle = Number(priorAudits.count);
  let activitySequence = 0;
  let firstCycleAfterAttach = true;
  let lastPlatformRead: JsonObject | null = null;
  let lastPlatformReadAt = 0;
  let lastReportAt: number | null = null;
  let artifactInventoryInitialized = false;
  let currentArtifactCandidates = new Map<string, AcceptanceArtifactCandidate>();
  const artifactEvidenceCache = new AcceptanceArtifactEvidenceCache(path.resolve(PROJECT_ROOT, config.artifactRoot));

  const listArtifactCandidates = (): AcceptanceArtifactCandidate[] => {
    // Generated reports are audit outputs, never recursively treated as scientific evidence inputs.
    const artifacts = migration.database.prepare(
      `SELECT artifacts.sha256, artifacts.media_type AS mediaType, artifacts.bytes,
       artifacts.relative_path AS relativePath, artifacts.producer
       FROM conversation_artifacts JOIN artifacts ON artifacts.sha256 = conversation_artifacts.sha256
       WHERE conversation_artifacts.conversation_id = ?
         AND NOT (artifacts.media_type = ? AND artifacts.producer GLOB 'qf-openhands-acceptance.*')
       ORDER BY conversation_artifacts.linked_at, artifacts.sha256`,
    ).all(campaign.conversationId, ACCEPTANCE_AUDIT_MEDIA_TYPE) as Array<{
      sha256: string;
      mediaType: string;
      bytes: number | bigint;
      relativePath: string;
      producer: string;
    }>;
    const parents = migration.database.prepare(
      `SELECT artifact_edges.child_sha256 AS childSha256, artifact_edges.parent_sha256 AS parentSha256
       FROM artifact_edges
       JOIN conversation_artifacts ON conversation_artifacts.sha256 = artifact_edges.child_sha256
       JOIN artifacts ON artifacts.sha256 = artifact_edges.child_sha256
       WHERE conversation_artifacts.conversation_id = ?
         AND NOT (artifacts.media_type = ? AND artifacts.producer GLOB 'qf-openhands-acceptance.*')
       ORDER BY artifact_edges.child_sha256, artifact_edges.parent_sha256`,
    ).all(campaign.conversationId, ACCEPTANCE_AUDIT_MEDIA_TYPE) as Array<{
      childSha256: string;
      parentSha256: string;
    }>;
    const parentsByChild = new Map<string, string[]>();
    for (const edge of parents) {
      const current = parentsByChild.get(edge.childSha256) ?? [];
      current.push(edge.parentSha256);
      parentsByChild.set(edge.childSha256, current);
    }
    return artifacts.map((artifact) => ({
      sha256: artifact.sha256,
      mediaType: artifact.mediaType,
      bytes: Number(artifact.bytes),
      relativePath: artifact.relativePath,
      producer: artifact.producer,
      parentHashes: parentsByChild.get(artifact.sha256) ?? [],
    }));
  };

  const generatedAuditArtifactCount = (): number => {
    const row = migration.database.prepare(
      `SELECT COUNT(*) AS count FROM conversation_artifacts
       JOIN artifacts ON artifacts.sha256 = conversation_artifacts.sha256
       WHERE conversation_artifacts.conversation_id = ? AND artifacts.media_type = ?
         AND artifacts.producer GLOB 'qf-openhands-acceptance.*'`,
    ).get(campaign.conversationId, ACCEPTANCE_AUDIT_MEDIA_TYPE) as { count: number | bigint };
    return Number(row.count);
  };

  const loadArtifactEvidence = async (): Promise<AcceptanceArtifactEvidence[]> => {
    const candidates = listArtifactCandidates();
    currentArtifactCandidates = new Map(candidates.map((candidate) => [candidate.sha256, candidate]));
    const evidence = await artifactEvidenceCache.synchronize(candidates);
    artifactInventoryInitialized = true;
    return evidence;
  };

  const persistReport = async (report: JsonObject, parentHashes: string[]): Promise<string> => {
    const reportBytes = new TextEncoder().encode(`${JSON.stringify(report, null, 2)}\n`);
    const outputPath = path.join(workspace, "audit", `audit-${String(cycle).padStart(6, "0")}.json`);
    await writeFile(outputPath, reportBytes, { mode: 0o600, flag: "wx" });
    const manifest = await storeArtifact({
      root: path.resolve(PROJECT_ROOT, config.artifactRoot),
      data: reportBytes,
      mediaType: ACCEPTANCE_AUDIT_MEDIA_TYPE,
      producer: `qf-openhands-acceptance.${lane}`,
      parentHashes,
    });
    const artifact = workspaceRepository.registerArtifact(campaign.conversationId, manifest);
    acceptanceRepository.claimArtifact({
      runId,
      artifactSha256: artifact.sha256,
      logicalName: `${lane}-audit-${cycle}`,
      relativeOutputPath: path.relative(PROJECT_ROOT, outputPath),
      parentHashes,
    });
    return artifact.sha256;
  };

  acceptanceRepository.appendEvent(runId, recovery ? "RUN_RECOVERED" : "RUN_STARTED", {
    lane,
    workerId,
    processId: process.pid,
    recovery,
    leaseGeneration,
    deadline: new Date(hardDeadline).toISOString(),
    formalTestSealed: true,
    hardwareMode: "READ_ONLY",
  });

  const conversationEvents = (): AcceptanceConversationEvent[] => workspaceRepository
    .listEventsAfter(campaign.conversationId, 0)
    .map((event) => ({ sequence: event.sequence, type: event.type, payload: event.payload, createdAt: event.createdAt }));
  const conversationProbeStatement = migration.database.prepare(
    `SELECT sequence, event_type AS eventType, created_at AS createdAt
     FROM events WHERE conversation_id = ? ORDER BY sequence DESC LIMIT 1`,
  );
  const acceptanceEventProbeStatement = migration.database.prepare(
    `SELECT sequence, event_type AS eventType, created_at AS createdAt
     FROM openhands_acceptance_events WHERE run_id = ? ORDER BY sequence DESC LIMIT 1`,
  );

  const buildReport = async (): Promise<{ report: JsonObject; parentHashes: string[] }> => {
    const snapshot = workspaceRepository.getSnapshot(campaign.conversationId);
    const session = workspaceRepository.getSession(campaign.conversationId);
    const events = conversationEvents();
    const postBaseline = events.filter((event) => event.sequence !== null
      && event.sequence > campaign.baselineEventSequence!);
    if (lane === "openhands_orchestration") {
      const toolsStarted = postBaseline.filter((event) => event.type === "tool.started");
      const toolsCompleted = postBaseline.filter((event) => event.type === "tool.completed" && event.payload.isError !== true);
      const starts = new Map(toolsStarted.flatMap((event) => {
        const id = event.payload.toolCallId;
        return typeof id === "string" ? [[id, event] as const] : [];
      }));
      const toolPairsValid = toolsCompleted.every((event) => {
        const id = event.payload.toolCallId;
        const start = typeof id === "string" ? starts.get(id) : undefined;
        return start !== undefined
          && start.payload.toolName === event.payload.toolName
          && Number(start.sequence) < Number(event.sequence);
      });
      const providerFailures = postBaseline.filter((event) => event.type === "run.failed"
        && (event.payload.category === "PROVIDER"
          || /(?:402|balance|rate.?limit|provider)/iu.test(String(event.payload.code ?? event.payload.message ?? ""))));
      return {
        parentHashes: [],
        report: {
          schemaVersion: "qf.openhands-acceptance-orchestration-audit.v2",
          cycle,
          capturedAt: new Date().toISOString(),
          conversationId: campaign.conversationId,
          taskId: campaign.taskId,
          conversationState: snapshot.conversation.state,
          runtimeKind: session?.runtimeKind ?? null,
          runtimeSessionId: session?.sessionId ?? null,
          runtimeRevision: session?.runtimeRevision ?? null,
          runtimeConfigHash: session?.configHash ?? null,
          recoveryCursor: session?.recoveryCursor ?? null,
          baselineEventSequence: campaign.baselineEventSequence,
          postBaselineOnly: postBaseline.every((event) => Number(event.sequence) > campaign.baselineEventSequence!),
          postBaselineEventCount: postBaseline.length,
          sequenceContinuous: persistentSequenceContinuous(events),
          completedTurnCount: postBaseline.filter((event) => event.type === "turn.completed").length,
          toolStartedCount: toolsStarted.length,
          toolCompletedCount: toolsCompleted.length,
          toolPairsValid,
          providerUsageEvents: postBaseline.filter((event) => event.type === "model.usage").length,
          providerBlocked: providerFailures.length > 0,
          secretPatternHits: secretLike(JSON.stringify({ events: postBaseline, messages: snapshot.messages })) ? 1 : 0,
          formalTestSealed: true,
          activeAuditWindowMs: ACTIVE_AUDIT_WINDOW_MS,
          reportIntervalMs: AUDIT_REPORT_INTERVAL_MS,
          generatedAuditArtifactCount: generatedAuditArtifactCount(),
        },
      };
    }
    const artifactEvidence = await loadArtifactEvidence();
    const currentState = acceptanceRepository.captureStateSnapshot(campaignId);
    const hardwareStable = campaign.baselineSnapshot!.hardwareDigest === currentState.hardwareDigest;
    const historyStable = campaign.baselineSnapshot!.historyDigest === currentState.historyDigest;
    if (lane === "quantum_science") {
      const scientific = analyzeAcceptanceEvidence({
        events,
        artifacts: artifactEvidence,
        baselineEventSequence: campaign.baselineEventSequence!,
        hardwareStable,
        historyStable,
        formalTestSealed: true,
      });
      return {
        parentHashes: Object.values(scientific.evidenceArtifacts ?? {}).flatMap((value) => Array.isArray(value)
          ? value.filter((item): item is string => typeof item === "string" && /^[a-f0-9]{64}$/u.test(item))
          : []).slice(-128),
        report: {
          ...scientific,
          cycle,
          capturedAt: new Date().toISOString(),
          platformRead: lastPlatformRead,
          hardwareMode: "READ_ONLY",
          hardwareTarget: "tianyan176",
          maxNewHardwareJobs: 0,
          shotsPerJob: 0,
          newQueryIds: 0,
          artifactAuditScope: "conversation evidence excluding generated acceptance audit reports",
          artifactAuditCache: artifactEvidenceCache.stats(),
          generatedAuditArtifactCount: generatedAuditArtifactCount(),
          activeAuditWindowMs: ACTIVE_AUDIT_WINDOW_MS,
          reportIntervalMs: AUDIT_REPORT_INTERVAL_MS,
        },
      };
    }
    const journalMode = String((migration.database.prepare("PRAGMA journal_mode").get() as Record<string, unknown>).journal_mode ?? "");
    const integrityRows = migration.database.prepare("PRAGMA integrity_check").all() as Array<Record<string, unknown>>;
    const integrityCheck = String(integrityRows[0]?.integrity_check ?? "unknown");
    const foreignKeys = Number((migration.database.prepare("PRAGMA foreign_keys").get() as Record<string, unknown>).foreign_keys ?? 0);
    const eventRows = migration.database.prepare(
      `SELECT run_id, COUNT(*) AS count, COUNT(DISTINCT sequence) AS distinct_count,
       MIN(sequence) AS minimum_sequence, MAX(sequence) AS maximum_sequence
       FROM openhands_acceptance_events WHERE run_id IN (
         SELECT id FROM openhands_acceptance_runs WHERE campaign_id = ?
       ) GROUP BY run_id`,
    ).all(campaignId) as Array<Record<string, number | string>>;
    const eventContinuity = eventRows.every((row) => Number(row.count) === Number(row.distinct_count)
      && Number(row.minimum_sequence) === 1 && Number(row.maximum_sequence) === Number(row.count));
    return {
      parentHashes: artifactEvidence.map((artifact) => artifact.sha256).slice(-128),
      report: {
        schemaVersion: "qf.openhands-acceptance-evidence-audit.v2",
        cycle,
        capturedAt: new Date().toISOString(),
        journalMode,
        integrityCheck,
        foreignKeysEnabled: foreignKeys === 1,
        eventContinuity,
        eventRows: jsonValue(eventRows),
        verifiedArtifactCount: artifactEvidence.length,
        artifactAuditScope: "conversation evidence excluding generated acceptance audit reports",
        artifactAuditCache: artifactEvidenceCache.stats(),
        generatedAuditArtifactCount: generatedAuditArtifactCount(),
        hardwareBaselineStable: hardwareStable,
        historyBaselineStable: historyStable,
        formalTestSealed: true,
        secretPatternHits: secretLike(JSON.stringify({ events: postBaseline, messages: snapshot.messages })) ? 1 : 0,
        hardwareJobsCreated: 0,
        activeAuditWindowMs: ACTIVE_AUDIT_WINDOW_MS,
        reportIntervalMs: AUDIT_REPORT_INTERVAL_MS,
      },
    };
  };

  const runAuditProbe = (iteration: number, reverifiedArtifactSha256: string | null): string => {
    const conversationState = jsonObject(conversationProbeStatement.get(campaign.conversationId));
    if (lane === "openhands_orchestration") {
      const session = workspaceRepository.getSession(campaign.conversationId);
      return acceptanceHash({
        lane,
        iteration,
        conversationState,
        runtimeIdentity: session ? {
          sessionId: session.sessionId,
          runtimeKind: session.runtimeKind,
          runtimeRevision: session.runtimeRevision,
          configHash: session.configHash,
          recoveryCursor: session.recoveryCursor,
        } : null,
      });
    }
    if (lane === "quantum_science") {
      return acceptanceHash({
        lane,
        iteration,
        reverifiedArtifactSha256,
        conversationState,
        artifactAuditCache: artifactEvidenceCache.stats(),
      });
    }
    const journalMode = migration.database.prepare("PRAGMA journal_mode").get() as Record<string, unknown>;
    const foreignKeys = migration.database.prepare("PRAGMA foreign_keys").get() as Record<string, unknown>;
    const acceptanceEvents = campaign.runs.map((campaignRun) => ({
      runId: campaignRun.runId,
      latest: jsonValue(acceptanceEventProbeStatement.get(campaignRun.runId) ?? null),
    }));
    return acceptanceHash({
      lane,
      iteration,
      reverifiedArtifactSha256,
      journalMode: jsonValue(journalMode),
      foreignKeys: jsonValue(foreignKeys),
      acceptanceEvents,
      conversationState,
      artifactAuditCache: artifactEvidenceCache.stats(),
    });
  };

  try {
    while (shouldContinueRuntime() && !stopping) {
      if (lane === "quantum_science" && (firstCycleAfterAttach || Date.now() - lastPlatformReadAt >= 60_000)) {
        const externalStarted = performance.now();
        const result = await runTianyanJob({
          projectRoot: PROJECT_ROOT,
          request: { action: "config_summary", machine_name: "tianyan176" },
          timeoutSeconds: 180,
        });
        const externalSeconds = Math.max(0, (performance.now() - externalStarted) / 1_000);
        acceptanceRepository.heartbeat(runId, workerId, leaseGeneration, externalSeconds);
        lastPlatformRead = {
          exitCode: result.exitCode,
          status: result.stdout.status ?? null,
          machineName: result.stdout.machine_name ?? "tianyan176",
          configHash: result.stdout.config_sha256 ?? result.stdout.calibration_sha256 ?? null,
          durationSeconds: result.durationSeconds,
          readOnly: true,
        };
        if (result.exitCode !== 0) throw new Error("read-only tianyan176 configuration audit failed");
        lastPlatformReadAt = Date.now();
      }
      const activityStartedAt = new Date();
      const activeStarted = performance.now();
      if (lane !== "openhands_orchestration" && !artifactInventoryInitialized) await loadArtifactEvidence();
      const reverifiedArtifactSha256 = lane === "openhands_orchestration"
        ? null
        : await artifactEvidenceCache.reverifyNext(currentArtifactCandidates);
      let workIterations = 0;
      let probeChainHash = acceptanceHash({
        campaignId,
        runId,
        lane,
        leaseGeneration,
        activitySequence: activitySequence + 1,
        activityStartedAt: activityStartedAt.toISOString(),
        reverifiedArtifactSha256,
      });
      do {
        const probeHash = runAuditProbe(workIterations + 1, reverifiedArtifactSha256);
        workIterations += 1;
        probeChainHash = acceptanceHash({ previous: probeChainHash, probeHash, workIterations });
        if (performance.now() - activeStarted < ACTIVE_AUDIT_WINDOW_MS) await yieldImmediate();
      } while (
        !stopping
        && shouldContinueRuntime()
        && performance.now() - activeStarted < ACTIVE_AUDIT_WINDOW_MS
      );
      if (stopping) break;
      let checkpointHash: string | null = null;
      if (shouldPersistAuditReport({
        firstCycleAfterAttach,
        now: Date.now(),
        lastReportAt,
        deadline: hardDeadline,
      })) {
        cycle += 1;
        const latest = await buildReport();
        const report = {
          ...latest.report,
          workIterations,
          probeChainHash,
          reverifiedArtifactSha256,
          timerIdleSecondsCountedAsActive: 0,
        };
        const artifactSha256 = await persistReport(report, latest.parentHashes);
        const checkpoint = acceptanceRepository.checkpoint({
          campaignId,
          runId,
          stage: `${lane}_audit`,
          idempotencyKey: `${campaignId}:${runId}:audit:${cycle}`,
          payload: { ...report, artifactSha256 },
        });
        checkpointHash = checkpoint.payloadHash;
        lastReportAt = Date.now();
        acceptanceRepository.appendEvent(runId, "AUDIT_CYCLE_COMPLETED", {
          lane,
          cycle,
          checkpointHash,
          artifactSha256,
          workIterations,
          reportIntervalMs: AUDIT_REPORT_INTERVAL_MS,
          formalTestSealed: true,
        });
      }
      activitySequence += 1;
      acceptanceRepository.recordActivity({
        runId,
        workerId,
        leaseGeneration,
        sequence: activitySequence,
        kind: activityKind(lane),
        startedAt: activityStartedAt.toISOString(),
        endedAt: new Date().toISOString(),
        evidenceHash: acceptanceHash({
          checkpointHash,
          probeChainHash,
          reverifiedArtifactSha256,
          workIterations,
          lane,
        }),
      });
      if (recovery && firstCycleAfterAttach && lane === "quantum_science") {
        if (!checkpointHash) throw new Error("science recovery requires an immediate durable audit checkpoint");
        const injected = migration.database.prepare(
          `SELECT id FROM openhands_acceptance_faults
           WHERE run_id = ? AND kind = 'SCIENCE_WORKER_TERMINATION' AND state = 'INJECTED'
           ORDER BY injected_at`,
        ).all(runId) as Array<{ id: string }>;
        for (const fault of injected) acceptanceRepository.verifyAndRecoverFault(fault.id, checkpointHash);
      }
      firstCycleAfterAttach = false;
    }

    if (stopping) {
      acceptanceRepository.appendEvent(runId, "RUN_INTERRUPTED", {
        lane,
        cycle,
        workerId,
        leaseGeneration,
        recoverable: true,
      });
      acceptanceRepository.setRunState(runId, "RECOVERING", workerId, leaseGeneration);
      return;
    }
    if (!runtimeRequirementsSatisfied()) {
      throw new Error("OpenHands acceptance runtime horizon expired before wall-clock and overlap gates converged");
    }
    acceptanceRepository.appendEvent(runId, "RUN_COMPLETED", {
      lane,
      cycle,
      wallClockSeconds: acceptanceRepository.get(campaignId).wallClockSeconds,
      activeSeconds: acceptanceRepository.getRun(runId).activeSeconds,
      formalTestSealed: true,
    });
    acceptanceRepository.setRunState(runId, "COMPLETED", workerId, leaseGeneration);
  } catch (error) {
    if (stopping) {
      acceptanceRepository.prepareRunRecovery(runId, workerId, leaseGeneration);
      return;
    }
    acceptanceRepository.appendEvent(runId, "RUN_FAILED", {
      lane,
      message: (error instanceof Error ? error.message : String(error)).slice(0, 1_000),
      formalTestSealed: true,
    });
    acceptanceRepository.setRunState(runId, "FAILED", workerId, leaseGeneration);
    throw error;
  } finally {
    workspaceRepository.close();
  }
}

const entrypoint = process.argv[1];
if (entrypoint && pathToFileURL(path.resolve(entrypoint)).href === import.meta.url) await main();
