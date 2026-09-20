import { createHash, randomUUID } from "node:crypto";
import type { DatabaseSync, StatementSync } from "node:sqlite";

import type { JsonObject, JsonValue } from "@q-fintelligence/contracts";

export const P07_ROLES = [
  "input_security_router",
  "research_supervisor",
  "data_agent",
  "classical_algorithm_agent",
  "quantum_algorithm_agent",
  "circuit_compiler_agent",
  "tool_builder_agent",
  "experiment_runner",
  "scientific_critic",
  "archive_release_gate",
] as const;

export const P07_LANES = [
  "data_quality",
  "classical_algorithms",
  "quantum_circuits",
  "capability_validation",
  "deepseek_agents",
  "visualization_archive",
] as const;

export type P07Role = (typeof P07_ROLES)[number];
export type P07Lane = (typeof P07_LANES)[number];
export type P07Status = "AWAITING_AUTHORIZATION" | "RUNNING" | "BLOCKED" | "FAILED" | "COMPLETED";
export type P07Provider = "deepseek/getoken" | "openai/getoken";
export type P07ModelId = "deepseek-v4-pro" | "gpt-5.6";

export function assertP07ModelLock(provider: P07Provider, modelId: P07ModelId): void {
  if (!((provider === "deepseek/getoken" && modelId === "deepseek-v4-pro")
    || (provider === "openai/getoken" && modelId === "gpt-5.6"))) {
    throw new Error(`unsupported P07 model lock ${provider}/${modelId}`);
  }
}

type SqlRow = Record<string, unknown>;

function now(): string {
  return new Date().toISOString();
}

function rows(statement: StatementSync, ...parameters: (string | number | null)[]): SqlRow[] {
  return statement.all(...parameters) as SqlRow[];
}

function text(row: SqlRow, key: string): string {
  const value = row[key];
  if (typeof value !== "string") throw new Error(`expected ${key} to be text`);
  return value;
}

function optionalText(row: SqlRow, key: string): string | null {
  const value = row[key];
  if (value === null || value === undefined) return null;
  if (typeof value !== "string") throw new Error(`expected ${key} to be nullable text`);
  return value;
}

function numeric(row: SqlRow, key: string): number {
  const value = row[key];
  if (typeof value !== "number") throw new Error(`expected ${key} to be numeric`);
  return value;
}

function object(value: string | null): JsonObject | null {
  if (value === null) return null;
  const parsed = JSON.parse(value) as unknown;
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error("persisted P07 JSON object is invalid");
  }
  return parsed as JsonObject;
}

function array(value: string): JsonValue[] {
  const parsed = JSON.parse(value) as unknown;
  if (!Array.isArray(parsed)) throw new Error("persisted P07 JSON array is invalid");
  return parsed as JsonValue[];
}

function hash(value: unknown): string {
  return createHash("sha256").update(typeof value === "string" ? value : JSON.stringify(value)).digest("hex");
}

export interface P07CampaignSummary {
  campaignId: string;
  taskId: string;
  conversationId: string;
  predecessorCampaignId: string | null;
  objective: string;
  provider: P07Provider;
  modelId: P07ModelId;
  status: P07Status;
  stage: string;
  formalTestSealed: true;
  minimumRuntimeSeconds: number;
  minimumVerifiedTokens: number;
  targetVerifiedTokens: number;
  verifiedPromptTokens: number;
  verifiedCompletionTokens: number;
  verifiedTotalTokens: number;
  stableConcurrency: number;
  newExternalCallsAllowed: boolean;
  browserGate: "PENDING" | "PASS" | "FAIL";
  engineeringGate: "PENDING" | "PASS" | "FAIL";
  checkpoint: JsonObject;
  error: JsonObject | null;
  startedAt: string | null;
  completedAt: string | null;
  heartbeatAt: string | null;
  createdAt: string;
  updatedAt: string;
  wallClockSeconds: number;
}

export interface P07TokenLedgerRow {
  callId: string;
  runId: string;
  role: string;
  purpose: string;
  provider: P07Provider;
  modelId: P07ModelId;
  status: "STARTED" | "COMPLETED" | "FAILED" | "UNKNOWN" | "REUSED";
  idempotencyKey: string;
  promptSha256: string;
  responseSha256: string | null;
  promptTokens: number;
  completionTokens: number;
  cachedTokens: number;
  totalTokens: number;
  responseArtifactSha256: string | null;
  errorCode: string | null;
  startedAt: string;
  completedAt: string | null;
}

export class P07Repository {
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

  createCampaign(input: {
    conversationId: string;
    taskId: string;
    objective: string;
    provider?: P07Provider;
    modelId?: P07ModelId;
    predecessorCampaignId?: string;
  }): string {
    const existing = this.database.prepare("SELECT id FROM p07_campaigns WHERE conversation_id = ?")
      .get(input.conversationId) as SqlRow | undefined;
    if (existing) return text(existing, "id");
    const provider = input.provider ?? "deepseek/getoken";
    const modelId = input.modelId ?? "deepseek-v4-pro";
    assertP07ModelLock(provider, modelId);
    if (input.predecessorCampaignId) this.getCampaign(input.predecessorCampaignId);
    const campaignId = `p07_campaign_${randomUUID()}`;
    const timestamp = now();
    this.transaction(() => {
      this.database.prepare(
        `INSERT INTO p07_campaigns(
          id, task_id, conversation_id, predecessor_campaign_id, objective, provider, model_id, status, stage,
          created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?,
          'AWAITING_AUTHORIZATION', 'input_security', ?, ?)`,
      ).run(campaignId, input.taskId, input.conversationId, input.predecessorCampaignId ?? null,
        input.objective, provider, modelId, timestamp, timestamp);
      for (const lane of P07_LANES) {
        this.database.prepare(
          `INSERT INTO p07_runs(id, campaign_id, lane, status, created_at, updated_at)
           VALUES (?, ?, ?, 'CREATED', ?, ?)`,
        ).run(`p07_run_${lane}_${randomUUID()}`, campaignId, lane, timestamp, timestamp);
      }
    });
    return campaignId;
  }

