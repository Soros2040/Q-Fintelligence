import { createHash, randomUUID } from "node:crypto";
import type { DatabaseSync } from "node:sqlite";

import type { JsonObject } from "@q-fintelligence/contracts";

export const OPENHANDS_ACCEPTANCE_LANES = [
  "openhands_orchestration",
  "quantum_science",
  "evidence_audit",
] as const;

export type OpenHandsAcceptanceLane = (typeof OPENHANDS_ACCEPTANCE_LANES)[number];
export type OpenHandsAcceptanceState = "CREATED" | "RUNNING" | "RECOVERING" | "COMPLETED" | "FAILED" | "BLOCKED" | "CANCELLED";
export type OpenHandsAcceptanceActivityKind =
  | "OPENHANDS_STATE_AUDIT"
  | "QUANTUM_REPRODUCIBILITY_AUDIT"
  | "EVIDENCE_INTEGRITY_AUDIT";

type SqlRow = Record<string, string | number | bigint | Uint8Array | null>;

function timestamp(): string {
  return new Date().toISOString();
}

function text(row: SqlRow, key: string): string {
  const value = row[key];
  if (typeof value !== "string") throw new Error(`expected text column ${key}`);
  return value;
}

function optionalText(row: SqlRow, key: string): string | null {
  const value = row[key];
  if (value === null || value === undefined) return null;
  if (typeof value !== "string") throw new Error(`expected nullable text column ${key}`);
  return value;
}

function numberValue(row: SqlRow, key: string): number {
  const value = row[key];
  if (typeof value === "number") return value;
  if (typeof value === "bigint") return Number(value);
  throw new Error(`expected number column ${key}`);
}

function optionalNumber(row: SqlRow, key: string): number | null {
  const value = row[key];
  if (value === null || value === undefined) return null;
  if (typeof value === "number") return value;
  if (typeof value === "bigint") return Number(value);
  throw new Error(`expected nullable number column ${key}`);
}

function jsonObjectFromText(value: string | null): JsonObject | null {
  if (value === null) return null;
  const parsed = JSON.parse(value) as unknown;
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error("stored acceptance JSON must be an object");
  }
  return parsed as JsonObject;
}

function intervalIntersectionSeconds(
  rows: Array<{ lane: OpenHandsAcceptanceLane; startedAt: string; endedAt: string }>,
): number {
  const byLane = new Map<OpenHandsAcceptanceLane, Array<[number, number]>>(
    OPENHANDS_ACCEPTANCE_LANES.map((lane) => [lane, []]),
  );
  for (const row of rows) {
    const started = Date.parse(row.startedAt);
    const ended = Date.parse(row.endedAt);
    if (!Number.isFinite(started) || !Number.isFinite(ended) || ended <= started) continue;
    byLane.get(row.lane)!.push([started, ended]);
  }
  const merge = (intervals: Array<[number, number]>): Array<[number, number]> => {
    const sorted = intervals.toSorted((left, right) => left[0] - right[0]);
    const merged: Array<[number, number]> = [];
    for (const interval of sorted) {
      const previous = merged.at(-1);
      if (!previous || interval[0] > previous[1]) merged.push([...interval]);
      else previous[1] = Math.max(previous[1], interval[1]);
    }
    return merged;
  };
  const lanes = OPENHANDS_ACCEPTANCE_LANES.map((lane) => merge(byLane.get(lane)!));
  if (lanes.some((intervals) => intervals.length === 0)) return 0;
  let intersection = lanes[0]!;
  for (const next of lanes.slice(1)) {
    const combined: Array<[number, number]> = [];
    let leftIndex = 0;
    let rightIndex = 0;
    while (leftIndex < intersection.length && rightIndex < next.length) {
      const left = intersection[leftIndex]!;
      const right = next[rightIndex]!;
      const start = Math.max(left[0], right[0]);
      const end = Math.min(left[1], right[1]);
      if (end > start) combined.push([start, end]);
      if (left[1] < right[1]) leftIndex += 1;
      else rightIndex += 1;
    }
    intersection = combined;
  }
  return intersection.reduce((sum, [started, ended]) => sum + (ended - started) / 1_000, 0);
}

function stableJson(value: unknown): string {
  if (value === null || typeof value === "boolean" || typeof value === "string") return JSON.stringify(value);
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new Error("non-finite JSON number");
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>)
      .filter(([, entry]) => entry !== undefined)
      .sort(([left], [right]) => left.localeCompare(right));
    return `{${entries.map(([key, entry]) => `${JSON.stringify(key)}:${stableJson(entry)}`).join(",")}}`;
  }
  throw new Error("unsupported JSON value");
}

export function acceptanceHash(value: unknown): string {
  return createHash("sha256").update(stableJson(value)).digest("hex");
}

export interface OpenHandsAcceptanceRun {
  runId: string;
  campaignId: string;
  lane: OpenHandsAcceptanceLane;
  status: OpenHandsAcceptanceState;
  relativeWorkspace: string;
  workerId: string | null;
  processId: number | null;
  leaseGeneration: number;
  leaseExpiresAt: string | null;
  recoveryCount: number;
  activeSeconds: number;
  externalWaitSeconds: number;
  startedAt: string | null;
  completedAt: string | null;
  lastHeartbeatAt: string | null;
}

export interface OpenHandsAcceptanceCampaign {
  campaignId: string;
  projectId: string;
  conversationId: string;
  taskId: string;
  status: OpenHandsAcceptanceState;
  fixedProviderModel: "deepseek/deepseek-v4-pro";
  authorizationHash: string;
  systemPromptHash: string;
  runtimeConfigHash: string | null;
  minimumWallClockSeconds: number;
  minimumOverlapSeconds: number;
  hardwareMode: "READ_ONLY";
  hardwareTarget: "tianyan176";
  maxNewHardwareJobs: 0;
  shotsPerJob: 0;
  formalTestSealed: true;
  baselineEventSequence: number | null;
  baselineSnapshotHash: string | null;
  baselineSnapshot: JsonObject | null;
  providerPromptTokens: number;
  providerCompletionTokens: number;
  providerCalls: number;
  hardwareJobsCreated: 0;
  wallClockSeconds: number;
  overlapSeconds: number;
  startedAt: string | null;
  completedAt: string | null;
  frozenAt: string | null;
  runs: OpenHandsAcceptanceRun[];
}

export type OpenHandsAcceptanceFaultState = "PLANNED" | "INJECTED" | "RECOVERED" | "FAILED";

export interface OpenHandsAcceptanceFault {
  faultId: string;
  campaignId: string;
  runId: string;
  kind: "OPENHANDS_PROCESS_TERMINATION" | "SCIENCE_WORKER_TERMINATION";
  targetProcessId: number;
  targetProcessKey: string;
  state: OpenHandsAcceptanceFaultState;
  beforeCheckpointHash: string;
  afterCheckpointHash: string | null;
  verification: JsonObject | null;
}

export class OpenHandsAcceptanceRepository {
  constructor(private readonly database: DatabaseSync) {}

  private jsonRows(sql: string, ...parameters: Array<string | number>): JsonObject[] {
    return (this.database.prepare(sql).all(...parameters) as SqlRow[]).map((row) => Object.fromEntries(
      Object.entries(row).map(([key, value]) => [key, typeof value === "bigint" ? Number(value) : value]),
    ) as JsonObject);
  }

  private usageAfterSequence(conversationId: string, sequence: number): {
    calls: number;
    promptTokens: number;
    completionTokens: number;
  } {
    const rows = this.database.prepare(
      `SELECT payload_json FROM events
       WHERE conversation_id = ? AND sequence > ? AND event_type = 'model.usage'
       ORDER BY sequence`,
    ).all(conversationId, sequence) as SqlRow[];
    let promptTokens = 0;
    let completionTokens = 0;
    for (const row of rows) {
      const parsed = JSON.parse(text(row, "payload_json")) as Record<string, unknown>;
      const prompt = parsed.input;
      const completion = parsed.output;
      if (Number.isSafeInteger(prompt) && Number(prompt) >= 0) promptTokens += Number(prompt);
      if (Number.isSafeInteger(completion) && Number(completion) >= 0) completionTokens += Number(completion);
    }
    return { calls: rows.length, promptTokens, completionTokens };
  }

