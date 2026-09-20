import { createHash, randomUUID } from "node:crypto";
import type { DatabaseSync, StatementSync } from "node:sqlite";

import type {
  CampaignActionSummary,
  CampaignCheckpoint,
  CampaignDetail,
  CampaignRole,
  CampaignState,
  CampaignSummary,
  ChainLevel,
  FailureCardSummary,
  JsonObject,
  P02LaunchAuthorization,
  P04LaunchAuthorization,
  P03HardwareBudgetSummary,
  P03QueueLifecycleState,
  P03QueueRunSummary,
  QuantumJobSummary,
} from "@q-fintelligence/contracts";
import { CONTRACT_SCHEMA_VERSION } from "@q-fintelligence/contracts";

import type { CampaignRoleDefinition } from "./roles.js";

type SqlRow = Record<string, string | number | bigint | Uint8Array | null>;

function now(): string {
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

function optionalNumberValue(row: SqlRow, key: string): number | null {
  const value = row[key];
  if (value === null || value === undefined) return null;
  if (typeof value === "number") return value;
  if (typeof value === "bigint") return Number(value);
  throw new Error(`expected nullable number column ${key}`);
}

function rows(statement: StatementSync, ...parameters: (string | number | null)[]): SqlRow[] {
  return statement.all(...parameters) as SqlRow[];
}

function stableHash(value: unknown): string {
  return createHash("sha256").update(JSON.stringify(value)).digest("hex");
}

export class CampaignRepository {
  constructor(private readonly database: DatabaseSync) {}

  transaction<T>(operation: () => T): T {
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

  ensureCampaign(input: {
    taskId: string;
    conversationId: string;
    authorizationHash: string;
    authorization: P02LaunchAuthorization | P04LaunchAuthorization;
    roles: CampaignRoleDefinition[];
  }): CampaignSummary {
    const existing = this.database.prepare(
      "SELECT id FROM campaigns WHERE task_id = ? AND authorization_hash = ?",
    ).get(input.taskId, input.authorizationHash) as SqlRow | undefined;
    if (existing) return this.getCampaign(text(existing, "id"));
    const campaignId = `campaign_${randomUUID()}`;
    const timestamp = now();
    this.transaction(() => {
      this.database.prepare(
        `INSERT INTO campaigns(
          id, task_id, conversation_id, authorization_hash, status, current_stage,
          minimum_runtime_seconds, maximum_runtime_seconds, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'CREATED', 'created', ?, ?, ?, ?)`,
      ).run(
        campaignId,
        input.taskId,
        input.conversationId,
        input.authorizationHash,
        input.authorization.minimumRuntimeMinutes * 60,
        input.authorization.maximumRuntimeMinutes * 60,
        timestamp,
        timestamp,
      );
      this.database.prepare(
        `INSERT INTO resource_authorizations(
          id, campaign_id, authorization_hash, source, scope_json, approved_at
        ) VALUES (?, ?, ?, 'USER_STANDING', ?, ?)`,
      ).run(
        `authorization_${randomUUID()}`,
        campaignId,
        input.authorizationHash,
        JSON.stringify(input.authorization),
        timestamp,
      );
      for (const role of input.roles) {
        this.database.prepare(
          `INSERT INTO campaign_agents(
            campaign_id, role, provider, model_id, prompt_version, prompt_hash, tool_names_json, state
          ) VALUES (?, ?, 'openai', 'gpt-5.6-sol', ?, ?, ?, 'REGISTERED')`,
        ).run(campaignId, role.role, role.promptVersion, role.promptHash, JSON.stringify(role.toolNames));
        this.database.prepare(
          `INSERT INTO campaign_agent_sessions(
            id, campaign_id, role, session_file, state, created_at, updated_at
          ) VALUES (?, ?, ?, ?, 'PENDING_PROVIDER_GATE', ?, ?)`,
        ).run(
          `campaign_session_${randomUUID()}`,
          campaignId,
          role.role,
          `.local/campaigns/${campaignId}/sessions/${role.role}.jsonl`,
          timestamp,
          timestamp,
        );
      }
      this.database.prepare(
        "INSERT INTO science_workspaces(campaign_id, relative_root, state, created_at) VALUES (?, ?, 'CREATED', ?)",
      ).run(campaignId, `.local/campaigns/${campaignId}/workspace`, timestamp);
    });
    return this.getCampaign(campaignId);
  }

  private campaignFromRow(row: SqlRow): CampaignSummary {
    const startedAt = optionalText(row, "started_at");
    const completedAt = optionalText(row, "completed_at");
    const end = completedAt ? Date.parse(completedAt) : Date.now();
    const wallClockSeconds = startedAt ? Math.max(0, (end - Date.parse(startedAt)) / 1000) : 0;
    const p03QueueRuns = this.listP03QueueRuns(text(row, "id"));
    return {
      schemaVersion: CONTRACT_SCHEMA_VERSION,
      campaignId: text(row, "id"),
      taskId: text(row, "task_id"),
      conversationId: text(row, "conversation_id"),
      authorizationHash: text(row, "authorization_hash"),
      status: text(row, "status") as CampaignState,
      currentStage: text(row, "current_stage"),
      minimumRuntimeSeconds: numberValue(row, "minimum_runtime_seconds"),
      maximumRuntimeSeconds: numberValue(row, "maximum_runtime_seconds"),
      wallClockSeconds,
      activeComputeSeconds: numberValue(row, "active_compute_seconds"),
      externalWaitSeconds: numberValue(row, "external_wait_seconds"),
      humanWaitSeconds: numberValue(row, "human_wait_seconds"),
      llmCalls: numberValue(row, "llm_calls"),
      hardwareJobs: numberValue(row, "hardware_jobs"),
      hardwareExecutionSeconds: numberValue(row, "hardware_execution_seconds"),
      p03HardwareBudget: this.getP03HardwareBudget(text(row, "id")),
      latestP03QueueRun: p03QueueRuns.at(-1) ?? null,
      highestChainLevel: text(row, "highest_chain_level") as ChainLevel,
      blockerCategory: optionalText(row, "blocker_category"),
      blockerArtifactSha256: optionalText(row, "blocker_artifact_sha256"),
      lastHeartbeatAt: optionalText(row, "last_heartbeat_at"),
      startedAt,
      completedAt,
      createdAt: text(row, "created_at"),
      updatedAt: text(row, "updated_at"),
    };
  }

  getCampaign(campaignId: string): CampaignSummary {
    const row = this.database.prepare(
      `SELECT campaigns.*, campaign_leases.heartbeat_at AS last_heartbeat_at
       FROM campaigns LEFT JOIN campaign_leases ON campaign_leases.campaign_id = campaigns.id
       WHERE campaigns.id = ?`,
    ).get(campaignId) as SqlRow | undefined;
    if (!row) throw new Error(`campaign ${campaignId} was not found`);
    return this.campaignFromRow(row);
  }

  listCampaigns(): CampaignSummary[] {
    return rows(this.database.prepare(
      `SELECT campaigns.*, campaign_leases.heartbeat_at AS last_heartbeat_at
       FROM campaigns LEFT JOIN campaign_leases ON campaign_leases.campaign_id = campaigns.id
       ORDER BY campaigns.created_at DESC`,
    )).map((row) => this.campaignFromRow(row));
  }

  listCampaignsForConversation(conversationId: string): CampaignSummary[] {
    return rows(this.database.prepare(
      `SELECT campaigns.*, campaign_leases.heartbeat_at AS last_heartbeat_at
       FROM campaigns LEFT JOIN campaign_leases ON campaign_leases.campaign_id = campaigns.id
       WHERE campaigns.conversation_id = ? ORDER BY campaigns.created_at DESC`,
    ), conversationId).map((row) => this.campaignFromRow(row));
  }

  setCampaignState(
    campaignId: string,
    status: CampaignState,
    stage: string,
    options: { blockerCategory?: string | null; blockerArtifactSha256?: string | null; level?: ChainLevel } = {},
  ): CampaignSummary {
    const timestamp = now();
    const terminal = new Set<CampaignState>(["BLOCKED", "FAILED", "COMPLETED", "CANCELLED"]).has(status);
    this.database.prepare(
      `UPDATE campaigns SET status = ?, current_stage = ?,
       started_at = CASE WHEN ? IN ('PREFLIGHT','RUNNING') THEN COALESCE(started_at, ?) ELSE started_at END,
       completed_at = CASE WHEN ? THEN COALESCE(completed_at, ?) ELSE NULL END,
       blocker_category = ?, blocker_artifact_sha256 = ?,
       highest_chain_level = COALESCE(?, highest_chain_level), updated_at = ? WHERE id = ?`,
    ).run(
      status,
      stage,
      status,
      timestamp,
      terminal ? 1 : 0,
      timestamp,
      options.blockerCategory ?? null,
      options.blockerArtifactSha256 ?? null,
      options.level ?? null,
      timestamp,
      campaignId,
    );
    return this.getCampaign(campaignId);
  }

  addTiming(campaignId: string, kind: "active" | "external" | "human", seconds: number): void {
    if (!Number.isFinite(seconds) || seconds < 0) throw new Error("campaign timing delta must be finite and non-negative");
    const column = kind === "active" ? "active_compute_seconds" : kind === "external" ? "external_wait_seconds" : "human_wait_seconds";
    this.database.prepare(`UPDATE campaigns SET ${column} = ${column} + ?, updated_at = ? WHERE id = ?`)
      .run(seconds, now(), campaignId);
  }

  incrementLlmCalls(campaignId: string): number {
    this.database.prepare(
      "UPDATE campaigns SET llm_calls = llm_calls + 1, updated_at = ? WHERE id = ? AND llm_calls < 120",
    ).run(now(), campaignId);
    return this.getCampaign(campaignId).llmCalls;
  }

  acquireLease(campaignId: string, workerId: string, ttlSeconds = 90): void {
    const timestamp = now();
    const expiresAt = new Date(Date.now() + ttlSeconds * 1000).toISOString();
    this.transaction(() => {
      const current = this.database.prepare("SELECT * FROM campaign_leases WHERE campaign_id = ?").get(campaignId) as SqlRow | undefined;
      if (current) {
        const released = optionalText(current, "released_at") !== null;
        const expired = Date.parse(text(current, "expires_at")) <= Date.now();
        if (!released && !expired && text(current, "worker_id") !== workerId) {
          throw new Error(`campaign ${campaignId} has a live lease`);
        }
        this.database.prepare("DELETE FROM campaign_leases WHERE campaign_id = ?").run(campaignId);
      }
      this.database.prepare(
        "INSERT INTO campaign_leases(campaign_id, worker_id, acquired_at, heartbeat_at, expires_at) VALUES (?, ?, ?, ?, ?)",
      ).run(campaignId, workerId, timestamp, timestamp, expiresAt);
      this.database.prepare(
        "UPDATE campaigns SET status = CASE WHEN status IN ('CREATED','PAUSED') THEN 'PREFLIGHT' ELSE status END, updated_at = ? WHERE id = ?",
      ).run(timestamp, campaignId);
    });
  }

  heartbeat(campaignId: string, workerId: string, ttlSeconds = 90): void {
    const timestamp = now();
    const expiresAt = new Date(Date.now() + ttlSeconds * 1000).toISOString();
    const result = this.database.prepare(
      `UPDATE campaign_leases SET heartbeat_at = ?, expires_at = ?
       WHERE campaign_id = ? AND worker_id = ? AND released_at IS NULL`,
    ).run(timestamp, expiresAt, campaignId, workerId);
    if (result.changes !== 1) throw new Error("campaign lease heartbeat was rejected");
  }

  releaseLease(campaignId: string, workerId: string): void {
    this.database.prepare(
      "UPDATE campaign_leases SET released_at = ?, expires_at = ? WHERE campaign_id = ? AND worker_id = ?",
    ).run(now(), now(), campaignId, workerId);
  }

  checkpoint(campaignId: string, stage: string, payload: JsonObject): CampaignCheckpoint {
    const checkpointId = `checkpoint_${randomUUID()}`;
    const createdAt = now();
    const stateHash = stableHash(payload);
    this.database.prepare(
      "INSERT INTO campaign_checkpoints(id, campaign_id, stage, state_hash, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
    ).run(checkpointId, campaignId, stage, stateHash, JSON.stringify(payload), createdAt);
    return { schemaVersion: CONTRACT_SCHEMA_VERSION, checkpointId, campaignId, stage, stateHash, payload, createdAt };
  }

  listCheckpoints(campaignId: string, limit = 20): CampaignCheckpoint[] {
    return rows(this.database.prepare(
      "SELECT * FROM campaign_checkpoints WHERE campaign_id = ? ORDER BY created_at DESC LIMIT ?",
    ), campaignId, limit).map((row) => ({
      schemaVersion: CONTRACT_SCHEMA_VERSION,
      checkpointId: text(row, "id"),
      campaignId,
      stage: text(row, "stage"),
      stateHash: text(row, "state_hash"),
      payload: JSON.parse(text(row, "payload_json")) as JsonObject,
      createdAt: text(row, "created_at"),
    }));
  }

  beginAction(input: {
    campaignId: string;
    stage: string;
    actionType: string;
    idempotencyKey: string;
    inputHash: string;
    expectedEvidence: string;
  }): CampaignActionSummary {
    const existing = this.database.prepare("SELECT id FROM campaign_actions WHERE idempotency_key = ?")
      .get(input.idempotencyKey) as SqlRow | undefined;
    if (existing) return this.getAction(text(existing, "id"));
    const actionId = `action_${randomUUID()}`;
    this.database.prepare(
      `INSERT INTO campaign_actions(
        id, campaign_id, stage, action_type, idempotency_key, input_hash, status, expected_evidence, started_at
      ) VALUES (?, ?, ?, ?, ?, ?, 'RUNNING', ?, ?)`,
    ).run(
      actionId,
      input.campaignId,
      input.stage,
      input.actionType,
      input.idempotencyKey,
      input.inputHash,
      input.expectedEvidence,
      now(),
    );
    return this.getAction(actionId);
  }

  completeAction(actionId: string, status: "COMPLETED" | "BLOCKED" | "FAILED", outputArtifactSha256?: string, error?: JsonObject): CampaignActionSummary {
    this.database.prepare(
      "UPDATE campaign_actions SET status = ?, output_artifact_sha256 = ?, error_json = ?, completed_at = ? WHERE id = ?",
    ).run(status, outputArtifactSha256 ?? null, error ? JSON.stringify(error) : null, now(), actionId);
    return this.getAction(actionId);
  }

  recordToolResultEnvelope(input: {
    campaignId: string;
    actionId: string;
    resultHash: string;
    artifactSha256: string;
    status: string;
    warnings: string[];
  }): string {
    const existing = this.database.prepare(
      "SELECT id FROM tool_result_envelopes WHERE campaign_id = ? AND result_hash = ?",
    ).get(input.campaignId, input.resultHash) as SqlRow | undefined;
    if (existing) return text(existing, "id");
    const id = `tool_result_${randomUUID()}`;
    this.database.prepare(
      `INSERT INTO tool_result_envelopes(
        id, campaign_id, action_id, schema_version, result_hash, artifact_sha256, status, warnings_json, created_at
      ) VALUES (?, ?, ?, 'qf.tool-result-envelope.v1', ?, ?, ?, ?, ?)`,
    ).run(
      id,
      input.campaignId,
      input.actionId,
      input.resultHash,
      input.artifactSha256,
      input.status,
      JSON.stringify(input.warnings),
      now(),
    );
    return id;
  }

  recordBudget(input: {
    campaignId: string;
    resourceKind: string;
    actionKey: string;
    calls?: number;
    shots?: number;
    executionSeconds?: number;
    expectedEvidence: string;
  }): void {
    this.database.prepare(
      `INSERT INTO budget_ledger(
        id, campaign_id, resource_kind, action_key, calls, shots, execution_seconds, expected_evidence, created_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(campaign_id, resource_kind, action_key) DO UPDATE SET
        calls = excluded.calls, shots = excluded.shots, execution_seconds = excluded.execution_seconds,
        expected_evidence = excluded.expected_evidence`,
    ).run(
      `budget_${randomUUID()}`,
      input.campaignId,
      input.resourceKind,
      input.actionKey,
      input.calls ?? 0,
      input.shots ?? 0,
      input.executionSeconds ?? 0,
      input.expectedEvidence,
      now(),
    );
  }

  getAction(actionId: string): CampaignActionSummary {
    const row = this.database.prepare("SELECT * FROM campaign_actions WHERE id = ?").get(actionId) as SqlRow | undefined;
    if (!row) throw new Error(`campaign action ${actionId} was not found`);
    return {
      schemaVersion: CONTRACT_SCHEMA_VERSION,
      actionId,
      campaignId: text(row, "campaign_id"),
      stage: text(row, "stage"),
      actionType: text(row, "action_type"),
      idempotencyKey: text(row, "idempotency_key"),
      inputHash: text(row, "input_hash"),
      status: text(row, "status") as CampaignActionSummary["status"],
      expectedEvidence: text(row, "expected_evidence"),
      outputArtifactSha256: optionalText(row, "output_artifact_sha256"),
      startedAt: text(row, "started_at"),
      completedAt: optionalText(row, "completed_at"),
    };
  }

  listActions(campaignId: string): CampaignActionSummary[] {
    return rows(this.database.prepare(
      "SELECT id FROM campaign_actions WHERE campaign_id = ? ORDER BY started_at, id",
    ), campaignId).map((row) => this.getAction(text(row, "id")));
  }

  recordGate(input: {
    campaignId: string;
    gateName: string;
    subjectHash: string;
    decision: "ALLOW" | "BLOCK" | "REVISE";
    reason: string;
    evidenceArtifactSha256?: string;
  }): void {
    this.database.prepare(
      `INSERT OR REPLACE INTO gate_decisions(
        id, campaign_id, gate_name, subject_hash, decision, reason, evidence_artifact_sha256, decided_at
      ) VALUES (
        COALESCE((SELECT id FROM gate_decisions WHERE campaign_id = ? AND gate_name = ? AND subject_hash = ?), ?),
        ?, ?, ?, ?, ?, ?, ?
      )`,
    ).run(
      input.campaignId,
      input.gateName,
      input.subjectHash,
      `gate_${randomUUID()}`,
      input.campaignId,
      input.gateName,
      input.subjectHash,
      input.decision,
      input.reason,
      input.evidenceArtifactSha256 ?? null,
      now(),
    );
  }

  getGateDecision(campaignId: string, gateName: string): {
    decision: "ALLOW" | "BLOCK" | "REVISE";
    reason: string;
    evidenceArtifactSha256: string | null;
  } | null {
    const row = this.database.prepare(
      "SELECT decision, reason, evidence_artifact_sha256 FROM gate_decisions WHERE campaign_id = ? AND gate_name = ? ORDER BY decided_at DESC LIMIT 1",
    ).get(campaignId, gateName) as SqlRow | undefined;
    return row ? {
      decision: text(row, "decision") as "ALLOW" | "BLOCK" | "REVISE",
      reason: text(row, "reason"),
      evidenceArtifactSha256: optionalText(row, "evidence_artifact_sha256"),
    } : null;
  }

  recordFailure(input: {
    campaignId: string;
    stage: string;
    category: string;
    summary: string;
    evidenceArtifactSha256: string;
    attempts: number;
    recoveryCommand: string;
  }): FailureCardSummary {
    const existing = this.listFailures(input.campaignId).find((failure) => (
      failure.stage === input.stage
      && failure.category === input.category
      && failure.summary === input.summary
      && failure.evidenceArtifactSha256 === input.evidenceArtifactSha256
    ));
    if (existing) return existing;
    const id = `failure_${randomUUID()}`;
    const createdAt = now();
    this.database.prepare(
      `INSERT INTO failure_cards(
        id, campaign_id, stage, category, summary, evidence_artifact_sha256, attempts, recovery_command, created_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    ).run(
      id,
      input.campaignId,
      input.stage,
      input.category,
      input.summary,
      input.evidenceArtifactSha256,
      input.attempts,
      input.recoveryCommand,
      createdAt,
    );
    return {
      schemaVersion: CONTRACT_SCHEMA_VERSION,
      failureCardId: id,
      campaignId: input.campaignId,
      stage: input.stage,
      category: input.category,
      summary: input.summary,
      evidenceArtifactSha256: input.evidenceArtifactSha256,
      attempts: input.attempts,
      recoveryCommand: input.recoveryCommand,
      createdAt,
    };
  }

  listFailures(campaignId: string): FailureCardSummary[] {
    return rows(this.database.prepare(
      "SELECT * FROM failure_cards WHERE campaign_id = ? ORDER BY created_at, id",
    ), campaignId).map((row) => ({
      schemaVersion: CONTRACT_SCHEMA_VERSION,
      failureCardId: text(row, "id"),
      campaignId,
      stage: text(row, "stage"),
      category: text(row, "category"),
      summary: text(row, "summary"),
      evidenceArtifactSha256: text(row, "evidence_artifact_sha256"),
      attempts: numberValue(row, "attempts"),
      recoveryCommand: text(row, "recovery_command"),
      createdAt: text(row, "created_at"),
    }));
  }

  registerAgentSessionState(campaignId: string, role: CampaignRole, state: string): void {
    this.database.prepare(
      "UPDATE campaign_agent_sessions SET state = ?, updated_at = ? WHERE campaign_id = ? AND role = ?",
    ).run(state, now(), campaignId, role);
    this.database.prepare(
      "UPDATE campaign_agents SET state = ? WHERE campaign_id = ? AND role = ?",
    ).run(state, campaignId, role);
  }

  prepareExternalRequest(input: {
    campaignId: string;
    provider: string;
    requestKind: string;
    target: string;
    idempotencyKey: string;
    approvalHash: string;
    requestHash: string;
  }): string {
    const existing = this.database.prepare("SELECT id FROM external_requests WHERE idempotency_key = ?")
      .get(input.idempotencyKey) as SqlRow | undefined;
    if (existing) return text(existing, "id");
    const id = `external_${randomUUID()}`;
    const timestamp = now();
    this.database.prepare(
      `INSERT INTO external_requests(
        id, campaign_id, provider, request_kind, target, idempotency_key, approval_hash, request_hash,
        status, created_at, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PREPARED', ?, ?)`,
    ).run(
      id,
      input.campaignId,
      input.provider,
      input.requestKind,
      input.target,
      input.idempotencyKey,
      input.approvalHash,
      input.requestHash,
      timestamp,
      timestamp,
    );
    return id;
  }

  markExternalRequest(id: string, status: string, externalId?: string, artifactSha256?: string): void {
    this.database.prepare(
      "UPDATE external_requests SET status = ?, external_id = COALESCE(?, external_id), response_artifact_sha256 = COALESCE(?, response_artifact_sha256), updated_at = ? WHERE id = ?",
    ).run(status, externalId ?? null, artifactSha256 ?? null, now(), id);
  }

  createQuantumJob(input: {
    campaignId: string;
    externalRequestId: string;
    purpose: QuantumJobSummary["purpose"];
    backend: string;
    targetType: QuantumJobSummary["targetType"];
    circuitHash: string;
    shots: number;
    estimatedExecutionSeconds?: number;
  }): QuantumJobSummary {
    const existing = this.database.prepare("SELECT id FROM quantum_jobs WHERE external_request_id = ?")
      .get(input.externalRequestId) as SqlRow | undefined;
    if (existing) return this.getQuantumJob(text(existing, "id"));
    const id = `quantum_job_${randomUUID()}`;
    const timestamp = now();
    this.transaction(() => {
      if (input.targetType === "HARDWARE") {
        if (input.estimatedExecutionSeconds === undefined || input.estimatedExecutionSeconds <= 0) {
          throw new Error("hardware job requires a positive execution-time estimate");
        }
        const budget = this.database.prepare(
          `SELECT campaigns.hardware_jobs, campaigns.hardware_execution_seconds,
           COALESCE(SUM(quantum_jobs.shots), 0) AS total_shots,
           COALESCE(SUM(quantum_jobs.estimated_execution_seconds), 0) AS total_estimated_seconds
           FROM campaigns LEFT JOIN quantum_jobs
             ON quantum_jobs.campaign_id = campaigns.id AND quantum_jobs.target_type = 'HARDWARE'
           WHERE campaigns.id = ? GROUP BY campaigns.id`,
        ).get(input.campaignId) as SqlRow | undefined;
        if (!budget) throw new Error(`campaign ${input.campaignId} was not found`);
        if (numberValue(budget, "hardware_jobs") >= 2) throw new Error("hardware job budget is exhausted");
        if (numberValue(budget, "total_shots") + input.shots > 10_000) throw new Error("hardware shot budget would be exceeded");
        if (numberValue(budget, "total_estimated_seconds") + input.estimatedExecutionSeconds > 600) {
          throw new Error("hardware execution-time estimate would exceed 600 seconds");
        }
      }
      this.database.prepare(
        `INSERT INTO quantum_jobs(
          id, campaign_id, external_request_id, purpose, backend, target_type, circuit_hash, shots,
          estimated_execution_seconds, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      ).run(
        id,
        input.campaignId,
        input.externalRequestId,
        input.purpose,
        input.backend,
        input.targetType,
        input.circuitHash,
        input.shots,
        input.estimatedExecutionSeconds ?? null,
        timestamp,
        timestamp,
      );
      if (input.targetType === "HARDWARE") {
        this.database.prepare(
          "UPDATE campaigns SET hardware_jobs = hardware_jobs + 1, updated_at = ? WHERE id = ?",
        ).run(timestamp, input.campaignId);
      }
    });
    return this.getQuantumJob(id);
  }

  completeQuantumJob(id: string, queryId: string, terminalStatus: string, artifactSha256: string, actualSeconds?: number): QuantumJobSummary {
    const job = this.getQuantumJob(id);
    this.transaction(() => {
      if (job.targetType === "HARDWARE" && actualSeconds !== undefined) {
        const campaign = this.getCampaign(job.campaignId);
        if (actualSeconds < 0 || campaign.hardwareExecutionSeconds + actualSeconds > 600) {
          throw new Error("hardware execution time would exceed the 600-second authorization");
        }
      }
      this.database.prepare(
        `UPDATE quantum_jobs SET query_id = ?, terminal_status = ?, raw_result_artifact_sha256 = ?,
         actual_execution_seconds = ?, updated_at = ? WHERE id = ?`,
      ).run(queryId, terminalStatus, artifactSha256, actualSeconds ?? null, now(), id);
      if (job.targetType === "HARDWARE" && actualSeconds !== undefined) {
        this.database.prepare(
          "UPDATE campaigns SET hardware_execution_seconds = hardware_execution_seconds + ?, updated_at = ? WHERE id = ?",
        ).run(actualSeconds, now(), job.campaignId);
      }
    });
    return this.getQuantumJob(id);
  }

  markQuantumJobSubmitted(id: string, queryId: string): QuantumJobSummary {
    if (!queryId.trim()) throw new Error("quantum query id must not be empty");
    this.database.prepare(
      `UPDATE quantum_jobs SET query_id = ?, terminal_status = 'SUBMITTED', updated_at = ?
       WHERE id = ? AND (query_id IS NULL OR query_id = ?)`,
    ).run(queryId, now(), id, queryId);
    const updated = this.getQuantumJob(id);
    if (updated.queryId !== queryId) {
      throw new Error("quantum job already has a different query id; resubmission is prohibited");
    }
    return updated;
  }

  getQuantumJob(id: string): QuantumJobSummary {
    const row = this.database.prepare("SELECT * FROM quantum_jobs WHERE id = ?").get(id) as SqlRow | undefined;
    if (!row) throw new Error(`quantum job ${id} was not found`);
    return {
      schemaVersion: CONTRACT_SCHEMA_VERSION,
      quantumJobId: id,
      campaignId: text(row, "campaign_id"),
      purpose: text(row, "purpose") as QuantumJobSummary["purpose"],
      backend: text(row, "backend"),
      targetType: text(row, "target_type") as QuantumJobSummary["targetType"],
      circuitHash: text(row, "circuit_hash"),
      shots: numberValue(row, "shots"),
      estimatedExecutionSeconds: row.estimated_execution_seconds === null ? null : numberValue(row, "estimated_execution_seconds"),
      actualExecutionSeconds: row.actual_execution_seconds === null ? null : numberValue(row, "actual_execution_seconds"),
      queryId: optionalText(row, "query_id"),
      terminalStatus: optionalText(row, "terminal_status"),
      rawResultArtifactSha256: optionalText(row, "raw_result_artifact_sha256"),
      createdAt: text(row, "created_at"),
      updatedAt: text(row, "updated_at"),
    };
  }

  listQuantumJobs(campaignId: string): QuantumJobSummary[] {
    return rows(this.database.prepare(
      "SELECT id FROM quantum_jobs WHERE campaign_id = ? ORDER BY created_at, id",
    ), campaignId).map((row) => this.getQuantumJob(text(row, "id")));
  }

  findQuantumJobByQueryId(campaignId: string, queryId: string): QuantumJobSummary | null {
    const row = this.database.prepare(
      "SELECT id FROM quantum_jobs WHERE campaign_id = ? AND query_id = ?",
    ).get(campaignId, queryId) as SqlRow | undefined;
    return row ? this.getQuantumJob(text(row, "id")) : null;
  }

  markExternalRequestForQuantumJob(quantumJobId: string, status: string, artifactSha256?: string): void {
    const row = this.database.prepare(
      "SELECT external_request_id, query_id FROM quantum_jobs WHERE id = ?",
    ).get(quantumJobId) as SqlRow | undefined;
    if (!row) throw new Error(`quantum job ${quantumJobId} was not found`);
    this.markExternalRequest(
      text(row, "external_request_id"),
      status,
      optionalText(row, "query_id") ?? undefined,
      artifactSha256,
    );
  }

  private p03QueueRunFromRow(row: SqlRow): P03QueueRunSummary {
    return {
      schemaVersion: CONTRACT_SCHEMA_VERSION,
      queueRunId: text(row, "id"),
      campaignId: text(row, "campaign_id"),
      ordinal: numberValue(row, "ordinal") as 1 | 2,
      stageId: "P03",
      backend: "tianyan176",
      shots: 100,
      purpose: text(row, "purpose") as P03QueueRunSummary["purpose"],
      lifecycleState: text(row, "lifecycle_state") as P03QueueLifecycleState,
      circuitHash: optionalText(row, "circuit_hash"),
      quantumJobId: optionalText(row, "quantum_job_id"),
      reservedExecutionSeconds: numberValue(row, "reserved_execution_seconds"),
      queryId: optionalText(row, "query_id"),
      providerStatus: optionalText(row, "provider_status"),
      machineStatus: optionalText(row, "machine_status"),
      queuePosition: optionalNumberValue(row, "queue_position"),
      estimatedStartAt: optionalText(row, "estimated_start_at"),
      lastQueriedAt: optionalText(row, "last_queried_at"),
      nextQueryAt: optionalText(row, "next_query_at"),
      pollAttempts: numberValue(row, "poll_attempts"),
      queueEnteredAt: optionalText(row, "queue_entered_at"),
      terminalAt: optionalText(row, "terminal_at"),
      checkpoint30mAt: optionalText(row, "checkpoint_30m_at"),
      unknownSubmission: numberValue(row, "unknown_submission") === 1,
      discoveryArtifactSha256: optionalText(row, "discovery_artifact_sha256"),
      stateArtifactSha256: optionalText(row, "state_artifact_sha256"),
      rawResultArtifactSha256: optionalText(row, "raw_result_artifact_sha256"),
      createdAt: text(row, "created_at"),
      updatedAt: text(row, "updated_at"),
    };
  }

  getP03QueueRun(queueRunId: string): P03QueueRunSummary {
    const row = this.database.prepare("SELECT * FROM p03_queue_runs WHERE id = ?")
      .get(queueRunId) as SqlRow | undefined;
    if (!row) throw new Error(`P03 queue run ${queueRunId} was not found`);
    return this.p03QueueRunFromRow(row);
  }

  listP03QueueRuns(campaignId: string): P03QueueRunSummary[] {
    return rows(this.database.prepare(
      "SELECT * FROM p03_queue_runs WHERE campaign_id = ? ORDER BY ordinal, created_at",
    ), campaignId).map((row) => this.p03QueueRunFromRow(row));
  }

  getP03HardwareBudget(campaignId: string): P03HardwareBudgetSummary {
    const p02 = this.database.prepare(
      `SELECT COUNT(*) AS jobs, COALESCE(SUM(q.shots), 0) AS shots,
       COALESCE(SUM(COALESCE(q.actual_execution_seconds, q.estimated_execution_seconds, 0)), 0) AS seconds
       FROM quantum_jobs q LEFT JOIN p03_queue_runs p ON p.quantum_job_id = q.id
       WHERE q.campaign_id = ? AND q.target_type = 'HARDWARE' AND p.id IS NULL`,
    ).get(campaignId) as SqlRow;
    const p03 = this.database.prepare(
      `SELECT COUNT(*) AS jobs, COALESCE(SUM(shots), 0) AS shots,
       COALESCE(SUM(reserved_execution_seconds), 0) AS seconds
       FROM p03_queue_runs WHERE campaign_id = ? AND quantum_job_id IS NOT NULL`,
    ).get(campaignId) as SqlRow;
    const p02HistoricalJobs = numberValue(p02, "jobs");
    const p02HistoricalShots = numberValue(p02, "shots");
    const p02HistoricalExecutionSeconds = numberValue(p02, "seconds");
    const p03Jobs = numberValue(p03, "jobs");
    const p03Shots = numberValue(p03, "shots");
    const p03ReservedExecutionSeconds = numberValue(p03, "seconds");
    return {
      p02HistoricalJobs,
      p02HistoricalShots,
      p02HistoricalExecutionSeconds,
      p03Jobs,
      p03Shots,
      p03ReservedExecutionSeconds,
      p03MaxJobs: 2,
      p03MaxShots: 200,
      p03MaxExecutionSeconds: 600,
      lifecycleJobs: p02HistoricalJobs + p03Jobs,
      lifecycleShots: p02HistoricalShots + p03Shots,
      lifecycleConservativeExecutionSeconds: p02HistoricalExecutionSeconds + p03ReservedExecutionSeconds,
    };
  }

  ensureP03QueueRun(input: {
    campaignId: string;
    ordinal: 1 | 2;
    authorizationHash: string;
    backend: "tianyan176";
    shots: 100;
    purpose: P03QueueRunSummary["purpose"];
    secondJobEvidenceSha256?: string;
  }): P03QueueRunSummary {
    if (!/^[a-f0-9]{64}$/u.test(input.authorizationHash)) throw new Error("P03 authorization hash is invalid");
    if (input.backend !== "tianyan176" || input.shots !== 100) throw new Error("P03 backend or shots exceed authorization");
    const existing = this.database.prepare(
      "SELECT id FROM p03_queue_runs WHERE campaign_id = ? AND ordinal = ?",
    ).get(input.campaignId, input.ordinal) as SqlRow | undefined;
    if (existing) {
      const run = this.getP03QueueRun(text(existing, "id"));
      if (run.backend !== input.backend || run.shots !== input.shots || run.purpose !== input.purpose) {
        throw new Error("P03 queue run identity changed; replacement is prohibited");
      }
      return run;
    }
    if (input.ordinal === 2) {
      if (!input.secondJobEvidenceSha256 || !/^[a-f0-9]{64}$/u.test(input.secondJobEvidenceSha256)) {
        throw new Error("second P03 job requires terminal defect or new-evidence evidence");
      }
      const first = this.listP03QueueRuns(input.campaignId).find((run) => run.ordinal === 1);
      if (!first || !new Set<P03QueueLifecycleState>(["COMPLETED", "FAILED", "CANCELLED"]).has(first.lifecycleState)) {
        throw new Error("second P03 job requires the first job to reach a terminal evidence state");
      }
    }
    this.getCampaign(input.campaignId);
    const id = `p03_queue_${randomUUID()}`;
    const timestamp = now();
    this.transaction(() => {
      this.database.prepare(
        `INSERT OR IGNORE INTO resource_authorizations(
          id, campaign_id, authorization_hash, source, scope_json, approved_at
        ) VALUES (?, ?, ?, 'USER_STANDING', ?, ?)`,
      ).run(
        `authorization_${randomUUID()}`,
        input.campaignId,
        input.authorizationHash,
        JSON.stringify({ stageId: "P03", backend: input.backend, shotsPerJob: input.shots }),
        timestamp,
      );
      this.database.prepare(
        `INSERT INTO p03_queue_runs(
          id, campaign_id, ordinal, stage_id, authorization_hash, backend, shots, purpose,
          lifecycle_state, second_job_evidence_sha256, created_at, updated_at
        ) VALUES (?, ?, ?, 'P03', ?, ?, ?, ?, 'DISCOVERING', ?, ?, ?)`,
      ).run(
        id,
        input.campaignId,
        input.ordinal,
        input.authorizationHash,
        input.backend,
        input.shots,
        input.purpose,
        input.secondJobEvidenceSha256 ?? null,
        timestamp,
        timestamp,
      );
    });
    return this.getP03QueueRun(id);
  }

  recordP03Discovery(queueRunId: string, machineStatus: string | null, artifactSha256: string): P03QueueRunSummary {
    this.database.prepare(
      `UPDATE p03_queue_runs SET machine_status = ?, discovery_artifact_sha256 = ?,
       state_artifact_sha256 = ?, updated_at = ? WHERE id = ? AND lifecycle_state = 'DISCOVERING'`,
    ).run(machineStatus, artifactSha256, artifactSha256, now(), queueRunId);
    return this.getP03QueueRun(queueRunId);
  }

  markP03BackendUnavailable(queueRunId: string, artifactSha256: string): P03QueueRunSummary {
    this.database.prepare(
      `UPDATE p03_queue_runs SET lifecycle_state = 'BACKEND_UNAVAILABLE', state_artifact_sha256 = ?,
       terminal_at = ?, updated_at = ? WHERE id = ? AND lifecycle_state = 'DISCOVERING'`,
    ).run(artifactSha256, now(), now(), queueRunId);
    return this.getP03QueueRun(queueRunId);
  }

  prepareP03QueueSubmission(input: {
    queueRunId: string;
    circuitHash: string;
    requestHash: string;
    idempotencyKey: string;
    reservedExecutionSeconds: number;
  }): P03QueueRunSummary {
    if (!/^[a-f0-9]{64}$/u.test(input.circuitHash) || !/^[a-f0-9]{64}$/u.test(input.requestHash)) {
      throw new Error("P03 circuit or request hash is invalid");
    }
    if (input.reservedExecutionSeconds !== 240) throw new Error("P03 execution reservation must be 240 seconds");
    const current = this.getP03QueueRun(input.queueRunId);
    if (current.lifecycleState !== "DISCOVERING") {
      const row = this.database.prepare("SELECT request_hash, idempotency_key FROM p03_queue_runs WHERE id = ?")
        .get(input.queueRunId) as SqlRow;
      if (
        current.circuitHash === input.circuitHash
        && optionalText(row, "request_hash") === input.requestHash
        && optionalText(row, "idempotency_key") === input.idempotencyKey
        && current.reservedExecutionSeconds === input.reservedExecutionSeconds
      ) return current;
      throw new Error("P03 prepared submission identity changed; replacement is prohibited");
    }
    const externalRequestId = `external_${randomUUID()}`;
    const quantumJobId = `quantum_job_${randomUUID()}`;
    const timestamp = now();
    this.transaction(() => {
      const totals = this.database.prepare(
        `SELECT COUNT(*) AS jobs, COALESCE(SUM(shots), 0) AS shots,
         COALESCE(SUM(reserved_execution_seconds), 0) AS seconds
         FROM p03_queue_runs WHERE campaign_id = ? AND quantum_job_id IS NOT NULL`,
      ).get(current.campaignId) as SqlRow;
      if (numberValue(totals, "jobs") >= 2) throw new Error("P03 hardware job budget is exhausted");
      if (numberValue(totals, "shots") + current.shots > 200) throw new Error("P03 shot budget would be exceeded");
      if (numberValue(totals, "seconds") + input.reservedExecutionSeconds > 600) {
        throw new Error("P03 execution budget would be exceeded");
      }
      const authorization = this.database.prepare(
        "SELECT authorization_hash FROM p03_queue_runs WHERE id = ?",
      ).get(input.queueRunId) as SqlRow;
      this.database.prepare(
        `INSERT INTO external_requests(
          id, campaign_id, provider, request_kind, target, idempotency_key, approval_hash,
          request_hash, status, created_at, updated_at
        ) VALUES (?, ?, 'tianyan', 'P03_HARDWARE_SUBMIT', 'tianyan176', ?, ?, ?, 'PREPARED', ?, ?)`,
      ).run(
        externalRequestId,
        current.campaignId,
        input.idempotencyKey,
        text(authorization, "authorization_hash"),
        input.requestHash,
        timestamp,
        timestamp,
      );
      this.database.prepare(
        `INSERT INTO quantum_jobs(
          id, campaign_id, external_request_id, purpose, backend, target_type, circuit_hash,
          shots, estimated_execution_seconds, terminal_status, created_at, updated_at
        ) VALUES (?, ?, ?, 'representative_qgnn_subcircuit', 'tianyan176', 'HARDWARE', ?, ?, ?,
          'READY_TO_SUBMIT', ?, ?)`,
      ).run(
        quantumJobId,
        current.campaignId,
        externalRequestId,
        input.circuitHash,
        current.shots,
        input.reservedExecutionSeconds,
        timestamp,
        timestamp,
      );
      this.database.prepare(
        `INSERT INTO budget_ledger(
          id, campaign_id, resource_kind, action_key, calls, shots, execution_seconds,
          expected_evidence, created_at
        ) VALUES (?, ?, 'TIANYAN_HARDWARE_P03_RESERVATION', ?, 0, ?, ?, ?, ?)`,
      ).run(
        `budget_${randomUUID()}`,
        current.campaignId,
        input.idempotencyKey,
        current.shots,
        input.reservedExecutionSeconds,
        "P03 tianyan176 original Query ID, queue lifecycle and terminal raw result",
        timestamp,
      );
      this.database.prepare(
        `UPDATE p03_queue_runs SET lifecycle_state = 'READY_TO_SUBMIT', circuit_hash = ?,
         request_hash = ?, idempotency_key = ?, external_request_id = ?, quantum_job_id = ?,
         reserved_execution_seconds = ?, updated_at = ? WHERE id = ?`,
      ).run(
        input.circuitHash,
        input.requestHash,
        input.idempotencyKey,
        externalRequestId,
        quantumJobId,
        input.reservedExecutionSeconds,
        timestamp,
        input.queueRunId,
      );
    });
    return this.getP03QueueRun(input.queueRunId);
  }

  markP03Submitting(queueRunId: string): P03QueueRunSummary {
    const run = this.getP03QueueRun(queueRunId);
    if (run.lifecycleState === "UNKNOWN") throw new Error("UNKNOWN submission is query-only; resubmission is prohibited");
    if (run.lifecycleState !== "READY_TO_SUBMIT") {
      throw new Error(`P03 queue run cannot submit from ${run.lifecycleState}; resubmission is prohibited`);
    }
    const timestamp = now();
    this.transaction(() => {
      this.database.prepare(
        "UPDATE p03_queue_runs SET lifecycle_state = 'SUBMITTING', updated_at = ? WHERE id = ?",
      ).run(timestamp, queueRunId);
      this.database.prepare(
        "UPDATE external_requests SET status = 'COMMITTING', updated_at = ? WHERE id = (SELECT external_request_id FROM p03_queue_runs WHERE id = ?)",
      ).run(timestamp, queueRunId);
      this.database.prepare(
        "UPDATE quantum_jobs SET terminal_status = 'SUBMITTING', updated_at = ? WHERE id = (SELECT quantum_job_id FROM p03_queue_runs WHERE id = ?)",
      ).run(timestamp, queueRunId);
    });
    return this.getP03QueueRun(queueRunId);
  }

  markP03Submitted(
    queueRunId: string,
    queryId: string,
    providerStatus: string | null,
    nextQueryAt: string,
  ): P03QueueRunSummary {
    if (!queryId.trim()) throw new Error("P03 Query ID must not be empty");
    const run = this.getP03QueueRun(queueRunId);
    if (run.queryId && run.queryId !== queryId) throw new Error("P03 resubmission with a different Query ID is prohibited");
    if (!new Set<P03QueueLifecycleState>(["SUBMITTING", "QUEUED", "RUNNING", "UNKNOWN"]).has(run.lifecycleState)) {
      throw new Error(`P03 Query ID cannot be recorded from ${run.lifecycleState}`);
    }
    const timestamp = now();
    this.transaction(() => {
      this.database.prepare(
        `UPDATE p03_queue_runs SET lifecycle_state = 'QUEUED', query_id = ?, provider_status = ?,
         queue_entered_at = COALESCE(queue_entered_at, ?), next_query_at = ?, unknown_submission = 0,
         updated_at = ? WHERE id = ?`,
      ).run(queryId, providerStatus, timestamp, nextQueryAt, timestamp, queueRunId);
      this.database.prepare(
        `UPDATE quantum_jobs SET query_id = ?, terminal_status = 'QUEUED', updated_at = ?
         WHERE id = (SELECT quantum_job_id FROM p03_queue_runs WHERE id = ?) AND (query_id IS NULL OR query_id = ?)`,
      ).run(queryId, timestamp, queueRunId, queryId);
      this.database.prepare(
        `UPDATE external_requests SET status = 'SUBMITTED', external_id = ?, updated_at = ?
         WHERE id = (SELECT external_request_id FROM p03_queue_runs WHERE id = ?)`,
      ).run(queryId, timestamp, queueRunId);
    });
    return this.getP03QueueRun(queueRunId);
  }

  markP03SubmissionUnknown(queueRunId: string, artifactSha256: string | null): P03QueueRunSummary {
    const run = this.getP03QueueRun(queueRunId);
    if (new Set<P03QueueLifecycleState>(["COMPLETED", "FAILED", "CANCELLED"]).has(run.lifecycleState)) return run;
    const timestamp = now();
    this.transaction(() => {
      this.database.prepare(
        `UPDATE p03_queue_runs SET lifecycle_state = 'UNKNOWN', unknown_submission = 1,
         state_artifact_sha256 = COALESCE(?, state_artifact_sha256), updated_at = ? WHERE id = ?`,
      ).run(artifactSha256, timestamp, queueRunId);
      this.database.prepare(
        `UPDATE external_requests SET status = 'UNKNOWN', response_artifact_sha256 = COALESCE(?, response_artifact_sha256),
         updated_at = ? WHERE id = (SELECT external_request_id FROM p03_queue_runs WHERE id = ?)`,
      ).run(artifactSha256, timestamp, queueRunId);
      this.database.prepare(
        `UPDATE quantum_jobs SET terminal_status = 'UNKNOWN', updated_at = ?
         WHERE id = (SELECT quantum_job_id FROM p03_queue_runs WHERE id = ?)`,
      ).run(timestamp, queueRunId);
    });
    return this.getP03QueueRun(queueRunId);
  }

  recordP03StateArtifact(queueRunId: string, artifactSha256: string): P03QueueRunSummary {
    this.database.prepare(
      "UPDATE p03_queue_runs SET state_artifact_sha256 = ?, updated_at = ? WHERE id = ?",
    ).run(artifactSha256, now(), queueRunId);
    return this.getP03QueueRun(queueRunId);
  }

  recordP03Poll(input: {
    queueRunId: string;
    lifecycleState: "QUEUED" | "RUNNING" | "UNKNOWN";
    providerStatus: string | null;
    lastQueriedAt: string;
    nextQueryAt: string;
    stateArtifactSha256: string;
  }): P03QueueRunSummary {
    const run = this.getP03QueueRun(input.queueRunId);
    if (!new Set<P03QueueLifecycleState>(["QUEUED", "RUNNING", "UNKNOWN"]).has(run.lifecycleState)) {
      throw new Error(`P03 queue run cannot be polled from ${run.lifecycleState}`);
    }
    this.database.prepare(
      `UPDATE p03_queue_runs SET lifecycle_state = ?, provider_status = ?, last_queried_at = ?,
       next_query_at = ?, poll_attempts = poll_attempts + 1, state_artifact_sha256 = ?,
       running_entered_at = CASE WHEN ? = 'RUNNING' THEN COALESCE(running_entered_at, ?) ELSE running_entered_at END,
       updated_at = ? WHERE id = ?`,
    ).run(
      input.lifecycleState,
      input.providerStatus,
      input.lastQueriedAt,
      input.nextQueryAt,
      input.stateArtifactSha256,
      input.lifecycleState,
      input.lastQueriedAt,
      now(),
      input.queueRunId,
    );
    return this.getP03QueueRun(input.queueRunId);
  }

  markP03Checkpoint30m(queueRunId: string): P03QueueRunSummary {
    this.database.prepare(
      "UPDATE p03_queue_runs SET checkpoint_30m_at = COALESCE(checkpoint_30m_at, ?), updated_at = ? WHERE id = ?",
    ).run(now(), now(), queueRunId);
    return this.getP03QueueRun(queueRunId);
  }

  backfillP03TerminalQueryTimestamp(queueRunId: string): P03QueueRunSummary {
    this.database.prepare(
      `UPDATE p03_queue_runs SET last_queried_at = COALESCE(last_queried_at, terminal_at), updated_at = ?
       WHERE id = ? AND terminal_at IS NOT NULL`,
    ).run(now(), queueRunId);
    return this.getP03QueueRun(queueRunId);
  }

  completeP03QueueRun(input: {
    queueRunId: string;
    terminalState: "COMPLETED" | "FAILED" | "CANCELLED";
    providerStatus: string;
    rawResultArtifactSha256: string;
  }): P03QueueRunSummary {
    const run = this.getP03QueueRun(input.queueRunId);
    if (!run.queryId || !run.quantumJobId) throw new Error("P03 terminal result requires the original Query ID and quantum job");
    const timestamp = now();
    this.transaction(() => {
      this.database.prepare(
        `UPDATE p03_queue_runs SET lifecycle_state = ?, provider_status = ?, raw_result_artifact_sha256 = ?,
         state_artifact_sha256 = ?, last_queried_at = ?, terminal_at = ?, next_query_at = NULL, updated_at = ? WHERE id = ?`,
      ).run(
        input.terminalState,
        input.providerStatus,
        input.rawResultArtifactSha256,
        input.rawResultArtifactSha256,
        timestamp,
        timestamp,
        timestamp,
        input.queueRunId,
      );
      this.database.prepare(
        `UPDATE quantum_jobs SET terminal_status = ?, raw_result_artifact_sha256 = ?,
         actual_execution_seconds = estimated_execution_seconds, updated_at = ? WHERE id = ?`,
      ).run(input.terminalState, input.rawResultArtifactSha256, timestamp, run.quantumJobId);
      this.database.prepare(
        `UPDATE external_requests SET status = ?, response_artifact_sha256 = ?, updated_at = ?
         WHERE id = (SELECT external_request_id FROM p03_queue_runs WHERE id = ?)`,
      ).run(input.terminalState, input.rawResultArtifactSha256, timestamp, input.queueRunId);
      this.database.prepare(
        `UPDATE budget_ledger SET calls = 1 WHERE campaign_id = ? AND resource_kind = 'TIANYAN_HARDWARE_P03_RESERVATION'
         AND action_key = (SELECT idempotency_key FROM p03_queue_runs WHERE id = ?)`,
      ).run(run.campaignId, input.queueRunId);
    });
    return this.getP03QueueRun(input.queueRunId);
  }

  getDetail(campaignId: string): CampaignDetail {
    return {
      campaign: this.getCampaign(campaignId),
      checkpoints: this.listCheckpoints(campaignId),
      actions: this.listActions(campaignId),
      failures: this.listFailures(campaignId),
      quantumJobs: this.listQuantumJobs(campaignId),
      p03QueueRuns: this.listP03QueueRuns(campaignId),
    };
  }
}