  authorize(campaignId: string, authorizationHash: string): void {
    if (!/^[a-f0-9]{64}$/u.test(authorizationHash)) throw new Error("P07 authorization hash is invalid");
    const timestamp = now();
    const result = this.database.prepare(
      `UPDATE p07_campaigns SET authorization_hash = ?, status = 'RUNNING', stage = 'preflight',
       new_external_calls_allowed = 1, started_at = COALESCE(started_at, ?), heartbeat_at = ?, updated_at = ?
       WHERE id = ? AND status IN ('AWAITING_AUTHORIZATION','BLOCKED')`,
    ).run(authorizationHash, timestamp, timestamp, timestamp, campaignId);
    if (Number(result.changes) !== 1) throw new Error("P07 campaign cannot be authorized from its current state");
  }

  private campaignFromRow(row: SqlRow): P07CampaignSummary {
    const startedAt = optionalText(row, "started_at");
    const completedAt = optionalText(row, "completed_at");
    const end = completedAt ? Date.parse(completedAt) : Date.now();
    return {
      campaignId: text(row, "id"),
      taskId: text(row, "task_id"),
      conversationId: text(row, "conversation_id"),
      predecessorCampaignId: optionalText(row, "predecessor_campaign_id"),
      objective: text(row, "objective"),
      provider: text(row, "provider") as P07Provider,
      modelId: text(row, "model_id") as P07ModelId,
      status: text(row, "status") as P07Status,
      stage: text(row, "stage"),
      formalTestSealed: true,
      minimumRuntimeSeconds: numeric(row, "minimum_runtime_seconds"),
      minimumVerifiedTokens: numeric(row, "minimum_verified_tokens"),
      targetVerifiedTokens: numeric(row, "target_verified_tokens"),
      verifiedPromptTokens: numeric(row, "verified_prompt_tokens"),
      verifiedCompletionTokens: numeric(row, "verified_completion_tokens"),
      verifiedTotalTokens: numeric(row, "verified_total_tokens"),
      stableConcurrency: numeric(row, "stable_concurrency"),
      newExternalCallsAllowed: numeric(row, "new_external_calls_allowed") === 1,
      browserGate: text(row, "browser_gate") as P07CampaignSummary["browserGate"],
      engineeringGate: text(row, "engineering_gate") as P07CampaignSummary["engineeringGate"],
      checkpoint: object(text(row, "checkpoint_json")) ?? {},
      error: object(optionalText(row, "error_json")),
      startedAt,
      completedAt,
      heartbeatAt: optionalText(row, "heartbeat_at"),
      createdAt: text(row, "created_at"),
      updatedAt: text(row, "updated_at"),
      wallClockSeconds: startedAt ? Math.max(0, (end - Date.parse(startedAt)) / 1000) : 0,
    };
  }

  getCampaign(campaignId: string): P07CampaignSummary {
    const row = this.database.prepare("SELECT * FROM p07_campaigns WHERE id = ?").get(campaignId) as SqlRow | undefined;
    if (!row) throw new Error(`P07 campaign ${campaignId} was not found`);
    return this.campaignFromRow(row);
  }

  listCampaigns(): P07CampaignSummary[] {
    return rows(this.database.prepare("SELECT * FROM p07_campaigns ORDER BY created_at DESC"))
      .map((row) => this.campaignFromRow(row));
  }

  setCampaign(input: {
    campaignId: string;
    status?: P07Status;
    stage?: string;
    stableConcurrency?: number;
    allowExternalCalls?: boolean;
    browserGate?: "PENDING" | "PASS" | "FAIL";
    engineeringGate?: "PENDING" | "PASS" | "FAIL";
    checkpoint?: JsonObject;
    error?: JsonObject | null;
    complete?: boolean;
  }): void {
    const current = this.getCampaign(input.campaignId);
    const timestamp = now();
    this.database.prepare(
      `UPDATE p07_campaigns SET status = ?, stage = ?, stable_concurrency = ?,
       new_external_calls_allowed = ?, browser_gate = ?, engineering_gate = ?, checkpoint_json = ?,
       error_json = ?, heartbeat_at = ?, completed_at = ?, updated_at = ? WHERE id = ?`,
    ).run(
      input.status ?? current.status,
      input.stage ?? current.stage,
      input.stableConcurrency ?? current.stableConcurrency,
      (input.allowExternalCalls ?? current.newExternalCallsAllowed) ? 1 : 0,
      input.browserGate ?? current.browserGate,
      input.engineeringGate ?? current.engineeringGate,
      JSON.stringify(input.checkpoint ?? current.checkpoint),
      input.error === undefined ? (current.error === null ? null : JSON.stringify(current.error))
        : input.error === null ? null : JSON.stringify(input.error),
      timestamp,
      input.complete ? timestamp : current.completedAt,
      timestamp,
      input.campaignId,
    );
  }

  freezeCampaignAtRunCompletion(campaignId: string): void {
    const row = this.database.prepare(
      "SELECT MAX(completed_at) AS completed_at FROM p07_runs WHERE campaign_id = ?",
    ).get(campaignId) as SqlRow;
    const completedAt = optionalText(row, "completed_at");
    if (!completedAt) throw new Error("P07 campaign has no terminal run completion to freeze");
    this.database.prepare(
      "UPDATE p07_campaigns SET completed_at = ?, heartbeat_at = ?, updated_at = ? WHERE id = ?",
    ).run(completedAt, completedAt, now(), campaignId);
  }

