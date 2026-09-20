import { randomUUID } from "node:crypto";
import type { DatabaseSync } from "node:sqlite";

import type { JsonObject } from "@q-fintelligence/contracts";

type Row = Record<string, unknown>;

function now(): string {
  return new Date().toISOString();
}

function text(row: Row, key: string): string {
  const value = row[key];
  if (typeof value !== "string") throw new Error(`expected ${key} to be text`);
  return value;
}

function optionalText(row: Row, key: string): string | null {
  const value = row[key];
  return typeof value === "string" ? value : null;
}

function number(row: Row, key: string): number {
  const value = row[key];
  if (typeof value !== "number" && typeof value !== "bigint") throw new Error(`expected ${key} to be numeric`);
  return Number(value);
}

function object(row: Row, key: string): JsonObject {
  const parsed = JSON.parse(text(row, key)) as unknown;
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error(`${key} is not a JSON object`);
  return parsed as JsonObject;
}

export type P15Status =
  | "PROBING" | "AWAITING_AUTHORIZATION" | "RUNNING" | "WAITING_HARDWARE"
  | "COMPLETED" | "BLOCKED" | "NEGATIVE_RESULT" | "FAILED";

export interface P15Campaign {
  campaignId: string;
  projectId: string;
  conversationId: string;
  taskId: string;
  provider: "deepseek/getoken";
  modelId: "deepseek-v4-pro";
  status: P15Status;
  stage: string;
  quboSha256: string | null;
  classicalResultSha256: string | null;
  bestCandidateSha256: string | null;
  targetObjective: number | null;
  bestQuantumObjective: number | null;
  confirmationBatches: number;
  newExternalCallsAllowed: boolean;
  checkpoint: JsonObject;
  error: JsonObject | null;
  startedAt: string;
  completedAt: string | null;
  heartbeatAt: string;
  createdAt: string;
  updatedAt: string;
}

export class P15Repository {
  constructor(private readonly database: DatabaseSync) {}

  createCampaign(input: {
    projectId: string;
    conversationId: string;
    taskId: string;
  }): string {
    const existing = this.database.prepare("SELECT id FROM p15_campaigns WHERE conversation_id = ?")
      .get(input.conversationId) as Row | undefined;
    if (existing) return text(existing, "id");
    const campaignId = `p15_campaign_${randomUUID()}`;
    const timestamp = now();
    this.database.prepare(
      `INSERT INTO p15_campaigns(
        id, project_id, conversation_id, task_id, provider, model_id, status, stage,
        started_at, heartbeat_at, created_at, updated_at
      ) VALUES (?, ?, ?, ?, 'deepseek/getoken', 'deepseek-v4-pro',
        'AWAITING_AUTHORIZATION', 'qubo_freeze_pending', ?, ?, ?, ?)`,
    ).run(campaignId, input.projectId, input.conversationId, input.taskId, timestamp, timestamp, timestamp, timestamp);
    return campaignId;
  }

  getCampaign(campaignId: string): P15Campaign {
    const row = this.database.prepare("SELECT * FROM p15_campaigns WHERE id = ?").get(campaignId) as Row | undefined;
    if (!row) throw new Error(`P15 campaign ${campaignId} was not found`);
    return {
      campaignId: text(row, "id"),
      projectId: text(row, "project_id"),
      conversationId: text(row, "conversation_id"),
      taskId: text(row, "task_id"),
      provider: "deepseek/getoken",
      modelId: "deepseek-v4-pro",
      status: text(row, "status") as P15Status,
      stage: text(row, "stage"),
      quboSha256: optionalText(row, "qubo_sha256"),
      classicalResultSha256: optionalText(row, "classical_result_sha256"),
      bestCandidateSha256: optionalText(row, "best_candidate_sha256"),
      targetObjective: row.target_objective === null ? null : number(row, "target_objective"),
      bestQuantumObjective: row.best_quantum_objective === null ? null : number(row, "best_quantum_objective"),
      confirmationBatches: number(row, "confirmation_batches"),
      newExternalCallsAllowed: number(row, "new_external_calls_allowed") === 1,
      startedAt: text(row, "started_at"),
      completedAt: optionalText(row, "completed_at"),
      heartbeatAt: text(row, "heartbeat_at"),
      checkpoint: object(row, "checkpoint_json"),
      error: row.error_json === null ? null : object(row, "error_json"),
      createdAt: text(row, "created_at"),
      updatedAt: text(row, "updated_at"),
    };
  }

  listCampaigns(): P15Campaign[] {
    return (this.database.prepare("SELECT id FROM p15_campaigns ORDER BY created_at DESC").all() as Row[])
      .map((row) => this.getCampaign(text(row, "id")));
  }