  captureStateSnapshot(campaignId: string, checkpointHash: string | null = null): JsonObject {
    const campaign = this.get(campaignId);
    const latestEvent = this.database.prepare(
      "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM events WHERE conversation_id = ?",
    ).get(campaign.conversationId) as SqlRow;
    const latestEventSequence = numberValue(latestEvent, "sequence");
    const session = this.database.prepare(
      `SELECT session_id, runtime_kind, runtime_revision, config_hash, recovery_cursor,
       provider, model_id, prompt_hash, lifecycle_state
       FROM agent_sessions WHERE conversation_id = ?`,
    ).get(campaign.conversationId) as SqlRow | undefined;
    const artifacts = this.jsonRows(
      `SELECT conversation_artifacts.sha256, artifacts.media_type AS mediaType,
       artifacts.bytes, artifacts.producer, artifacts.created_at AS createdAt
       FROM conversation_artifacts JOIN artifacts ON artifacts.sha256 = conversation_artifacts.sha256
       WHERE conversation_artifacts.conversation_id = ? ORDER BY conversation_artifacts.sha256`,
      campaign.conversationId,
    );
    const artifactEdges = this.jsonRows(
      `SELECT artifact_edges.child_sha256 AS childSha256, artifact_edges.parent_sha256 AS parentSha256
       FROM artifact_edges JOIN conversation_artifacts
         ON conversation_artifacts.sha256 = artifact_edges.child_sha256
       WHERE conversation_artifacts.conversation_id = ?
       ORDER BY artifact_edges.child_sha256, artifact_edges.parent_sha256`,
      campaign.conversationId,
    );
    const toolInbox = this.jsonRows(
      `SELECT tool_call_id AS toolCallId, tool_name AS toolName, request_hash AS requestHash,
       status, result_json AS resultJson, error_json AS errorJson
       FROM openhands_tool_call_inbox WHERE conversation_id = ?
       ORDER BY runtime_session_id, tool_call_id`,
      campaign.conversationId,
    );
    const approvals = this.jsonRows(
      `SELECT id, action, subject_hash AS subjectHash, status, requested_at AS requestedAt,
       decided_at AS decidedAt FROM approval_requests WHERE conversation_id = ? ORDER BY id`,
      campaign.conversationId,
    );
    const hardware = {
      legacyQuantumJobs: this.jsonRows(
        `SELECT id, campaign_id AS campaignId, backend, target_type AS targetType,
         circuit_hash AS circuitHash, shots, query_id AS queryId, terminal_status AS terminalStatus
         FROM quantum_jobs ORDER BY id`,
      ),
      p15Batches: this.jsonRows(
        `SELECT id, campaign_id AS campaignId, generation_id AS generationId, lease_key AS leaseKey,
         request_sha256 AS requestSha256, status, query_ids_json AS queryIdsJson
         FROM p15_hardware_batches ORDER BY id`,
      ),
      p16Batches: this.jsonRows(
        `SELECT id, campaign_id AS campaignId, generation_id AS generationId,
         idempotency_key AS idempotencyKey, request_sha256 AS requestSha256,
         status, query_ids_json AS queryIdsJson, submit_attempts AS submitAttempts
         FROM p16_hardware_batches ORDER BY id`,
      ),
    } satisfies JsonObject;
    const history = {
      p04AndP07Campaigns: this.jsonRows(
        `SELECT id, status, current_stage AS stage, highest_chain_level AS highestChainLevel,
         blocker_category AS blockerCategory, blocker_artifact_sha256 AS blockerArtifactSha256
         FROM campaigns WHERE id LIKE 'p04_%' OR id LIKE 'p07_%'
         ORDER BY id`,
      ),
      p15Campaigns: this.jsonRows(
        "SELECT id, status, stage, checkpoint_json AS checkpointJson FROM p15_campaigns ORDER BY id",
      ),
      p16Campaigns: this.jsonRows(
        "SELECT id, status, stage, checkpoint_json AS checkpointJson FROM p16_hardware_campaigns ORDER BY id",
      ),
    } satisfies JsonObject;
    const checkpoints = this.jsonRows(
      `SELECT idempotency_key AS idempotencyKey, payload_hash AS payloadHash, run_id AS runId, stage
       FROM openhands_acceptance_checkpoints WHERE campaign_id = ? ORDER BY idempotency_key`,
      campaignId,
    );
    const usage = this.usageAfterSequence(campaign.conversationId, campaign.baselineEventSequence ?? latestEventSequence);
    const snapshot: JsonObject = {
      schemaVersion: "qf.openhands-acceptance-state-snapshot.v2",
      campaignId,
      conversationId: campaign.conversationId,
      latestEventSequence,
      session: session ? {
        sessionId: text(session, "session_id"),
        runtimeKind: text(session, "runtime_kind"),
        runtimeRevision: text(session, "runtime_revision"),
        configHash: text(session, "config_hash"),
        recoveryCursor: numberValue(session, "recovery_cursor"),
        provider: text(session, "provider"),
        modelId: text(session, "model_id"),
        promptHash: text(session, "prompt_hash"),
        lifecycleState: text(session, "lifecycle_state"),
      } : null,
      providerUsageDelta: usage,
      artifacts,
      artifactEdges,
      artifactDigest: acceptanceHash({ artifacts, artifactEdges }),
      toolInbox,
      toolInboxDigest: acceptanceHash(toolInbox),
      approvals,
      approvalsDigest: acceptanceHash(approvals),
      hardware,
      hardwareDigest: acceptanceHash(hardware),
      history,
      historyDigest: acceptanceHash(history),
      checkpoints,
      checkpointDigest: acceptanceHash(checkpoints),
      checkpointHash,
      capturedAt: timestamp(),
    };
    return snapshot;
  }

  private transaction<T>(operation: () => T): T {
    this.database.exec("BEGIN IMMEDIATE");
    try {
      const result = operation();
      this.database.exec("COMMIT");
      return result;
    } catch (error) {
      this.database.exec("ROLLBACK");
      throw error;
    }
  }

  create(input: {
    projectId: string;
    conversationId: string;
    taskId: string;
    authorizationHash: string;
    systemPromptHash: string;
    runtimeConfigHash?: string | null;
    minimumWallClockSeconds: number;
    minimumOverlapSeconds: number;
  }): OpenHandsAcceptanceCampaign {
    if (input.minimumWallClockSeconds < 7_200 || input.minimumOverlapSeconds < 6_900) {
      throw new Error("OpenHands acceptance duration is below the frozen gate");
    }
    const campaignId = `openhands_acceptance_${randomUUID()}`;
    const createdAt = timestamp();
    this.transaction(() => {
      this.database.prepare(
        `INSERT INTO openhands_acceptance_campaigns(
          id, project_id, conversation_id, task_id, status, fixed_provider_model,
          authorization_hash, system_prompt_hash, runtime_config_hash,
          minimum_wall_clock_seconds, minimum_overlap_seconds,
          hardware_mode, hardware_target, max_new_hardware_jobs, shots_per_job,
          formal_test_sealed, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'CREATED', 'deepseek/deepseek-v4-pro', ?, ?, ?, ?, ?, 'READ_ONLY', 'tianyan176', 0, 0, 1, ?, ?)`,
      ).run(
        campaignId,
        input.projectId,
        input.conversationId,
        input.taskId,
        input.authorizationHash,
        input.systemPromptHash,
        input.runtimeConfigHash ?? null,
        input.minimumWallClockSeconds,
        input.minimumOverlapSeconds,
        createdAt,
        createdAt,
      );
      for (const lane of OPENHANDS_ACCEPTANCE_LANES) {
        this.database.prepare(
          `INSERT INTO openhands_acceptance_runs(
            id, campaign_id, lane, status, relative_workspace, created_at, updated_at
          ) VALUES (?, ?, ?, 'CREATED', ?, ?, ?)`,
        ).run(
          `openhands_acceptance_run_${randomUUID()}`,
          campaignId,
          lane,
          `.local/openhands-acceptance/${campaignId}/${lane}`,
          createdAt,
          createdAt,
        );
      }
    });
    this.checkpoint({
      campaignId,
      stage: "created",
      idempotencyKey: `${campaignId}:created`,
      payload: {
        schemaVersion: "qf.openhands-acceptance-created.v1",
        fixedProviderModel: "deepseek/deepseek-v4-pro",
        minimumWallClockSeconds: input.minimumWallClockSeconds,
        minimumOverlapSeconds: input.minimumOverlapSeconds,
        lanes: [...OPENHANDS_ACCEPTANCE_LANES],
        hardwareMode: "READ_ONLY",
        hardwareTarget: "tianyan176",
        maxNewHardwareJobs: 0,
        shotsPerJob: 0,
        formalTestSealed: true,
        authorizationHash: input.authorizationHash,
        systemPromptHash: input.systemPromptHash,
        runtimeConfigHash: input.runtimeConfigHash ?? null,
      },
    });
    return this.get(campaignId);
  }