  ensureRole(input: {
    campaignId: string;
    role: P07Role;
    conversationId: string;
    promptVersion: string;
    promptHash: string;
    toolNames: readonly string[];
  }): void {
    const timestamp = now();
    this.database.prepare(
      `INSERT OR IGNORE INTO p07_roles(
        campaign_id, role, conversation_id, prompt_version, prompt_hash, tool_names_json,
        state, created_at, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, 'READY', ?, ?)`,
    ).run(
      input.campaignId,
      input.role,
      input.conversationId,
      input.promptVersion,
      input.promptHash,
      JSON.stringify([...input.toolNames]),
      timestamp,
      timestamp,
    );
  }

  getRun(campaignId: string, lane: P07Lane): SqlRow {
    const row = this.database.prepare("SELECT * FROM p07_runs WHERE campaign_id = ? AND lane = ?")
      .get(campaignId, lane) as SqlRow | undefined;
    if (!row) throw new Error(`P07 lane ${lane} was not found`);
    return row;
  }

  getRunId(campaignId: string, lane: P07Lane): string {
    return text(this.getRun(campaignId, lane), "id");
  }

  getRunCheckpoint(campaignId: string, lane: P07Lane): JsonObject {
    return object(text(this.getRun(campaignId, lane), "checkpoint_json")) ?? {};
  }

  acquireRun(campaignId: string, lane: P07Lane, workerId: string, processId: number, recovering = false): string {
    const row = this.getRun(campaignId, lane);
    const runId = text(row, "id");
    const timestamp = now();
    const expiresAt = new Date(Date.now() + 90_000).toISOString();
    this.database.prepare(
      `UPDATE p07_runs SET status = ?, worker_id = ?, process_id = ?, lease_acquired_at = ?,
       heartbeat_at = ?, lease_expires_at = ?, released_at = NULL,
       recovery_count = recovery_count + ?, started_at = COALESCE(started_at, ?), updated_at = ? WHERE id = ?`,
    ).run(recovering ? "RECOVERING" : "RUNNING", workerId, processId, timestamp, timestamp, expiresAt, recovering ? 1 : 0, timestamp, timestamp, runId);
    return runId;
  }

  heartbeatRun(runId: string, workerId: string, checkpoint?: JsonObject): void {
    const timestamp = now();
    const result = this.database.prepare(
      `UPDATE p07_runs SET heartbeat_at = ?, lease_expires_at = ?, checkpoint_json = COALESCE(?, checkpoint_json),
       updated_at = ? WHERE id = ? AND worker_id = ? AND released_at IS NULL`,
    ).run(timestamp, new Date(Date.now() + 90_000).toISOString(), checkpoint ? JSON.stringify(checkpoint) : null, timestamp, runId, workerId);
    if (Number(result.changes) !== 1) throw new Error(`P07 lease heartbeat rejected for ${runId}`);
  }

  finishRun(runId: string, status: "BLOCKED" | "FAILED" | "COMPLETED", checkpoint: JsonObject = {}): void {
    const timestamp = now();
    this.database.prepare(
      `UPDATE p07_runs SET status = ?, checkpoint_json = ?, completed_at = ?, released_at = ?, updated_at = ? WHERE id = ?`,
    ).run(status, JSON.stringify(checkpoint), timestamp, timestamp, timestamp, runId);
  }