  updateCampaign(campaignId: string, input: {
    status?: P15Status;
    stage?: string;
    quboSha256?: string | null;
    classicalResultSha256?: string | null;
    bestCandidateSha256?: string | null;
    targetObjective?: number | null;
    bestQuantumObjective?: number | null;
    confirmationBatches?: number;
    newExternalCallsAllowed?: boolean;
    checkpoint?: JsonObject;
    error?: JsonObject | null;
    completed?: boolean;
    reopened?: boolean;
  }): void {
    const current = this.getCampaign(campaignId);
    const timestamp = now();
    this.database.prepare(
      `UPDATE p15_campaigns SET status = ?, stage = ?, qubo_sha256 = ?,
       classical_result_sha256 = ?, best_candidate_sha256 = ?, target_objective = ?,
       best_quantum_objective = ?, confirmation_batches = ?, new_external_calls_allowed = ?,
       heartbeat_at = ?, checkpoint_json = ?, error_json = ?, completed_at = ?, updated_at = ?
       WHERE id = ?`,
    ).run(
      input.status ?? current.status,
      input.stage ?? current.stage,
      input.quboSha256 === undefined ? current.quboSha256 : input.quboSha256,
      input.classicalResultSha256 === undefined ? current.classicalResultSha256 : input.classicalResultSha256,
      input.bestCandidateSha256 === undefined ? current.bestCandidateSha256 : input.bestCandidateSha256,
      input.targetObjective === undefined ? current.targetObjective : input.targetObjective,
      input.bestQuantumObjective === undefined ? current.bestQuantumObjective : input.bestQuantumObjective,
      input.confirmationBatches ?? current.confirmationBatches,
      (input.newExternalCallsAllowed ?? current.newExternalCallsAllowed) ? 1 : 0,
      timestamp,
      JSON.stringify(input.checkpoint ?? current.checkpoint),
      input.error === undefined ? (current.error ? JSON.stringify(current.error) : null) : (input.error ? JSON.stringify(input.error) : null),
      input.reopened ? null : input.completed ? timestamp : current.completedAt,
      timestamp,
      campaignId,
    );
  }

  createGeneration(input: {
    campaignId: string;
    generationIndex: number;
    strategy: string;
    calibrationSnapshotId: string;
    candidateCount: number;
    approvalId: string;
  }): string {
    const existing = this.database.prepare(
      "SELECT id FROM p15_generations WHERE campaign_id = ? AND generation_index = ?",
    ).get(input.campaignId, input.generationIndex) as Row | undefined;
    if (existing) return text(existing, "id");
    const generationId = `p15_generation_${randomUUID()}`;
    this.database.prepare(
      `INSERT INTO p15_generations(
        id, campaign_id, generation_index, strategy, calibration_snapshot_id,
        status, candidate_count, approval_id, created_at
      ) VALUES (?, ?, ?, ?, ?, 'GENERATING', ?, ?, ?)`,
    ).run(generationId, input.campaignId, input.generationIndex, input.strategy,
      input.calibrationSnapshotId, input.candidateCount, input.approvalId, now());
    return generationId;
  }

  getGeneration(campaignId: string, generationIndex: number): Row | null {
    return (this.database.prepare(
      "SELECT * FROM p15_generations WHERE campaign_id = ? AND generation_index = ?",
    ).get(campaignId, generationIndex) as Row | undefined) ?? null;
  }

  updateGeneration(generationId: string, input: {
    status: string;
    top5?: JsonObject[];
    batchId?: string;
    metrics?: JsonObject;
    completed?: boolean;
  }): void {
    const row = this.database.prepare("SELECT * FROM p15_generations WHERE id = ?").get(generationId) as Row;
    this.database.prepare(
      `UPDATE p15_generations SET status = ?, top5_json = ?, batch_id = ?,
       metrics_json = ?, completed_at = ? WHERE id = ?`,
    ).run(
      input.status,
      JSON.stringify(input.top5 ?? JSON.parse(text(row, "top5_json"))),
      input.batchId ?? optionalText(row, "batch_id"),
      JSON.stringify(input.metrics ?? object(row, "metrics_json")),
      input.completed ? now() : optionalText(row, "completed_at"),
      generationId,
    );
  }