  get(campaignId: string): OpenHandsAcceptanceCampaign {
    const row = this.database.prepare("SELECT * FROM openhands_acceptance_campaigns WHERE id = ?")
      .get(campaignId) as SqlRow | undefined;
    if (!row) throw new Error(`OpenHands acceptance Campaign ${campaignId} was not found`);
    const runs = this.listRuns(campaignId);
    const startedAt = optionalText(row, "started_at");
    const completedAt = optionalText(row, "completed_at");
    const wallClockSeconds = startedAt
      ? Math.max(0, (Date.parse(completedAt ?? new Date().toISOString()) - Date.parse(startedAt)) / 1_000)
      : 0;
    const activityRows = this.database.prepare(
      `SELECT openhands_acceptance_runs.lane, openhands_acceptance_activity_intervals.started_at,
       openhands_acceptance_activity_intervals.ended_at
       FROM openhands_acceptance_activity_intervals
       JOIN openhands_acceptance_runs ON openhands_acceptance_runs.id = openhands_acceptance_activity_intervals.run_id
       WHERE openhands_acceptance_runs.campaign_id = ?
       ORDER BY openhands_acceptance_activity_intervals.started_at`,
    ).all(campaignId) as SqlRow[];
    const overlapSeconds = intervalIntersectionSeconds(activityRows.map((activityRow) => ({
      lane: text(activityRow, "lane") as OpenHandsAcceptanceLane,
      startedAt: text(activityRow, "started_at"),
      endedAt: text(activityRow, "ended_at"),
    })));
    return {
      campaignId: text(row, "id"),
      projectId: text(row, "project_id"),
      conversationId: text(row, "conversation_id"),
      taskId: text(row, "task_id"),
      status: text(row, "status") as OpenHandsAcceptanceState,
      fixedProviderModel: "deepseek/deepseek-v4-pro",
      authorizationHash: text(row, "authorization_hash"),
      systemPromptHash: text(row, "system_prompt_hash"),
      runtimeConfigHash: optionalText(row, "runtime_config_hash"),
      minimumWallClockSeconds: numberValue(row, "minimum_wall_clock_seconds"),
      minimumOverlapSeconds: numberValue(row, "minimum_overlap_seconds"),
      hardwareMode: "READ_ONLY",
      hardwareTarget: "tianyan176",
      maxNewHardwareJobs: 0,
      shotsPerJob: 0,
      formalTestSealed: true,
      baselineEventSequence: optionalNumber(row, "baseline_event_sequence"),
      baselineSnapshotHash: optionalText(row, "baseline_snapshot_hash"),
      baselineSnapshot: jsonObjectFromText(optionalText(row, "baseline_snapshot_json")),
      providerPromptTokens: numberValue(row, "provider_prompt_tokens"),
      providerCompletionTokens: numberValue(row, "provider_completion_tokens"),
      providerCalls: numberValue(row, "provider_calls"),
      hardwareJobsCreated: 0,
      wallClockSeconds,
      overlapSeconds,
      startedAt,
      completedAt,
      frozenAt: optionalText(row, "frozen_at"),
      runs,
    };
  }

  list(): OpenHandsAcceptanceCampaign[] {
    return (this.database.prepare("SELECT id FROM openhands_acceptance_campaigns ORDER BY created_at DESC").all() as SqlRow[])
      .map((row) => this.get(text(row, "id")));
  }

  listRuns(campaignId: string): OpenHandsAcceptanceRun[] {
    return (this.database.prepare(
      "SELECT * FROM openhands_acceptance_runs WHERE campaign_id = ? ORDER BY created_at, lane",
    ).all(campaignId) as SqlRow[]).map((row) => ({
      runId: text(row, "id"),
      campaignId: text(row, "campaign_id"),
      lane: text(row, "lane") as OpenHandsAcceptanceLane,
      status: text(row, "status") as OpenHandsAcceptanceState,
      relativeWorkspace: text(row, "relative_workspace"),
      workerId: optionalText(row, "worker_id"),
      processId: row.process_id === null ? null : numberValue(row, "process_id"),
      leaseGeneration: numberValue(row, "lease_generation"),
      leaseExpiresAt: optionalText(row, "lease_expires_at"),
      recoveryCount: numberValue(row, "recovery_count"),
      activeSeconds: numberValue(row, "active_seconds"),
      externalWaitSeconds: numberValue(row, "external_wait_seconds"),
      startedAt: optionalText(row, "started_at"),
      completedAt: optionalText(row, "completed_at"),
      lastHeartbeatAt: optionalText(row, "last_heartbeat_at"),
    }));
  }

  start(campaignId: string): OpenHandsAcceptanceCampaign {
    const campaign = this.get(campaignId);
    if (campaign.status !== "CREATED" || campaign.baselineEventSequence !== null) {
      throw new Error(`Campaign ${campaignId} cannot start from ${campaign.status}`);
    }
    const baseline = this.captureStateSnapshot(campaignId);
    const baselineEventSequence = Number(baseline.latestEventSequence);
    if (!Number.isSafeInteger(baselineEventSequence) || baselineEventSequence < 1) {
      throw new Error("OpenHands acceptance baseline requires persistent preflight events");
    }
    const baselineHash = acceptanceHash(baseline);
    const at = timestamp();
    this.transaction(() => {
      const updated = this.database.prepare(
        `UPDATE openhands_acceptance_campaigns SET status = 'RUNNING', started_at = ?,
         baseline_event_sequence = ?, baseline_snapshot_hash = ?, baseline_snapshot_json = ?,
         provider_prompt_tokens = 0, provider_completion_tokens = 0, provider_calls = 0, updated_at = ?
         WHERE id = ? AND status = 'CREATED' AND baseline_event_sequence IS NULL`,
      ).run(at, baselineEventSequence, baselineHash, stableJson(baseline), at, campaignId);
      if (updated.changes !== 1) throw new Error("OpenHands acceptance baseline CAS was rejected");
      const runsUpdated = this.database.prepare(
        `UPDATE openhands_acceptance_runs SET status = 'RUNNING', started_at = ?, completed_at = NULL,
         worker_id = NULL, process_id = NULL, lease_expires_at = NULL, updated_at = ?
         WHERE campaign_id = ? AND status = 'CREATED'`,
      ).run(at, at, campaignId);
      if (runsUpdated.changes !== OPENHANDS_ACCEPTANCE_LANES.length) {
        throw new Error("OpenHands acceptance start did not cover every lane");
      }
      this.checkpoint({
        campaignId,
        stage: "start_baseline",
        idempotencyKey: `${campaignId}:start-baseline`,
        payload: {
          schemaVersion: "qf.openhands-acceptance-start-baseline.v2",
          baselineEventSequence,
          baselineHash,
          baseline,
          preStartCanaryExcluded: true,
        },
      });
    });
    return this.get(campaignId);
  }