  appendEvent(input: {
    campaignId: string;
    lane?: P07Lane;
    eventType: string;
    idempotencyKey: string;
    payload: JsonObject;
  }): { eventId: string; sequence: number } {
    const existing = this.database.prepare("SELECT event_id, sequence FROM p07_events WHERE idempotency_key = ?")
      .get(input.idempotencyKey) as SqlRow | undefined;
    if (existing) return { eventId: text(existing, "event_id"), sequence: numeric(existing, "sequence") };
    const eventId = `p07_event_${randomUUID()}`;
    const result = this.database.prepare(
      `INSERT INTO p07_events(event_id, campaign_id, lane, event_type, idempotency_key, payload_hash, payload_json, created_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
    ).run(eventId, input.campaignId, input.lane ?? null, input.eventType, input.idempotencyKey, hash(input.payload), JSON.stringify(input.payload), now());
    return { eventId, sequence: Number(result.lastInsertRowid) };
  }

  listEvents(campaignId: string, after = 0, limit = 500): Array<JsonObject & { sequence: number; eventId: string }> {
    return rows(this.database.prepare(
      `SELECT * FROM p07_events WHERE campaign_id = ? AND sequence > ? ORDER BY sequence LIMIT ?`,
    ), campaignId, after, Math.min(Math.max(limit, 1), 2_000)).map((row) => ({
      ...(object(text(row, "payload_json")) ?? {}),
      sequence: numeric(row, "sequence"),
      eventId: text(row, "event_id"),
      eventType: text(row, "event_type"),
      lane: optionalText(row, "lane"),
      createdAt: text(row, "created_at"),
    }));
  }

  listAllEvents(campaignId: string): Array<JsonObject & { sequence: number; eventId: string }> {
    return rows(this.database.prepare(
      "SELECT * FROM p07_events WHERE campaign_id = ? ORDER BY sequence",
    ), campaignId).map((row) => ({
      ...(object(text(row, "payload_json")) ?? {}),
      sequence: numeric(row, "sequence"),
      eventId: text(row, "event_id"),
      eventType: text(row, "event_type"),
      lane: optionalText(row, "lane"),
      createdAt: text(row, "created_at"),
    }));
  }

  beginModelCall(input: {
    campaignId: string;
    runId: string;
    role: P07Role;
    purpose: string;
    idempotencyKey: string;
    promptSha256: string;
  }): P07TokenLedgerRow {
    const existing = this.database.prepare("SELECT * FROM p07_token_ledger WHERE idempotency_key = ?")
      .get(input.idempotencyKey) as SqlRow | undefined;
    if (existing) {
      if (text(existing, "status") === "FAILED") {
        this.database.prepare(
          `UPDATE p07_token_ledger SET status = 'STARTED', prompt_sha256 = ?, error_code = NULL,
           retry_count = retry_count + 1, started_at = ?, completed_at = NULL WHERE id = ?`,
        ).run(input.promptSha256, now(), text(existing, "id"));
        return this.getModelCall(text(existing, "id"));
      }
      return this.ledgerFromRow(existing);
    }
    const compatible = this.database.prepare(
      `SELECT * FROM p07_token_ledger WHERE campaign_id = ? AND role = ? AND purpose = ?
       AND status IN ('COMPLETED','REUSED','UNKNOWN','STARTED')
       ORDER BY CASE status WHEN 'COMPLETED' THEN 0 WHEN 'REUSED' THEN 1
         WHEN 'UNKNOWN' THEN 2 ELSE 3 END, started_at DESC LIMIT 1`,
    ).get(input.campaignId, input.role, input.purpose) as SqlRow | undefined;
    if (compatible) return this.ledgerFromRow(compatible);
    const campaign = this.getCampaign(input.campaignId);
    const callId = `p07_call_${randomUUID()}`;
    this.database.prepare(
      `INSERT INTO p07_token_ledger(
        id, campaign_id, run_id, role, purpose, provider, model_id, idempotency_key,
        prompt_sha256, status, started_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'STARTED', ?)`,
    ).run(callId, input.campaignId, input.runId, input.role, input.purpose,
      campaign.provider, campaign.modelId, input.idempotencyKey, input.promptSha256, now());
    return this.getModelCall(callId);
  }

  private ledgerFromRow(row: SqlRow): P07TokenLedgerRow {
    return {
      callId: text(row, "id"),
      runId: text(row, "run_id"),
      role: text(row, "role"),
      purpose: text(row, "purpose"),
      provider: text(row, "provider") as P07Provider,
      modelId: text(row, "model_id") as P07ModelId,
      status: text(row, "status") as P07TokenLedgerRow["status"],
      idempotencyKey: text(row, "idempotency_key"),
      promptSha256: text(row, "prompt_sha256"),
      responseSha256: optionalText(row, "response_sha256"),
      promptTokens: numeric(row, "prompt_tokens"),
      completionTokens: numeric(row, "completion_tokens"),
      cachedTokens: numeric(row, "cached_tokens"),
      totalTokens: numeric(row, "total_tokens"),
      responseArtifactSha256: optionalText(row, "response_artifact_sha256"),
      errorCode: optionalText(row, "error_code"),
      startedAt: text(row, "started_at"),
      completedAt: optionalText(row, "completed_at"),
    };
  }

  getModelCall(callId: string): P07TokenLedgerRow {
    const row = this.database.prepare("SELECT * FROM p07_token_ledger WHERE id = ?").get(callId) as SqlRow | undefined;
    if (!row) throw new Error(`P07 model call ${callId} was not found`);
    return this.ledgerFromRow(row);
  }

  completeModelCall(input: {
    callId: string;
    responseSha256: string;
    promptTokens: number;
    completionTokens: number;
    cachedTokens: number;
    totalTokens: number;
    responseArtifactSha256: string;
  }): void {
    this.transaction(() => {
      const current = this.getModelCall(input.callId);
      if (current.status === "COMPLETED" || current.status === "REUSED") return;
      if (input.totalTokens <= 0 || input.totalTokens !== input.promptTokens + input.completionTokens + input.cachedTokens) {
        throw new Error("P07 Provider usage is not verifiable");
      }
      const timestamp = now();
      this.database.prepare(
        `UPDATE p07_token_ledger SET status = 'COMPLETED', response_sha256 = ?, prompt_tokens = ?,
         completion_tokens = ?, cached_tokens = ?, total_tokens = ?, response_artifact_sha256 = ?, completed_at = ?
         WHERE id = ? AND status = 'STARTED'`,
      ).run(input.responseSha256, input.promptTokens, input.completionTokens, input.cachedTokens, input.totalTokens, input.responseArtifactSha256, timestamp, input.callId);
      const row = this.database.prepare("SELECT campaign_id FROM p07_token_ledger WHERE id = ?").get(input.callId) as SqlRow;
      this.database.prepare(
        `UPDATE p07_campaigns SET verified_prompt_tokens = verified_prompt_tokens + ?,
         verified_completion_tokens = verified_completion_tokens + ?, verified_total_tokens = verified_total_tokens + ?,
         heartbeat_at = ?, updated_at = ? WHERE id = ?`,
      ).run(input.promptTokens + input.cachedTokens, input.completionTokens, input.totalTokens, timestamp, timestamp, text(row, "campaign_id"));
    });
  }

  failModelCall(callId: string, status: "FAILED" | "UNKNOWN", errorCode: string): void {
    this.database.prepare(
      `UPDATE p07_token_ledger SET status = ?, error_code = ?, completed_at = ? WHERE id = ? AND status = 'STARTED'`,
    ).run(status, errorCode.slice(0, 300), now(), callId);
  }

  recoverStartedModelCalls(campaignId: string): number {
    const result = this.database.prepare(
      `UPDATE p07_token_ledger SET status = 'UNKNOWN', error_code = 'CONTROL_PLANE_RESTART_UNKNOWN_USAGE', completed_at = ?
       WHERE campaign_id = ? AND status = 'STARTED'`,
    ).run(now(), campaignId);
    return Number(result.changes);
  }

  listLedger(campaignId: string, limit = 500): P07TokenLedgerRow[] {
    return rows(this.database.prepare(
      "SELECT * FROM p07_token_ledger WHERE campaign_id = ? ORDER BY started_at DESC LIMIT ?",
    ), campaignId, Math.min(Math.max(limit, 1), 5_000)).map((row) => this.ledgerFromRow(row));
  }

  registerSource(input: {
    campaignId: string;
    kind: "GITHUB" | "PAPER" | "DATA" | "PLATFORM_DOC";
    title: string;
    url: string;
    domain: string;
    version: string;
    commitHash?: string;
    license: string;
    maintenanceStatus: string;
    reviewArtifactSha256?: string;
  }): string {
    const existing = this.database.prepare(
      "SELECT id FROM p07_sources WHERE campaign_id = ? AND url = ? AND version = ?",
    ).get(input.campaignId, input.url, input.version) as SqlRow | undefined;
    if (existing) return text(existing, "id");
    const sourceId = `p07_source_${randomUUID()}`;
    this.database.prepare(
      `INSERT INTO p07_sources(id, campaign_id, kind, title, url, domain, version, commit_hash,
       license, maintenance_status, retrieved_at, review_artifact_sha256)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    ).run(sourceId, input.campaignId, input.kind, input.title, input.url, input.domain, input.version,
      input.commitHash ?? null, input.license, input.maintenanceStatus, now(), input.reviewArtifactSha256 ?? null);
    return sourceId;
  }