  registerCandidate(input: {
    generationId: string;
    candidateIndex: number;
    family: string;
    parameters: JsonObject;
    qcis: string;
    qcisSha256: string;
    circuitArtifactSha256: string;
    mappedQubits: string[];
    localMetrics: JsonObject;
  }): void {
    const timestamp = now();
    this.database.prepare(
      `INSERT OR IGNORE INTO p15_candidates(
        id, generation_id, candidate_index, family, parameters_json, qcis, qcis_sha256,
        circuit_artifact_sha256, mapped_qubits_json, local_metrics_json, created_at, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    ).run(`p15_candidate_${randomUUID()}`, input.generationId, input.candidateIndex, input.family,
      JSON.stringify(input.parameters), input.qcis, input.qcisSha256, input.circuitArtifactSha256,
      JSON.stringify(input.mappedQubits), JSON.stringify(input.localMetrics), timestamp, timestamp);
  }

  createBatch(input: {
    campaignId: string;
    generationId: string;
    calibrationSnapshotId: string;
    approvalId: string;
    leaseKey: string;
    requestSha256: string;
  }): string {
    const existing = this.database.prepare("SELECT id FROM p15_hardware_batches WHERE request_sha256 = ?")
      .get(input.requestSha256) as Row | undefined;
    if (existing) return text(existing, "id");
    const batchId = `p15_batch_${randomUUID()}`;
    const timestamp = now();
    this.database.prepare(
      `INSERT INTO p15_hardware_batches(
        id, campaign_id, generation_id, calibration_snapshot_id, approval_id, lease_key,
        request_sha256, status, heartbeat_at, created_at, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, 'PREPARED', ?, ?, ?)`,
    ).run(batchId, input.campaignId, input.generationId, input.calibrationSnapshotId,
      input.approvalId, input.leaseKey, input.requestSha256, timestamp, timestamp, timestamp);
    return batchId;
  }

  getBatch(batchId: string): Row {
    const row = this.database.prepare("SELECT * FROM p15_hardware_batches WHERE id = ?").get(batchId) as Row | undefined;
    if (!row) throw new Error(`P15 batch ${batchId} was not found`);
    return row;
  }

  updateBatch(batchId: string, status: string, queryIds?: string[], completed = false): void {
    const current = this.getBatch(batchId);
    const timestamp = now();
    this.database.prepare(
      `UPDATE p15_hardware_batches SET status = ?, query_ids_json = ?, submitted_at = ?,
       completed_at = ?, heartbeat_at = ?, updated_at = ? WHERE id = ?`,
    ).run(
      status,
      JSON.stringify(queryIds ?? JSON.parse(text(current, "query_ids_json"))),
      status === "SUBMITTED" ? timestamp : optionalText(current, "submitted_at"),
      completed ? timestamp : optionalText(current, "completed_at"),
      timestamp,
      timestamp,
      batchId,
    );
  }

  updateCandidateResult(input: {
    generationId: string;
    candidateIndex: number;
    queryId: string;
    rawSha256: string;
    correctedSha256: string;
    metrics: JsonObject;
  }): void {
    this.database.prepare(
      `UPDATE p15_candidates SET query_id = ?, terminal_status = 'COMPLETED',
       raw_result_sha256 = ?, corrected_result_sha256 = ?, hardware_metrics_json = ?, updated_at = ?
       WHERE generation_id = ? AND candidate_index = ?`,
    ).run(input.queryId, input.rawSha256, input.correctedSha256, JSON.stringify(input.metrics),
      now(), input.generationId, input.candidateIndex);
  }

  getDetail(campaignId: string): JsonObject {
    const campaign = this.getCampaign(campaignId);
    const generations = this.database.prepare(
      "SELECT * FROM p15_generations WHERE campaign_id = ? ORDER BY generation_index",
    ).all(campaignId) as Row[];
    const batches = this.database.prepare(
      "SELECT * FROM p15_hardware_batches WHERE campaign_id = ? ORDER BY created_at",
    ).all(campaignId) as Row[];
    return {
      campaign: campaign as unknown as JsonObject,
      generations: generations.map((row) => ({
        generationId: text(row, "id"),
        generationIndex: number(row, "generation_index"),
        strategy: text(row, "strategy"),
        status: text(row, "status"),
        candidateCount: number(row, "candidate_count"),
        top5: JSON.parse(text(row, "top5_json")),
        batchId: optionalText(row, "batch_id"),
        metrics: JSON.parse(text(row, "metrics_json")),
        createdAt: text(row, "created_at"),
        completedAt: optionalText(row, "completed_at"),
      })),
      hardwareBatches: batches.map((row) => ({
        batchId: text(row, "id"),
        generationId: text(row, "generation_id"),
        status: text(row, "status"),
        queryIds: JSON.parse(text(row, "query_ids_json")),
        requestSha256: text(row, "request_sha256"),
        submittedAt: optionalText(row, "submitted_at"),
        completedAt: optionalText(row, "completed_at"),
        heartbeatAt: text(row, "heartbeat_at"),
      })),
      formalTestSealed: true,
    };
  }
}