  compensateStart(campaignId: string): OpenHandsAcceptanceCampaign {
    const at = timestamp();
    this.transaction(() => {
      const campaign = this.database.prepare(
        "SELECT status, baseline_event_sequence FROM openhands_acceptance_campaigns WHERE id = ?",
      ).get(campaignId) as SqlRow | undefined;
      if (!campaign) throw new Error(`OpenHands acceptance Campaign ${campaignId} was not found`);
      const status = text(campaign, "status");
      if (this.startupComplete(campaignId)) {
        throw new Error(`Campaign ${campaignId} completed startup and cannot be compensated`);
      }
      if ((status !== "RUNNING" && status !== "RECOVERING") || campaign.baseline_event_sequence === null) {
        throw new Error(`Campaign ${campaignId} start compensation was rejected from ${status}`);
      }
      const runState = this.database.prepare(
        `SELECT COUNT(*) AS count,
         SUM(CASE WHEN status IN ('RUNNING','RECOVERING','FAILED') THEN 1 ELSE 0 END) AS recoverable_count
         FROM openhands_acceptance_runs WHERE campaign_id = ?`,
      ).get(campaignId) as SqlRow;
      if (
        numberValue(runState, "count") !== OPENHANDS_ACCEPTANCE_LANES.length
        || numberValue(runState, "recoverable_count") !== OPENHANDS_ACCEPTANCE_LANES.length
      ) {
        throw new Error("Campaign start compensation found a terminal or unprepared Run");
      }
      if (status === "RUNNING") {
        const campaignUpdated = this.database.prepare(
          `UPDATE openhands_acceptance_campaigns SET status = 'RECOVERING', updated_at = ?
           WHERE id = ? AND status = 'RUNNING'`,
        ).run(at, campaignId);
        if (campaignUpdated.changes !== 1) throw new Error("Campaign start compensation CAS was rejected");
      }
      const runsUpdated = this.database.prepare(
        `UPDATE openhands_acceptance_runs SET status = 'RECOVERING', lease_expires_at = NULL,
         completed_at = NULL, updated_at = ?
         WHERE campaign_id = ? AND status IN ('RUNNING','RECOVERING','FAILED')`,
      ).run(at, campaignId);
      if (runsUpdated.changes !== OPENHANDS_ACCEPTANCE_LANES.length) {
        throw new Error("Campaign start compensation did not cover every lane");
      }
    });
    return this.get(campaignId);
  }

  resumeStart(campaignId: string): OpenHandsAcceptanceCampaign {
    const campaign = this.get(campaignId);
    if (
      this.startupComplete(campaignId)
      || campaign.status !== "RECOVERING"
      || campaign.baselineEventSequence === null
      || campaign.baselineSnapshot === null
      || campaign.runs.length !== OPENHANDS_ACCEPTANCE_LANES.length
      || campaign.runs.some((run) => run.status !== "RECOVERING")
    ) {
      throw new Error(`Campaign ${campaignId} startup recovery precondition was rejected`);
    }
    const result = this.database.prepare(
      `UPDATE openhands_acceptance_campaigns SET status = 'RUNNING', updated_at = ?
       WHERE id = ? AND status = 'RECOVERING' AND baseline_event_sequence IS NOT NULL`,
    ).run(timestamp(), campaignId);
    if (result.changes !== 1) throw new Error("Campaign startup recovery CAS was rejected");
    return this.get(campaignId);
  }

  startupComplete(campaignId: string): boolean {
    this.get(campaignId);
    return this.database.prepare(
      "SELECT 1 FROM openhands_acceptance_checkpoints WHERE campaign_id = ? AND idempotency_key = ?",
    ).get(campaignId, `${campaignId}:startup-complete`) !== undefined;
  }

  completeStart(campaignId: string): OpenHandsAcceptanceCampaign {
    if (this.startupComplete(campaignId)) return this.get(campaignId);
    this.transaction(() => {
      const campaign = this.get(campaignId);
      if (
        campaign.status !== "RUNNING"
        || campaign.baselineEventSequence === null
        || campaign.baselineSnapshot === null
        || campaign.runs.length !== OPENHANDS_ACCEPTANCE_LANES.length
        || campaign.runs.some((run) => (
          run.status !== "RUNNING"
          || run.workerId === null
          || run.processId === null
          || run.leaseGeneration < 1
        ))
      ) {
        throw new Error(`Campaign ${campaignId} startup completion precondition was rejected`);
      }
      this.checkpoint({
        campaignId,
        stage: "startup_complete",
        idempotencyKey: `${campaignId}:startup-complete`,
        payload: {
          schemaVersion: "qf.openhands-acceptance-startup-complete.v1",
          campaignId,
          lanes: campaign.runs
            .map((run) => ({ lane: run.lane, runId: run.runId }))
            .sort((left, right) => left.lane.localeCompare(right.lane)),
        },
      });
    });
    return this.get(campaignId);
  }

  attachWorker(
    runId: string,
    workerId: string,
    processId: number,
    recovery: boolean,
    expectedLeaseGeneration: number,
  ): OpenHandsAcceptanceRun {
    if (!Number.isSafeInteger(expectedLeaseGeneration) || expectedLeaseGeneration < 0) {
      throw new Error("expected acceptance lease generation is invalid");
    }
    const at = timestamp();
    const expiresAt = new Date(Date.now() + 60_000).toISOString();
    const result = this.database.prepare(
      `UPDATE openhands_acceptance_runs SET status = 'RUNNING', worker_id = ?, process_id = ?,
       lease_generation = lease_generation + 1, lease_expires_at = ?,
       recovery_count = recovery_count + ?, started_at = COALESCE(started_at, ?),
       completed_at = NULL, last_heartbeat_at = ?, updated_at = ?
       WHERE id = ? AND lease_generation = ? AND status = ?
         AND (? = 1 OR worker_id IS NULL)`,
    ).run(
      workerId,
      processId,
      expiresAt,
      recovery ? 1 : 0,
      at,
      at,
      at,
      runId,
      expectedLeaseGeneration,
      recovery ? "RECOVERING" : "RUNNING",
      recovery ? 1 : 0,
    );
    if (result.changes !== 1) throw new Error(`OpenHands acceptance Run ${runId} lease CAS was rejected`);
    return this.getRun(runId);
  }

  heartbeat(runId: string, workerId: string, leaseGeneration: number, externalWaitDeltaSeconds = 0): void {
    if (!Number.isSafeInteger(leaseGeneration) || leaseGeneration < 1
      || !Number.isFinite(externalWaitDeltaSeconds) || externalWaitDeltaSeconds < 0 || externalWaitDeltaSeconds > 300) {
      throw new Error("Run heartbeat identity or external wait delta is invalid");
    }
    const at = timestamp();
    const expiresAt = new Date(Date.now() + 60_000).toISOString();
    const result = this.database.prepare(
      `UPDATE openhands_acceptance_runs SET last_heartbeat_at = ?, lease_expires_at = ?,
       external_wait_seconds = external_wait_seconds + ?, updated_at = ?
       WHERE id = ? AND worker_id = ? AND lease_generation = ? AND status = 'RUNNING'`,
    ).run(at, expiresAt, externalWaitDeltaSeconds, at, runId, workerId, leaseGeneration);
    if (result.changes !== 1) throw new Error("OpenHands acceptance heartbeat identity was rejected");
  }