  registerCapability(input: {
    campaignId: string;
    name: string;
    kind: "CLASSICAL" | "QUANTUM_CIRCUIT";
    version: string;
    sourceId: string;
    status: "REVIEWING" | "APPROVED" | "REJECTED";
    adapterHash: string;
    schemaHash: string;
    benchmarkArtifactSha256?: string;
    knowledgeArtifactSha256?: string;
    rejectionReason?: string;
  }): string {
    const existing = this.database.prepare(
      "SELECT id FROM p07_capability_packages WHERE campaign_id = ? AND name = ? AND version = ?",
    ).get(input.campaignId, input.name, input.version) as SqlRow | undefined;
    const capabilityId = existing ? text(existing, "id") : `p07_capability_${randomUUID()}`;
    const timestamp = now();
    this.database.prepare(
      `INSERT INTO p07_capability_packages(id, campaign_id, name, capability_kind, version, source_id,
       status, adapter_hash, schema_hash, benchmark_artifact_sha256, knowledge_artifact_sha256,
       rejection_reason, created_at, updated_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT(campaign_id, name, version) DO UPDATE SET status=excluded.status,
       benchmark_artifact_sha256=excluded.benchmark_artifact_sha256,
       knowledge_artifact_sha256=excluded.knowledge_artifact_sha256,
       rejection_reason=excluded.rejection_reason, updated_at=excluded.updated_at`,
    ).run(capabilityId, input.campaignId, input.name, input.kind, input.version, input.sourceId,
      input.status, input.adapterHash, input.schemaHash, input.benchmarkArtifactSha256 ?? null,
      input.knowledgeArtifactSha256 ?? null, input.rejectionReason ?? null, timestamp, timestamp);
    return capabilityId;
  }

  createHardwareBatch(input: {
    campaignId: string;
    idempotencyKey: string;
    circuitHashes: string[];
    shots: number;
  }): string {
    if (input.circuitHashes.length < 1 || input.circuitHashes.length > 50) throw new Error("P07 hardware batch must contain 1-50 circuits");
    const existing = this.database.prepare("SELECT id FROM p07_hardware_batches WHERE idempotency_key = ?")
      .get(input.idempotencyKey) as SqlRow | undefined;
    if (existing) return text(existing, "id");
    const batchId = `p07_batch_${randomUUID()}`;
    const timestamp = now();
    this.database.prepare(
      `INSERT INTO p07_hardware_batches(id, campaign_id, backend, idempotency_key, status,
       circuit_count, shots, circuit_hashes_json, created_at, updated_at)
       VALUES (?, ?, 'tianyan176', ?, 'REGISTERED', ?, ?, ?, ?, ?)`,
    ).run(batchId, input.campaignId, input.idempotencyKey, input.circuitHashes.length, input.shots,
      JSON.stringify(input.circuitHashes), timestamp, timestamp);
    return batchId;
  }

  getHardwareBatch(batchId: string): {
    batchId: string;
    campaignId: string;
    status: "REGISTERED" | "COMMITTING" | "SUBMITTED" | "QUERYING" | "COMPLETED" | "UNKNOWN" | "FAILED";
    circuitHashes: string[];
    queryIds: string[];
    shots: number;
    resultArtifactSha256: string | null;
  } {
    const row = this.database.prepare("SELECT * FROM p07_hardware_batches WHERE id = ?").get(batchId) as SqlRow | undefined;
    if (!row) throw new Error(`P07 hardware batch ${batchId} was not found`);
    return {
      batchId: text(row, "id"),
      campaignId: text(row, "campaign_id"),
      status: text(row, "status") as "REGISTERED" | "COMMITTING" | "SUBMITTED" | "QUERYING" | "COMPLETED" | "UNKNOWN" | "FAILED",
      circuitHashes: array(text(row, "circuit_hashes_json")).map(String),
      queryIds: array(text(row, "query_ids_json")).map(String),
      shots: numeric(row, "shots"),
      resultArtifactSha256: optionalText(row, "result_artifact_sha256"),
    };
  }

  reuseCompletedHardwareBatch(campaignId: string, predecessorCampaignId: string): string {
    const source = this.database.prepare(
      `SELECT * FROM p07_hardware_batches
       WHERE campaign_id = ? AND status = 'COMPLETED' AND result_artifact_sha256 IS NOT NULL
       ORDER BY completed_at DESC LIMIT 1`,
    ).get(predecessorCampaignId) as SqlRow | undefined;
    if (!source) throw new Error("P07 continuation requires a completed predecessor hardware batch");
    const sourceBatchId = text(source, "id");
    const idempotencyKey = `${campaignId}:reuse:${sourceBatchId}`;
    const existing = this.database.prepare(
      "SELECT id FROM p07_hardware_batches WHERE idempotency_key = ?",
    ).get(idempotencyKey) as SqlRow | undefined;
    if (existing) return text(existing, "id");
    const batchId = `p07_batch_${randomUUID()}`;
    const timestamp = now();
    this.database.prepare(
      `INSERT INTO p07_hardware_batches(
        id, campaign_id, backend, idempotency_key, status, circuit_count, shots,
        circuit_hashes_json, query_ids_json, result_artifact_sha256, created_at,
        updated_at, completed_at, reuse_of_batch_id
      ) VALUES (?, ?, 'tianyan176', ?, 'COMPLETED', ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    ).run(batchId, campaignId, idempotencyKey, numeric(source, "circuit_count"),
      numeric(source, "shots"), text(source, "circuit_hashes_json"),
      text(source, "query_ids_json"), text(source, "result_artifact_sha256"),
      timestamp, timestamp, timestamp, sourceBatchId);
    return batchId;
  }

  registerCircuit(input: {
    campaignId: string;
    circuitHash: string;
    family: string;
    depth: number;
    shots: number;
    mapping: JsonObject;
    circuitIrArtifactSha256: string;
    qcisArtifactSha256: string;
  }): void {
    const timestamp = now();
    this.database.prepare(
      `INSERT OR IGNORE INTO p07_circuits(
       circuit_hash, campaign_id, family, depth, shots, mapping_json,
       circuit_ir_artifact_sha256, qcis_artifact_sha256, created_at, updated_at
       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    ).run(input.circuitHash, input.campaignId, input.family, input.depth, input.shots,
      JSON.stringify(input.mapping), input.circuitIrArtifactSha256, input.qcisArtifactSha256,
      timestamp, timestamp);
  }

  updateCircuitResult(input: {
    circuitHash: string;
    queryId: string;
    terminalState: string;
    rawResultArtifactSha256?: string;
  }): void {
    this.database.prepare(
      `UPDATE p07_circuits SET query_id = ?, terminal_state = ?,
       raw_result_artifact_sha256 = COALESCE(?, raw_result_artifact_sha256), updated_at = ?
       WHERE circuit_hash = ?`,
    ).run(input.queryId, input.terminalState, input.rawResultArtifactSha256 ?? null, now(), input.circuitHash);
  }

  updateHardwareBatch(input: {
    batchId: string;
    status: "COMMITTING" | "SUBMITTED" | "QUERYING" | "COMPLETED" | "UNKNOWN" | "FAILED";
    queryIds?: string[];
    resultArtifactSha256?: string;
  }): void {
    const current = this.database.prepare("SELECT * FROM p07_hardware_batches WHERE id = ?").get(input.batchId) as SqlRow | undefined;
    if (!current) throw new Error(`P07 hardware batch ${input.batchId} was not found`);
    const timestamp = now();
    this.database.prepare(
      `UPDATE p07_hardware_batches SET status = ?, query_ids_json = ?, result_artifact_sha256 = ?,
       completed_at = ?, updated_at = ? WHERE id = ?`,
    ).run(input.status, JSON.stringify(input.queryIds ?? array(text(current, "query_ids_json"))),
      input.resultArtifactSha256 ?? optionalText(current, "result_artifact_sha256"),
      input.status === "COMPLETED" ? timestamp : optionalText(current, "completed_at"), timestamp, input.batchId);
  }

  acquireHardwareLease(campaignId: string, batchId: string, workerId: string, idempotencyKey: string): void {
    const timestamp = now();
    const existing = this.database.prepare("SELECT * FROM p07_hardware_lease WHERE backend = 'tianyan176'")
      .get() as SqlRow | undefined;
    if (existing && optionalText(existing, "released_at") === null
      && text(existing, "campaign_id") !== campaignId) throw new Error("tianyan176 already has an active P07 lease");
    this.database.prepare(
      `INSERT INTO p07_hardware_lease(backend, campaign_id, batch_id, worker_id, idempotency_key, state, heartbeat_at, expires_at)
       VALUES ('tianyan176', ?, ?, ?, ?, 'ACTIVE', ?, ?)
       ON CONFLICT(backend) DO UPDATE SET campaign_id=excluded.campaign_id, batch_id=excluded.batch_id,
       worker_id=excluded.worker_id, idempotency_key=excluded.idempotency_key, state='ACTIVE',
       heartbeat_at=excluded.heartbeat_at, expires_at=excluded.expires_at, released_at=NULL`,
    ).run(campaignId, batchId, workerId, idempotencyKey, timestamp, new Date(Date.now() + 300_000).toISOString());
  }

  releaseHardwareLease(campaignId: string, state: string): void {
    this.database.prepare(
      `UPDATE p07_hardware_lease SET state = ?, released_at = ?, heartbeat_at = ?
       WHERE backend = 'tianyan176' AND campaign_id = ?`,
    ).run(state, now(), now(), campaignId);
  }

  registerHistory(input: {
    campaignId: string;
    entityType: "A2A" | "P2A" | "REVIEW" | "RUN" | "EVENT" | "SOURCE" | "CIRCUIT" | "ARTIFACT";
    logicalKey: string;
    sha256: string;
    relativePath: string;
    parentId?: string;
  }): string {
    const versionRow = this.database.prepare(
      `SELECT COALESCE(MAX(version), 0) AS version FROM p07_history_entities
       WHERE campaign_id = ? AND entity_type = ? AND logical_key = ?`,
    ).get(input.campaignId, input.entityType, input.logicalKey) as SqlRow;
    const existing = this.database.prepare(
      `SELECT id FROM p07_history_entities WHERE campaign_id = ? AND entity_type = ?
       AND logical_key = ? AND sha256 = ?`,
    ).get(input.campaignId, input.entityType, input.logicalKey, input.sha256) as SqlRow | undefined;
    if (existing) return text(existing, "id");
    const historyId = `p07_history_${randomUUID()}`;
    this.database.prepare(
      `INSERT INTO p07_history_entities(id, campaign_id, entity_type, logical_key, version,
       sha256, relative_path, parent_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    ).run(historyId, input.campaignId, input.entityType, input.logicalKey,
      numeric(versionRow, "version") + 1, input.sha256, input.relativePath, input.parentId ?? null, now());
    return historyId;
  }

  indexRuntimeHistory(campaignId: string): JsonObject {
    const specifications = [
      { table: "p07_runs", entityType: "RUN", key: "id" },
      { table: "p07_events", entityType: "EVENT", key: "event_id" },
      { table: "p07_sources", entityType: "SOURCE", key: "id" },
      { table: "p07_circuits", entityType: "CIRCUIT", key: "circuit_hash" },
    ] as const;
    const counts: Record<string, number> = {};
    for (const specification of specifications) {
      const runtimeRows = rows(this.database.prepare(
        `SELECT * FROM ${specification.table} WHERE campaign_id = ? ORDER BY ${specification.key}`,
      ), campaignId);
      for (const row of runtimeRows) {
        const logicalKey = text(row, specification.key);
        this.registerHistory({
          campaignId,
          entityType: specification.entityType,
          logicalKey,
          sha256: hash(row),
          relativePath: `sqlite:${specification.table}/${logicalKey}`,
        });
      }
      counts[specification.entityType] = runtimeRows.length;
    }
    return { indexed: counts };
  }

  getAuditSnapshot(campaignId: string): JsonObject {
    const detail = this.getDetail(campaignId);
    const history = rows(this.database.prepare(
      `SELECT id, entity_type, logical_key, version, sha256, relative_path, parent_id, created_at
       FROM p07_history_entities WHERE campaign_id = ? ORDER BY created_at, id`,
    ), campaignId).map((row) => ({
      historyId: text(row, "id"), entityType: text(row, "entity_type"),
      logicalKey: text(row, "logical_key"), version: numeric(row, "version"),
      sha256: text(row, "sha256"), relativePath: text(row, "relative_path"),
      parentId: optionalText(row, "parent_id"), createdAt: text(row, "created_at"),
    }));
    return {
      ...detail,
      tokenLedger: this.listLedger(campaignId, 5_000) as unknown as JsonObject[],
      events: this.listAllEvents(campaignId),
      history,
    };
  }

  recordFault(input: {
    campaignId: string;
    kind: "WORKER_CRASH" | "SQLITE_BUSY" | "PROVIDER_RATE_LIMIT" | "NETWORK_INTERRUPTION" | "ARTIFACT_WRITE_FAILURE";
    status: "PLANNED" | "INJECTED" | "RECOVERED" | "FAILED";
    evidenceArtifactSha256?: string;
    duplicateModelCalls?: number;
    duplicateHardwareSubmissions?: number;
  }): void {
    const existing = this.database.prepare(
      "SELECT id, created_at FROM p07_fault_injections WHERE campaign_id = ? AND kind = ?",
    ).get(input.campaignId, input.kind) as SqlRow | undefined;
    const timestamp = now();
    this.database.prepare(
      `INSERT INTO p07_fault_injections(id, campaign_id, kind, status, duplicate_model_calls,
       duplicate_hardware_submissions, evidence_artifact_sha256, created_at, injected_at, recovered_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT(campaign_id, kind) DO UPDATE SET status=excluded.status,
       duplicate_model_calls=excluded.duplicate_model_calls,
       duplicate_hardware_submissions=excluded.duplicate_hardware_submissions,
       evidence_artifact_sha256=COALESCE(excluded.evidence_artifact_sha256, p07_fault_injections.evidence_artifact_sha256),
       injected_at=COALESCE(p07_fault_injections.injected_at, excluded.injected_at),
       recovered_at=excluded.recovered_at`,
    ).run(existing ? text(existing, "id") : `p07_fault_${randomUUID()}`, input.campaignId, input.kind,
      input.status, input.duplicateModelCalls ?? 0, input.duplicateHardwareSubmissions ?? 0,
      input.evidenceArtifactSha256 ?? null, existing ? text(existing, "created_at") : timestamp,
      input.status === "PLANNED" ? null : timestamp, input.status === "RECOVERED" ? timestamp : null);
  }

  getDetail(campaignId: string): JsonObject {
    const campaign = this.getCampaign(campaignId);
    const runsResult = rows(this.database.prepare("SELECT * FROM p07_runs WHERE campaign_id = ? ORDER BY lane"), campaignId)
      .map((row) => ({
        runId: text(row, "id"), lane: text(row, "lane"), status: text(row, "status"),
        workerId: optionalText(row, "worker_id"),
        processId: row.process_id === null || row.process_id === undefined ? null : numeric(row, "process_id"),
        heartbeatAt: optionalText(row, "heartbeat_at"), recoveryCount: numeric(row, "recovery_count"),
        checkpoint: object(text(row, "checkpoint_json")) ?? {},
      }));
    const roles = rows(this.database.prepare("SELECT * FROM p07_roles WHERE campaign_id = ? ORDER BY role"), campaignId)
      .map((row) => ({ role: text(row, "role"), conversationId: text(row, "conversation_id"),
        promptVersion: text(row, "prompt_version"), promptHash: text(row, "prompt_hash"),
        toolNames: array(text(row, "tool_names_json")), state: text(row, "state") }));
    const sources = rows(this.database.prepare("SELECT * FROM p07_sources WHERE campaign_id = ? ORDER BY retrieved_at"), campaignId)
      .map((row) => ({ sourceId: text(row, "id"), kind: text(row, "kind"), title: text(row, "title"),
        url: text(row, "url"), domain: text(row, "domain"), version: text(row, "version"),
        commitHash: optionalText(row, "commit_hash"), license: text(row, "license"),
        maintenanceStatus: text(row, "maintenance_status"), retrievedAt: text(row, "retrieved_at") }));
    const capabilities = rows(this.database.prepare(
      "SELECT * FROM p07_capability_packages WHERE campaign_id = ? ORDER BY capability_kind, name",
    ), campaignId).map((row) => ({ capabilityId: text(row, "id"), name: text(row, "name"),
      kind: text(row, "capability_kind"), version: text(row, "version"), status: text(row, "status"),
      benchmarkArtifactSha256: optionalText(row, "benchmark_artifact_sha256"),
      knowledgeArtifactSha256: optionalText(row, "knowledge_artifact_sha256"),
      rejectionReason: optionalText(row, "rejection_reason") }));
    const batches = rows(this.database.prepare("SELECT * FROM p07_hardware_batches WHERE campaign_id = ? ORDER BY created_at"), campaignId)
      .map((row) => ({ batchId: text(row, "id"), backend: text(row, "backend"), status: text(row, "status"),
        circuitCount: numeric(row, "circuit_count"), shots: numeric(row, "shots"),
        queryIds: array(text(row, "query_ids_json")), resultArtifactSha256: optionalText(row, "result_artifact_sha256"),
        reusedFromBatchId: optionalText(row, "reuse_of_batch_id") }));
    const faults = rows(this.database.prepare("SELECT * FROM p07_fault_injections WHERE campaign_id = ? ORDER BY kind"), campaignId)
      .map((row) => ({ kind: text(row, "kind"), status: text(row, "status"),
        duplicateModelCalls: numeric(row, "duplicate_model_calls"),
        duplicateHardwareSubmissions: numeric(row, "duplicate_hardware_submissions"),
        evidenceArtifactSha256: optionalText(row, "evidence_artifact_sha256") }));
    const historyCount = numeric(this.database.prepare(
      "SELECT COUNT(*) AS count FROM p07_history_entities WHERE campaign_id = ?",
    ).get(campaignId) as SqlRow, "count");
    const ledgerCount = numeric(this.database.prepare(
      "SELECT COUNT(*) AS count FROM p07_token_ledger WHERE campaign_id = ?",
    ).get(campaignId) as SqlRow, "count");
    const history = rows(this.database.prepare(
      `SELECT id, entity_type, logical_key, version, sha256, relative_path, parent_id, created_at
       FROM p07_history_entities WHERE campaign_id = ? ORDER BY created_at DESC LIMIT 1000`,
    ), campaignId).map((row) => ({
      historyId: text(row, "id"), entityType: text(row, "entity_type"), logicalKey: text(row, "logical_key"),
      version: numeric(row, "version"), sha256: text(row, "sha256"), relativePath: text(row, "relative_path"),
      parentId: optionalText(row, "parent_id"), createdAt: text(row, "created_at"),
    }));
    const circuits = rows(this.database.prepare(
      `SELECT circuit_hash, family, depth, shots, mapping_json, circuit_ir_artifact_sha256,
       qcis_artifact_sha256, query_id, terminal_state, raw_result_artifact_sha256, created_at
       FROM p07_circuits WHERE campaign_id = ? ORDER BY created_at DESC`,
    ), campaignId).map((row) => ({
      circuitHash: text(row, "circuit_hash"), family: text(row, "family"), depth: numeric(row, "depth"),
      shots: numeric(row, "shots"), mapping: object(text(row, "mapping_json")) ?? {},
      circuitIrArtifactSha256: text(row, "circuit_ir_artifact_sha256"),
      qcisArtifactSha256: text(row, "qcis_artifact_sha256"), queryId: optionalText(row, "query_id"),
      terminalState: optionalText(row, "terminal_state"),
      rawResultArtifactSha256: optionalText(row, "raw_result_artifact_sha256"), createdAt: text(row, "created_at"),
    }));
    return {
      campaign: campaign as unknown as JsonObject,
      predecessorCampaign: campaign.predecessorCampaignId
        ? this.getCampaign(campaign.predecessorCampaignId) as unknown as JsonObject
        : null,
      runs: runsResult,
      roles,
      sources,
      capabilities,
      hardwareBatches: batches,
      faults,
      historyCount,
      history,
      circuits,
      ledgerCount,
      recentLedger: this.listLedger(campaignId, 100) as unknown as JsonObject[],
      recentEvents: this.listEvents(campaignId, Math.max(0, this.latestEventSequence(campaignId) - 300), 300),
    };
  }

  latestEventSequence(campaignId: string): number {
    const row = this.database.prepare(
      "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM p07_events WHERE campaign_id = ?",
    ).get(campaignId) as SqlRow;
    return numeric(row, "sequence");
  }
}