  recordActivity(input: {
    runId: string;
    workerId: string;
    leaseGeneration: number;
    sequence: number;
    kind: OpenHandsAcceptanceActivityKind;
    startedAt: string;
    endedAt: string;
    evidenceHash: string;
  }): void {
    const started = Date.parse(input.startedAt);
    const ended = Date.parse(input.endedAt);
    const durationSeconds = (ended - started) / 1_000;
    if (
      !Number.isSafeInteger(input.leaseGeneration) || input.leaseGeneration < 1
      || !Number.isSafeInteger(input.sequence) || input.sequence < 1
      || !Number.isFinite(durationSeconds) || durationSeconds <= 0 || durationSeconds > 30
      || ended > Date.now() + 1_000 || started < Date.now() - 60_000
      || !/^[a-f0-9]{64}$/u.test(input.evidenceHash)
    ) {
      throw new Error("acceptance activity interval failed the real-time bounded-work gate");
    }
    const at = timestamp();
    this.transaction(() => {
      const lease = this.database.prepare(
        `SELECT 1 FROM openhands_acceptance_runs WHERE id = ? AND worker_id = ?
         AND lease_generation = ? AND status = 'RUNNING' AND lease_expires_at >= ?`,
      ).get(input.runId, input.workerId, input.leaseGeneration, input.endedAt);
      if (!lease) throw new Error("acceptance activity lease identity was rejected");
      this.database.prepare(
        `INSERT INTO openhands_acceptance_activity_intervals(
          run_id, lease_generation, sequence, worker_id, activity_kind,
          started_at, ended_at, active_seconds, evidence_hash, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      ).run(
        input.runId,
        input.leaseGeneration,
        input.sequence,
        input.workerId,
        input.kind,
        input.startedAt,
        input.endedAt,
        durationSeconds,
        input.evidenceHash,
        at,
      );
      const updated = this.database.prepare(
        `UPDATE openhands_acceptance_runs SET active_seconds = active_seconds + ?,
         last_heartbeat_at = ?, lease_expires_at = ?, updated_at = ?
         WHERE id = ? AND worker_id = ? AND lease_generation = ? AND status = 'RUNNING'`,
      ).run(
        durationSeconds,
        at,
        new Date(Date.now() + 60_000).toISOString(),
        at,
        input.runId,
        input.workerId,
        input.leaseGeneration,
      );
      if (updated.changes !== 1) throw new Error("acceptance activity lease changed during commit");
    });
  }

  prepareRunRecovery(runId: string, workerId: string, leaseGeneration: number): OpenHandsAcceptanceRun {
    const at = timestamp();
    const result = this.database.prepare(
      `UPDATE openhands_acceptance_runs SET status = 'RECOVERING', lease_expires_at = NULL,
       completed_at = NULL, updated_at = ?
       WHERE id = ? AND worker_id = ? AND lease_generation = ? AND status = 'RUNNING'`,
    ).run(at, runId, workerId, leaseGeneration);
    if (result.changes !== 1) {
      const current = this.getRun(runId);
      if (current.status !== "RECOVERING" || current.workerId !== workerId || current.leaseGeneration !== leaseGeneration) {
        throw new Error("acceptance Run recovery CAS was rejected");
      }
    }
    return this.getRun(runId);
  }

  prepareRunRecoveryAfterProcessDeath(
    runId: string,
    workerId: string,
    processId: number,
    leaseGeneration: number,
  ): OpenHandsAcceptanceRun {
    if (!workerId || !Number.isSafeInteger(processId) || processId <= 1
      || !Number.isSafeInteger(leaseGeneration) || leaseGeneration < 1) {
      throw new Error("acceptance dead-process recovery identity is invalid");
    }
    const at = timestamp();
    const result = this.database.prepare(
      `UPDATE openhands_acceptance_runs SET status = 'RECOVERING', lease_expires_at = NULL,
       completed_at = NULL, updated_at = ?
       WHERE id = ? AND worker_id = ? AND process_id = ? AND lease_generation = ? AND status = 'RUNNING'`,
    ).run(at, runId, workerId, processId, leaseGeneration);
    if (result.changes !== 1) {
      const current = this.getRun(runId);
      if (
        current.status !== "RECOVERING"
        || current.workerId !== workerId
        || current.processId !== processId
        || current.leaseGeneration !== leaseGeneration
      ) {
        throw new Error("acceptance dead-process recovery CAS was rejected");
      }
    }
    return this.getRun(runId);
  }

  setRunState(
    runId: string,
    state: OpenHandsAcceptanceState,
    workerId: string,
    leaseGeneration: number,
  ): OpenHandsAcceptanceRun {
    const terminal = new Set<OpenHandsAcceptanceState>(["COMPLETED", "FAILED", "BLOCKED", "CANCELLED"]).has(state);
    const at = timestamp();
    const result = this.database.prepare(
      `UPDATE openhands_acceptance_runs SET status = ?,
       completed_at = CASE WHEN ? THEN COALESCE(completed_at, ?) ELSE NULL END,
       lease_expires_at = NULL, updated_at = ?
       WHERE id = ? AND worker_id = ? AND lease_generation = ? AND status IN ('RUNNING','RECOVERING')`,
    ).run(state, terminal ? 1 : 0, at, at, runId, workerId, leaseGeneration);
    if (result.changes !== 1) throw new Error("acceptance Run terminal state CAS was rejected");
    return this.getRun(runId);
  }

  getRun(runId: string): OpenHandsAcceptanceRun {
    const row = this.database.prepare("SELECT * FROM openhands_acceptance_runs WHERE id = ?").get(runId) as SqlRow | undefined;
    if (!row) throw new Error(`OpenHands acceptance Run ${runId} was not found`);
    return this.listRuns(text(row, "campaign_id")).find((run) => run.runId === runId)!;
  }

  appendEvent(runId: string, eventType: string, payload: JsonObject): number {
    return this.transaction(() => {
      const row = this.database.prepare(
        "SELECT COALESCE(MAX(sequence), 0) + 1 AS sequence FROM openhands_acceptance_events WHERE run_id = ?",
      ).get(runId) as SqlRow;
      const sequence = numberValue(row, "sequence");
      const payloadJson = stableJson(payload);
      this.database.prepare(
        "INSERT INTO openhands_acceptance_events(run_id, sequence, event_type, payload_hash, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
      ).run(runId, sequence, eventType, acceptanceHash(payload), payloadJson, timestamp());
      return sequence;
    });
  }

  checkpoint(input: {
    campaignId: string;
    runId?: string;
    stage: string;
    idempotencyKey: string;
    payload: JsonObject;
  }): { checkpointId: string; payloadHash: string; reused: boolean } {
    const existing = this.database.prepare(
      "SELECT id, payload_hash FROM openhands_acceptance_checkpoints WHERE idempotency_key = ?",
    ).get(input.idempotencyKey) as SqlRow | undefined;
    const payloadHash = acceptanceHash(input.payload);
    if (existing) {
      if (text(existing, "payload_hash") !== payloadHash) throw new Error("acceptance checkpoint idempotency collision");
      return { checkpointId: text(existing, "id"), payloadHash, reused: true };
    }
    const checkpointId = `openhands_acceptance_checkpoint_${randomUUID()}`;
    this.database.prepare(
      `INSERT INTO openhands_acceptance_checkpoints(
        id, campaign_id, run_id, stage, idempotency_key, payload_hash, payload_json, created_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
    ).run(
      checkpointId,
      input.campaignId,
      input.runId ?? null,
      input.stage,
      input.idempotencyKey,
      payloadHash,
      stableJson(input.payload),
      timestamp(),
    );
    return { checkpointId, payloadHash, reused: false };
  }

  recordProviderUsage(campaignId: string, promptTokens: number, completionTokens: number): void {
    if (![promptTokens, completionTokens].every((value) => Number.isSafeInteger(value) && value >= 0)) {
      throw new Error("Provider usage must be non-negative integer tokens");
    }
    this.database.prepare(
      `UPDATE openhands_acceptance_campaigns SET provider_prompt_tokens = provider_prompt_tokens + ?,
       provider_completion_tokens = provider_completion_tokens + ?, provider_calls = provider_calls + 1,
       updated_at = ? WHERE id = ?`,
    ).run(promptTokens, completionTokens, timestamp(), campaignId);
  }

  setProviderUsage(campaignId: string, calls: number, promptTokens: number, completionTokens: number): void {
    if (![calls, promptTokens, completionTokens].every((value) => Number.isSafeInteger(value) && value >= 0)) {
      throw new Error("Provider usage must be non-negative integers");
    }
    this.database.prepare(
      `UPDATE openhands_acceptance_campaigns SET provider_prompt_tokens = ?,
       provider_completion_tokens = ?, provider_calls = ?, updated_at = ? WHERE id = ?`,
    ).run(promptTokens, completionTokens, calls, timestamp(), campaignId);
  }

  claimArtifact(input: {
    runId: string;
    artifactSha256: string;
    logicalName: string;
    relativeOutputPath: string;
    parentHashes: string[];
  }): void {
    if (!/^[a-f0-9]{64}$/u.test(input.artifactSha256) || !input.parentHashes.every((hash) => /^[a-f0-9]{64}$/u.test(hash))) {
      throw new Error("OpenHands acceptance artifact claim contains an invalid SHA-256");
    }
    this.database.prepare(
      `INSERT INTO openhands_acceptance_artifact_claims(
        run_id, artifact_sha256, logical_name, relative_output_path, parent_hashes_json, created_at
      ) VALUES (?, ?, ?, ?, ?, ?)`,
    ).run(
      input.runId,
      input.artifactSha256,
      input.logicalName,
      input.relativeOutputPath,
      stableJson(input.parentHashes),
      timestamp(),
    );
  }

  planFault(input: {
    campaignId: string;
    runId: string;
    kind: "OPENHANDS_PROCESS_TERMINATION" | "SCIENCE_WORKER_TERMINATION";
    targetProcessId: number;
    targetProcessKey: string;
    beforeCheckpointHash: string;
    beforeSnapshot: JsonObject;
  }): string {
    if (!Number.isSafeInteger(input.targetProcessId) || input.targetProcessId <= 1
      || !input.targetProcessKey.trim() || !/^[a-f0-9]{64}$/u.test(input.beforeCheckpointHash)) {
      throw new Error("OpenHands acceptance fault identity is invalid");
    }
    const run = this.getRun(input.runId);
    if (run.campaignId !== input.campaignId
      || (input.kind === "SCIENCE_WORKER_TERMINATION" && run.lane !== "quantum_science")
      || (input.kind === "OPENHANDS_PROCESS_TERMINATION" && run.lane !== "openhands_orchestration")) {
      throw new Error("OpenHands acceptance fault Run identity is invalid");
    }
    const existing = this.database.prepare(
      "SELECT * FROM openhands_acceptance_faults WHERE campaign_id = ? AND kind = ?",
    ).get(input.campaignId, input.kind) as SqlRow | undefined;
    if (existing) {
      if (
        text(existing, "run_id") !== input.runId
        || numberValue(existing, "target_process_id") !== input.targetProcessId
        || optionalText(existing, "target_process_key") !== input.targetProcessKey
        || text(existing, "before_checkpoint_hash") !== input.beforeCheckpointHash
        || optionalText(existing, "before_snapshot_json") !== stableJson(input.beforeSnapshot)
      ) {
        throw new Error("OpenHands acceptance fault idempotency collision");
      }
      return text(existing, "id");
    }
    const faultId = `openhands_acceptance_fault_${randomUUID()}`;
    const at = timestamp();
    this.database.prepare(
      `INSERT INTO openhands_acceptance_faults(
        id, campaign_id, run_id, kind, target_process_id, target_process_key, state,
        before_checkpoint_hash, before_snapshot_json, created_at, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, 'PLANNED', ?, ?, ?, ?)`,
    ).run(
      faultId,
      input.campaignId,
      input.runId,
      input.kind,
      input.targetProcessId,
      input.targetProcessKey,
      input.beforeCheckpointHash,
      stableJson(input.beforeSnapshot),
      at,
      at,
    );
    return faultId;
  }

  private faultFromRow(row: SqlRow): OpenHandsAcceptanceFault {
    const kind = text(row, "kind");
    if (kind !== "OPENHANDS_PROCESS_TERMINATION" && kind !== "SCIENCE_WORKER_TERMINATION") {
      throw new Error("stored OpenHands acceptance fault kind is invalid");
    }
    return {
      faultId: text(row, "id"),
      campaignId: text(row, "campaign_id"),
      runId: text(row, "run_id"),
      kind,
      targetProcessId: numberValue(row, "target_process_id"),
      targetProcessKey: optionalText(row, "target_process_key") ?? "",
      state: text(row, "state") as OpenHandsAcceptanceFaultState,
      beforeCheckpointHash: text(row, "before_checkpoint_hash"),
      afterCheckpointHash: optionalText(row, "after_checkpoint_hash"),
      verification: jsonObjectFromText(optionalText(row, "verification_json")),
    };
  }

  getFault(
    campaignId: string,
    runId: string,
    kind: "OPENHANDS_PROCESS_TERMINATION" | "SCIENCE_WORKER_TERMINATION",
  ): OpenHandsAcceptanceFault | null {
    const row = this.database.prepare(
      "SELECT * FROM openhands_acceptance_faults WHERE campaign_id = ? AND run_id = ? AND kind = ?",
    ).get(campaignId, runId, kind) as SqlRow | undefined;
    return row ? this.faultFromRow(row) : null;
  }

  getFaultById(faultId: string): OpenHandsAcceptanceFault {
    const row = this.database.prepare("SELECT * FROM openhands_acceptance_faults WHERE id = ?")
      .get(faultId) as SqlRow | undefined;
    if (!row) throw new Error(`OpenHands acceptance fault ${faultId} was not found`);
    return this.faultFromRow(row);
  }

  markFaultInjected(faultId: string): OpenHandsAcceptanceFault {
    const current = this.getFaultById(faultId);
    if (current.state === "INJECTED" || current.state === "RECOVERED") return current;
    if (current.state !== "PLANNED") throw new Error("fault injection transition was rejected");
    const at = timestamp();
    const result = this.database.prepare(
      "UPDATE openhands_acceptance_faults SET state = 'INJECTED', injected_at = ?, updated_at = ? WHERE id = ? AND state = 'PLANNED'",
    ).run(at, at, faultId);
    if (result.changes !== 1) {
      const raced = this.getFaultById(faultId);
      if (raced.state !== "INJECTED" && raced.state !== "RECOVERED") {
        throw new Error("fault injection transition was rejected");
      }
      return raced;
    }
    return this.getFaultById(faultId);
  }

  verifyAndRecoverFault(faultId: string, afterCheckpointHash: string): JsonObject {
    const current = this.getFaultById(faultId);
    if (current.state === "RECOVERED") {
      if (current.afterCheckpointHash !== afterCheckpointHash || current.verification === null) {
        throw new Error("fault recovery idempotency collision");
      }
      return current.verification;
    }
    if (current.state !== "INJECTED") throw new Error("fault recovery requires an injected fault");
    const fault = this.database.prepare("SELECT * FROM openhands_acceptance_faults WHERE id = ?")
      .get(faultId) as SqlRow;
    if (!/^[a-f0-9]{64}$/u.test(afterCheckpointHash) || afterCheckpointHash === text(fault, "before_checkpoint_hash")) {
      throw new Error("fault recovery requires a distinct durable after-checkpoint");
    }
    const before = jsonObjectFromText(optionalText(fault, "before_snapshot_json"));
    if (!before) throw new Error("fault recovery snapshot is missing");
    const after = this.captureStateSnapshot(text(fault, "campaign_id"), afterCheckpointHash);
    const beforeSession = before.session;
    const afterSession = after.session;
    const sessionStable = typeof beforeSession === "object" && beforeSession !== null && !Array.isArray(beforeSession)
      && typeof afterSession === "object" && afterSession !== null && !Array.isArray(afterSession)
      && beforeSession.sessionId === afterSession.sessionId
      && beforeSession.runtimeRevision === afterSession.runtimeRevision
      && beforeSession.configHash === afterSession.configHash
      && Number(afterSession.recoveryCursor) >= Number(beforeSession.recoveryCursor);
    const beforeTools = Array.isArray(before.toolInbox) ? before.toolInbox : [];
    const afterTools = Array.isArray(after.toolInbox) ? after.toolInbox : [];
    const afterToolById = new Map(afterTools.flatMap((value) => {
      if (typeof value !== "object" || value === null || Array.isArray(value) || typeof value.toolCallId !== "string") return [];
      return [[value.toolCallId, value] as const];
    }));
    const toolInboxStable = beforeTools.every((value) => {
      if (typeof value !== "object" || value === null || Array.isArray(value) || typeof value.toolCallId !== "string") return false;
      const current = afterToolById.get(value.toolCallId);
      if (!current || current.toolName !== value.toolName || current.requestHash !== value.requestHash) return false;
      if (value.status === "COMPLETED" || value.status === "FAILED") {
        return current.status === value.status
          && current.resultJson === value.resultJson
          && current.errorJson === value.errorJson;
      }
      return current.status === "RUNNING" || current.status === "UNKNOWN";
    });
    const beforeArtifacts = new Set((Array.isArray(before.artifacts) ? before.artifacts : []).flatMap((value) => {
      if (typeof value !== "object" || value === null || Array.isArray(value) || typeof value.sha256 !== "string") return [];
      return [value.sha256];
    }));
    const afterArtifacts = new Set((Array.isArray(after.artifacts) ? after.artifacts : []).flatMap((value) => {
      if (typeof value !== "object" || value === null || Array.isArray(value) || typeof value.sha256 !== "string") return [];
      return [value.sha256];
    }));
    const artifactsStable = [...beforeArtifacts].every((sha256) => afterArtifacts.has(sha256));
    const beforeCheckpoints = new Map((Array.isArray(before.checkpoints) ? before.checkpoints : []).flatMap((value) => {
      if (typeof value !== "object" || value === null || Array.isArray(value)
        || typeof value.idempotencyKey !== "string" || typeof value.payloadHash !== "string") return [];
      return [[value.idempotencyKey, value.payloadHash] as const];
    }));
    const afterCheckpoints = new Map((Array.isArray(after.checkpoints) ? after.checkpoints : []).flatMap((value) => {
      if (typeof value !== "object" || value === null || Array.isArray(value)
        || typeof value.idempotencyKey !== "string" || typeof value.payloadHash !== "string") return [];
      return [[value.idempotencyKey, value.payloadHash] as const];
    }));
    const idempotencyStable = [...beforeCheckpoints].every(([key, hash]) => afterCheckpoints.get(key) === hash);
    const beforeUsage = before.providerUsageDelta;
    const afterUsage = after.providerUsageDelta;
    const providerUsageStable = typeof beforeUsage === "object" && beforeUsage !== null && !Array.isArray(beforeUsage)
      && typeof afterUsage === "object" && afterUsage !== null && !Array.isArray(afterUsage)
      && beforeUsage.calls === afterUsage.calls
      && beforeUsage.promptTokens === afterUsage.promptTokens
      && beforeUsage.completionTokens === afterUsage.completionTokens;
    const verification: JsonObject = {
      schemaVersion: "qf.openhands-acceptance-fault-recovery-verification.v2",
      sessionStable,
      toolInboxStable,
      artifactsStable,
      idempotencyStable,
      providerUsageStable,
      approvalsStable: before.approvalsDigest === after.approvalsDigest,
      hardwareStable: before.hardwareDigest === after.hardwareDigest,
      historyStable: before.historyDigest === after.historyDigest,
      beforeCheckpointHash: text(fault, "before_checkpoint_hash"),
      afterCheckpointHash,
      externalActionsReplayed: false,
    };
    if (Object.entries(verification)
      .filter(([key]) => key.endsWith("Stable"))
      .some(([, value]) => value !== true)) {
      throw new Error(`fault recovery invariant failed: ${stableJson(verification)}`);
    }
    const at = timestamp();
    const result = this.database.prepare(
      `UPDATE openhands_acceptance_faults SET state = 'RECOVERED', after_checkpoint_hash = ?,
       after_snapshot_json = ?, verification_json = ?, recovered_at = ?, updated_at = ?
       WHERE id = ? AND state = 'INJECTED'`,
    ).run(afterCheckpointHash, stableJson(after), stableJson(verification), at, at, faultId);
    if (result.changes !== 1) {
      const raced = this.getFaultById(faultId);
      if (raced.state !== "RECOVERED" || raced.afterCheckpointHash !== afterCheckpointHash || raced.verification === null) {
        throw new Error("fault recovery transition was rejected");
      }
      return raced.verification;
    }
    return verification;
  }

  audit(campaignId: string): JsonObject {
    const campaign = this.get(campaignId);
    const eventRows = this.database.prepare(
      `SELECT run_id, COUNT(*) AS event_count, COUNT(DISTINCT sequence) AS distinct_count,
       MIN(sequence) AS minimum_sequence, MAX(sequence) AS maximum_sequence
       FROM openhands_acceptance_events WHERE run_id IN (
         SELECT id FROM openhands_acceptance_runs WHERE campaign_id = ?
       ) GROUP BY run_id`,
    ).all(campaignId) as SqlRow[];
    const eventContinuity = eventRows.length === OPENHANDS_ACCEPTANCE_LANES.length && eventRows.every((row) => {
      const count = numberValue(row, "event_count");
      return count === numberValue(row, "distinct_count")
        && numberValue(row, "minimum_sequence") === 1
        && numberValue(row, "maximum_sequence") === count;
    });
    // Conversation event sequence allocation is durable-store global rather
    // than Conversation-local. Verify adjacency directly, allowing a positive
    // first sequence that is greater than one.
    const conversationSequenceRows = this.database.prepare(
      "SELECT sequence FROM events WHERE conversation_id = ? ORDER BY sequence",
    ).all(campaign.conversationId) as SqlRow[];
    const conversationEventSequenceContinuous = conversationSequenceRows.length > 0
      && conversationSequenceRows.every((row, index) => {
        const sequence = numberValue(row, "sequence");
        if (!Number.isSafeInteger(sequence) || sequence <= 0) return false;
        return index === 0 || sequence === numberValue(conversationSequenceRows[index - 1]!, "sequence") + 1;
      });
    const collisionRow = this.database.prepare(
      `SELECT COUNT(*) - COUNT(DISTINCT relative_output_path) AS collisions
       FROM openhands_acceptance_artifact_claims WHERE run_id IN (
         SELECT id FROM openhands_acceptance_runs WHERE campaign_id = ?
       )`,
    ).get(campaignId) as SqlRow;
    const faults = this.database.prepare(
      `SELECT kind, state, target_process_id, target_process_key, before_checkpoint_hash,
       after_checkpoint_hash, verification_json FROM openhands_acceptance_faults
       WHERE campaign_id = ? ORDER BY created_at`,
    ).all(campaignId) as SqlRow[];
    const latestCheckpoint = (stage: string): JsonObject | null => {
      const row = this.database.prepare(
        "SELECT payload_json FROM openhands_acceptance_checkpoints WHERE campaign_id = ? AND stage = ? ORDER BY created_at DESC LIMIT 1",
      ).get(campaignId, stage) as SqlRow | undefined;
      if (!row) return null;
      const parsed = JSON.parse(text(row, "payload_json")) as unknown;
      return typeof parsed === "object" && parsed !== null && !Array.isArray(parsed) ? parsed as JsonObject : null;
    };
    const orchestration = latestCheckpoint("openhands_orchestration_audit");
    const quantum = latestCheckpoint("quantum_science_audit");
    const evidence = latestCheckpoint("evidence_audit_audit");
    const quantumStages = quantum?.stages;
    const stageComplete = (stage: string): boolean => {
      if (typeof quantumStages !== "object" || quantumStages === null || Array.isArray(quantumStages)) return false;
      const value = quantumStages[stage];
      return typeof value === "object" && value !== null && !Array.isArray(value) && value.complete === true;
    };
    const orchestrationHealthy = orchestration?.runtimeKind === "OPENHANDS"
      && conversationEventSequenceContinuous
      && orchestration.postBaselineOnly === true
      && orchestration.toolPairsValid === true
      && Number(orchestration.completedTurnCount ?? 0) >= 1
      && Number(orchestration.providerUsageEvents ?? 0) >= 1
      && orchestration.secretPatternHits === 0;
    const quantumWorkflowComplete = ["A", "B", "C", "D", "E", "F"].every(stageComplete)
      && quantum?.postBaselineOnly === true
      && quantum?.toolOrderValid === true
      && quantum?.artifactDagValid === true
      && quantum?.structuredEvidenceValid === true
      && quantum?.readOnlyHardwareBoundary === true;
    const evidenceHealthy = evidence?.journalMode === "wal"
      && evidence.integrityCheck === "ok"
      && evidence.foreignKeysEnabled === true
      && evidence.eventContinuity === true
      && evidence.secretPatternHits === 0
      && evidence.hardwareJobsCreated === 0;
    const currentSnapshot = campaign.baselineSnapshot ? this.captureStateSnapshot(campaignId) : null;
    const hardwareStable = campaign.baselineSnapshot !== null && currentSnapshot !== null
      && campaign.baselineSnapshot.hardwareDigest === currentSnapshot.hardwareDigest;
    const historyStable = campaign.baselineSnapshot !== null && currentSnapshot !== null
      && campaign.baselineSnapshot.historyDigest === currentSnapshot.historyDigest;
    const baselineFrozen = campaign.baselineEventSequence !== null
      && typeof campaign.baselineSnapshotHash === "string"
      && campaign.baselineSnapshotHash === acceptanceHash(campaign.baselineSnapshot);
    const activityRows = this.database.prepare(
      `SELECT COUNT(*) AS interval_count, COALESCE(SUM(active_seconds), 0) AS active_seconds,
       MAX(active_seconds) AS maximum_interval_seconds
       FROM openhands_acceptance_activity_intervals WHERE run_id IN (
         SELECT id FROM openhands_acceptance_runs WHERE campaign_id = ?
       )`,
    ).get(campaignId) as SqlRow;
    const activityIntervalsRecorded = numberValue(activityRows, "interval_count") >= 3
      && numberValue(activityRows, "maximum_interval_seconds") <= 30
      && campaign.runs.every((run) => run.activeSeconds >= campaign.minimumOverlapSeconds);
    const recoveredFaults = faults.filter((row) => text(row, "state") === "RECOVERED");
    const faultVerificationHealthy = recoveredFaults.length === 2 && recoveredFaults.every((row) => {
      const verification = jsonObjectFromText(optionalText(row, "verification_json"));
      return verification !== null
        && verification.sessionStable === true
        && verification.toolInboxStable === true
        && verification.artifactsStable === true
        && verification.idempotencyStable === true
        && verification.providerUsageStable === true
        && verification.hardwareStable === true
        && verification.historyStable === true
        && verification.externalActionsReplayed === false;
    });
    const providerBlocked = orchestration?.providerBlocked === true;
    const gates: JsonObject = {
      baselineFrozen,
      wallClock: campaign.wallClockSeconds >= campaign.minimumWallClockSeconds,
      verifiedThreeLaneActiveOverlap: campaign.overlapSeconds >= campaign.minimumOverlapSeconds,
      activityIntervalsRecorded,
      threeRunsCompleted: campaign.runs.length === 3 && campaign.runs.every((run) => run.status === "COMPLETED"),
      twoFaultsRecovered: faults.length === 2 && faultVerificationHealthy,
      eventContinuity,
      conversationEventSequenceContinuous,
      noOutputCollision: numberValue(collisionRow, "collisions") === 0,
      formalTestSealed: campaign.formalTestSealed,
      readOnlyHardware: campaign.hardwareMode === "READ_ONLY" && campaign.hardwareJobsCreated === 0 && hardwareStable,
      historyStable,
      postStartProviderUsage: campaign.providerCalls >= 1
        && campaign.providerPromptTokens + campaign.providerCompletionTokens > 0,
      postStartOpenHandsMilestones: orchestrationHealthy,
      orderedQuantumWorkflow: quantumWorkflowComplete,
      evidenceHealthy,
    };
    const frameworkVerdict = providerBlocked
      ? "BLOCKED"
      : orchestrationHealthy && evidenceHealthy && baselineFrozen ? "PASS" : "FAILED";
    const durabilityVerdict = gates.wallClock === true
      && gates.verifiedThreeLaneActiveOverlap === true
      && gates.activityIntervalsRecorded === true
      && gates.threeRunsCompleted === true
      && gates.twoFaultsRecovered === true
      && gates.eventContinuity === true
      && gates.noOutputCollision === true
      ? "PASS"
      : "FAILED";
    const quantumWorkflowVerdict = quantumWorkflowComplete ? "PASS" : providerBlocked ? "BLOCKED" : "FAILED";
    const hardwareVerdict = "NOT_AUTHORIZED";
    const reportedScientificVerdict = quantum?.scientificVerdict;
    const scientificVerdict = reportedScientificVerdict === "POSITIVE" || reportedScientificVerdict === "NEGATIVE"
      ? reportedScientificVerdict
      : "INCONCLUSIVE";
    const overallVerdict = frameworkVerdict === "BLOCKED" || quantumWorkflowVerdict === "BLOCKED"
      ? "BLOCKED"
      : frameworkVerdict === "PASS" && durabilityVerdict === "PASS" && quantumWorkflowVerdict === "PASS"
        && Object.values(gates).every((value) => value === true)
        ? "PASS"
        : "FAILED";
    return {
      schemaVersion: "qf.openhands-acceptance-audit.v2",
      campaignId,
      wallClockSeconds: campaign.wallClockSeconds,
      overlapSeconds: campaign.overlapSeconds,
      eventContinuity,
      outputPathCollisions: numberValue(collisionRow, "collisions"),
      formalTestSealed: campaign.formalTestSealed,
      hardwareJobsCreated: campaign.hardwareJobsCreated,
      providerCalls: campaign.providerCalls,
      providerPromptTokens: campaign.providerPromptTokens,
      providerCompletionTokens: campaign.providerCompletionTokens,
      orchestrationHealthy,
      quantumWorkflowComplete,
      evidenceHealthy,
      hardwareStable,
      historyStable,
      baselineFrozen,
      stateAxes: {
        frameworkVerdict,
        durabilityVerdict,
        quantumWorkflowVerdict,
        hardwareVerdict,
        scientificVerdict,
        overallVerdict,
      },
      runs: campaign.runs.map((run) => ({
        runId: run.runId,
        lane: run.lane,
        status: run.status,
        activeSeconds: run.activeSeconds,
        externalWaitSeconds: run.externalWaitSeconds,
        recoveryCount: run.recoveryCount,
        leaseGeneration: run.leaseGeneration,
      })),
      faults: faults.map((row) => ({
        kind: text(row, "kind"),
        state: text(row, "state"),
        targetProcessId: numberValue(row, "target_process_id"),
        targetProcessKey: optionalText(row, "target_process_key"),
        beforeCheckpointHash: text(row, "before_checkpoint_hash"),
        afterCheckpointHash: optionalText(row, "after_checkpoint_hash"),
        verification: jsonObjectFromText(optionalText(row, "verification_json")),
      })),
      gates,
    };
  }

  finalize(campaignId: string): { campaign: OpenHandsAcceptanceCampaign; audit: JsonObject; passed: boolean } {
    const audit = this.audit(campaignId);
    const gates = audit.gates;
    if (typeof gates !== "object" || gates === null || Array.isArray(gates)) throw new Error("acceptance audit gates are invalid");
    const axes = audit.stateAxes;
    if (typeof axes !== "object" || axes === null || Array.isArray(axes)) throw new Error("acceptance state axes are invalid");
    const passed = Object.values(gates).every((value) => value === true) && axes.overallVerdict === "PASS";
    const at = timestamp();
    const terminalState = axes.overallVerdict === "BLOCKED" ? "BLOCKED" : passed ? "COMPLETED" : "FAILED";
    this.database.prepare(
      `UPDATE openhands_acceptance_campaigns SET status = ?, completed_at = COALESCE(completed_at, ?),
       frozen_at = COALESCE(frozen_at, ?), updated_at = ? WHERE id = ?`,
    ).run(terminalState, at, at, at, campaignId);
    return { campaign: this.get(campaignId), audit, passed };
  }
}
