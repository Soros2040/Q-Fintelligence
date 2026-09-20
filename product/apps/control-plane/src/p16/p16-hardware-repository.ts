// Authorship category: supervisor_infrastructure
// Persists P16 approvals, idempotency, Query IDs, terminal artifacts, and query-only recovery.

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

function integer(row: Row, key: string): number {
  const value = row[key];
  if (typeof value !== "number" && typeof value !== "bigint") {
    throw new Error(`expected ${key} to be numeric`);
  }
  return Number(value);
}

function stringArray(row: Row, key: string): string[] {
  const parsed = JSON.parse(text(row, key)) as unknown;
  if (!Array.isArray(parsed) || parsed.some((value) => typeof value !== "string")) {
    throw new Error(`${key} is not a string array`);
  }
  return parsed;
}

function optionalObject(row: Row, key: string): JsonObject | null {
  const value = optionalText(row, key);
  if (value === null) return null;
  const parsed = JSON.parse(value) as unknown;
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error(`${key} is not a JSON object`);
  }
  return parsed as JsonObject;
}

export type P16HardwareBatchStatus =
  | "PREPARED" | "COMMITTING" | "SUBMITTED" | "QUERYING"
  | "COMPLETED" | "UNKNOWN" | "BLOCKED" | "FAILED";

export interface P16HardwareBatch {
  batchId: string;
  campaignId: string;
  generationId: string;
  batchIndex: number;
  status: P16HardwareBatchStatus;
  requestSha256: string;
  requestArtifactSha256: string;
  circuitSha256s: string[];
  mappingReportSha256: string;
  validationReportSha256: string;
  approvalId: string;
  queryIds: string[];
  submissionArtifactSha256: string | null;
  rawResultSha256: string | null;
  correctedResultSha256: string | null;
  error: JsonObject | null;
  submitAttempts: number;
  calibrationSnapshotId: string;
  submittedAt: string | null;
  completedAt: string | null;
  heartbeatAt: string;
}

export function p16BatchWithoutQueryIdsMustRemainQueryOnly(status: string): boolean {
  return status === "COMMITTING" || status === "UNKNOWN";
}

export class P16HardwareRepository {
  constructor(private readonly database: DatabaseSync) {}

  getBatch(batchId: string): P16HardwareBatch {
    const row = this.database.prepare("SELECT * FROM p16_hardware_batches WHERE id = ?")
      .get(batchId) as Row | undefined;
    if (!row) throw new Error(`P16 hardware batch ${batchId} was not found`);
    return {
      batchId: text(row, "id"),
      campaignId: text(row, "campaign_id"),
      generationId: text(row, "generation_id"),
      batchIndex: integer(row, "batch_index"),
      status: text(row, "status") as P16HardwareBatchStatus,
      requestSha256: text(row, "request_sha256"),
      requestArtifactSha256: text(row, "request_artifact_sha256"),
      circuitSha256s: stringArray(row, "circuit_sha256s_json"),
      mappingReportSha256: text(row, "mapping_report_sha256"),
      validationReportSha256: text(row, "validation_report_sha256"),
      approvalId: text(row, "approval_id"),
      queryIds: stringArray(row, "query_ids_json"),
      submissionArtifactSha256: optionalText(row, "submission_artifact_sha256"),
      rawResultSha256: optionalText(row, "raw_result_sha256"),
      correctedResultSha256: optionalText(row, "corrected_result_sha256"),
      error: optionalObject(row, "error_json"),
      submitAttempts: integer(row, "submit_attempts"),
      calibrationSnapshotId: text(row, "calibration_snapshot_id"),
      submittedAt: optionalText(row, "submitted_at"),
      completedAt: optionalText(row, "completed_at"),
      heartbeatAt: text(row, "heartbeat_at"),
    };
  }

  getBatchForConversation(batchId: string, conversationId: string): P16HardwareBatch {
    const row = this.database.prepare(
      `SELECT batch.id FROM p16_hardware_batches batch
       JOIN p16_hardware_campaigns campaign ON campaign.id = batch.campaign_id
       WHERE batch.id = ? AND campaign.conversation_id = ?`,
    ).get(batchId, conversationId) as Row | undefined;
    if (!row) throw new Error(`P16 hardware batch ${batchId} does not belong to this conversation`);
    return this.getBatch(text(row, "id"));
  }

  listBatches(conversationId: string): P16HardwareBatch[] {
    const rows = this.database.prepare(
      `SELECT batch.id FROM p16_hardware_batches batch
       JOIN p16_hardware_campaigns campaign ON campaign.id = batch.campaign_id
       WHERE campaign.conversation_id = ? ORDER BY batch.batch_index, batch.id`,
    ).all(conversationId) as Row[];
    return rows.map((row) => this.getBatch(text(row, "id")));
  }

  prepareBatch(input: {
    projectId: string;
    conversationId: string;
    taskId: string;
    protocolSha256: string;
    searchSpaceSha256: string;
    quboSha256: string;
    calibrationSnapshotId: string;
    generationIndex: number;
    batchIndex: number;
    strategy: string;
    circuitSha256s: string[];
    mappingReportSha256: string;
    validationReportSha256: string;
    approvalId: string;
    requestSha256: string;
    requestArtifactSha256: string;
  }): P16HardwareBatch {
    const existingRequest = this.database.prepare(
      "SELECT id FROM p16_hardware_batches WHERE request_sha256 = ?",
    ).get(input.requestSha256) as Row | undefined;
    if (existingRequest) return this.getBatch(text(existingRequest, "id"));

    const timestamp = now();
    this.database.exec("BEGIN IMMEDIATE");
    try {
      const campaignRow = this.database.prepare(
        "SELECT * FROM p16_hardware_campaigns WHERE conversation_id = ?",
      ).get(input.conversationId) as Row | undefined;
      let campaignId: string;
      if (campaignRow) {
        campaignId = text(campaignRow, "id");
        const immutable = [
          ["protocol_sha256", input.protocolSha256],
          ["search_space_sha256", input.searchSpaceSha256],
          ["qubo_sha256", input.quboSha256],
        ] as const;
        for (const [column, expected] of immutable) {
          if (text(campaignRow, column) !== expected) {
            throw new Error(`P16 Campaign immutable ${column} does not match the frozen request`);
          }
        }
        this.database.prepare(
          "UPDATE p16_hardware_campaigns SET calibration_snapshot_id = ?, heartbeat_at = ?, updated_at = ? WHERE id = ?",
        ).run(input.calibrationSnapshotId, timestamp, timestamp, campaignId);
      } else {
        campaignId = `p16_campaign_${randomUUID()}`;
        this.database.prepare(
          `INSERT INTO p16_hardware_campaigns(
            id, project_id, conversation_id, task_id, provider, model_id, status, stage,
            authorization_basis, protocol_sha256, search_space_sha256, qubo_sha256,
            calibration_snapshot_id, started_at, heartbeat_at, created_at, updated_at
          ) VALUES (?, ?, ?, ?, 'deepseek/getoken', 'deepseek-v4-pro', 'READY',
            'hardware_batch_prepared', 'P16_USER_REQUEST_20260725', ?, ?, ?, ?, ?, ?, ?, ?)`,
        ).run(
          campaignId,
          input.projectId,
          input.conversationId,
          input.taskId,
          input.protocolSha256,
          input.searchSpaceSha256,
          input.quboSha256,
          input.calibrationSnapshotId,
          timestamp,
          timestamp,
          timestamp,
          timestamp,
        );
      }

      const existingGeneration = this.database.prepare(
        "SELECT * FROM p16_hardware_generations WHERE campaign_id = ? AND generation_index = ?",
      ).get(campaignId, input.generationIndex) as Row | undefined;
      let generationId: string;
      if (existingGeneration) {
        generationId = text(existingGeneration, "id");
        if (integer(existingGeneration, "candidate_count") !== input.circuitSha256s.length) {
          throw new Error("P16 generation candidate count conflicts with its frozen batch");
        }
      } else {
        generationId = `p16_generation_${randomUUID()}`;
        this.database.prepare(
          `INSERT INTO p16_hardware_generations(
            id, campaign_id, generation_index, strategy, status, candidate_count, created_at
          ) VALUES (?, ?, ?, ?, 'PREPARED', ?, ?)`,
        ).run(
          generationId,
          campaignId,
          input.generationIndex,
          input.strategy,
          input.circuitSha256s.length,
          timestamp,
        );
      }

      const conflictingBatch = this.database.prepare(
        "SELECT id, request_sha256 FROM p16_hardware_batches WHERE campaign_id = ? AND batch_index = ?",
      ).get(campaignId, input.batchIndex) as Row | undefined;
      if (conflictingBatch) {
        if (text(conflictingBatch, "request_sha256") !== input.requestSha256) {
          throw new Error("P16 batch index is already frozen to another immutable request");
        }
        this.database.exec("COMMIT");
        return this.getBatch(text(conflictingBatch, "id"));
      }

      const batchId = `p16_batch_${randomUUID()}`;
      const idempotencyKey = `P16:${campaignId}:G${input.generationIndex}:B${input.batchIndex}:${input.requestSha256}`;
      this.database.prepare(
        `INSERT INTO p16_hardware_batches(
          id, campaign_id, generation_id, batch_index, purpose, machine_name, shots,
          calibration_snapshot_id, approval_id, authorization_basis, idempotency_key,
          request_sha256, request_artifact_sha256, circuit_sha256s_json,
          mapping_report_sha256, validation_report_sha256, status,
          heartbeat_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'p16_quantum_advantage_validation', 'tianyan176', 100,
          ?, ?, 'P16_USER_REQUEST_20260725', ?, ?, ?, ?, ?, ?, 'PREPARED', ?, ?, ?)`,
      ).run(
        batchId,
        campaignId,
        generationId,
        input.batchIndex,
        input.calibrationSnapshotId,
        input.approvalId,
        idempotencyKey,
        input.requestSha256,
        input.requestArtifactSha256,
        JSON.stringify(input.circuitSha256s),
        input.mappingReportSha256,
        input.validationReportSha256,
        timestamp,
        timestamp,
        timestamp,
      );
      this.database.prepare(
        "UPDATE p16_hardware_generations SET batch_id = ? WHERE id = ?",
      ).run(batchId, generationId);
      this.database.exec("COMMIT");
      return this.getBatch(batchId);
    } catch (error) {
      try { this.database.exec("ROLLBACK"); } catch { /* transaction already closed */ }
      throw error;
    }
  }

  markCommitting(batchId: string): P16HardwareBatch {
    const batch = this.getBatch(batchId);
    if (batch.queryIds.length > 0 || p16BatchWithoutQueryIdsMustRemainQueryOnly(batch.status)) {
      throw new Error("P16 batch is query-only; automatic resubmission is forbidden");
    }
    if (batch.status !== "PREPARED" || batch.submitAttempts !== 0) {
      throw new Error(`P16 batch cannot submit from ${batch.status}`);
    }
    const timestamp = now();
    this.database.prepare(
      `UPDATE p16_hardware_batches SET status = 'COMMITTING', submit_attempts = 1,
       heartbeat_at = ?, updated_at = ? WHERE id = ?`,
    ).run(timestamp, timestamp, batchId);
    this.database.prepare(
      `UPDATE p16_hardware_campaigns SET status = 'WAITING_HARDWARE', stage = 'hardware_committing',
       heartbeat_at = ?, updated_at = ? WHERE id = ?`,
    ).run(timestamp, timestamp, batch.campaignId);
    return this.getBatch(batchId);
  }

  markUnknown(batchId: string, error: JsonObject): P16HardwareBatch {
    const batch = this.getBatch(batchId);
    if (batch.queryIds.length > 0) throw new Error("P16 submitted batch cannot lose persisted Query IDs");
    const timestamp = now();
    this.database.prepare(
      `UPDATE p16_hardware_batches SET status = 'UNKNOWN', error_json = ?,
       heartbeat_at = ?, updated_at = ? WHERE id = ?`,
    ).run(JSON.stringify(error), timestamp, timestamp, batchId);
    this.database.prepare(
      `UPDATE p16_hardware_generations SET status = 'UNKNOWN' WHERE id = ?`,
    ).run(batch.generationId);
    this.database.prepare(
      `UPDATE p16_hardware_campaigns SET status = 'BLOCKED', stage = 'unknown_submission_query_only',
       new_external_calls_allowed = 0, error_json = ?, heartbeat_at = ?, updated_at = ? WHERE id = ?`,
    ).run(JSON.stringify(error), timestamp, timestamp, batch.campaignId);
    return this.getBatch(batchId);
  }

  markSubmitted(batchId: string, queryIds: string[]): P16HardwareBatch {
    const batch = this.getBatch(batchId);
    if (batch.status !== "COMMITTING" || batch.submitAttempts !== 1) {
      throw new Error("P16 Query IDs may only be attached to the single COMMITTING attempt");
    }
    if (queryIds.length !== batch.circuitSha256s.length
      || queryIds.some((value) => !value)
      || new Set(queryIds).size !== queryIds.length) {
      throw new Error("P16 Query IDs must be non-empty, distinct, and match the frozen circuit count");
    }
    const timestamp = now();
    this.database.prepare(
      `UPDATE p16_hardware_batches SET status = 'SUBMITTED', query_ids_json = ?,
       submitted_at = ?, error_json = NULL,
       heartbeat_at = ?, updated_at = ? WHERE id = ?`,
    ).run(JSON.stringify(queryIds), timestamp, timestamp, timestamp, batchId);
    this.database.prepare(
      "UPDATE p16_hardware_generations SET status = 'SUBMITTED' WHERE id = ?",
    ).run(batch.generationId);
    return this.getBatch(batchId);
  }

  attachSubmissionArtifact(batchId: string, submissionArtifactSha256: string): P16HardwareBatch {
    const batch = this.getBatch(batchId);
    if (batch.queryIds.length === 0 || !["SUBMITTED", "QUERYING"].includes(batch.status)) {
      throw new Error("P16 submission artifact requires already-persisted Query IDs");
    }
    const timestamp = now();
    this.database.prepare(
      "UPDATE p16_hardware_batches SET submission_artifact_sha256 = ?, heartbeat_at = ?, updated_at = ? WHERE id = ?",
    ).run(submissionArtifactSha256, timestamp, timestamp, batchId);
    return this.getBatch(batchId);
  }

  markQuerying(batchId: string): P16HardwareBatch {
    const batch = this.getBatch(batchId);
    if (batch.queryIds.length === 0) {
      throw new Error("P16 query recovery requires the original persisted Query IDs");
    }
    if (!["SUBMITTED", "QUERYING"].includes(batch.status)) {
      throw new Error(`P16 batch cannot be queried from ${batch.status}`);
    }
    const timestamp = now();
    this.database.prepare(
      "UPDATE p16_hardware_batches SET status = 'QUERYING', heartbeat_at = ?, updated_at = ? WHERE id = ?",
    ).run(timestamp, timestamp, batchId);
    this.database.prepare(
      "UPDATE p16_hardware_generations SET status = 'QUERYING' WHERE id = ?",
    ).run(batch.generationId);
    return this.getBatch(batchId);
  }

  markQueryPending(batchId: string, error: JsonObject | null = null): P16HardwareBatch {
    const batch = this.getBatch(batchId);
    if (batch.queryIds.length === 0) throw new Error("P16 pending query has no recovery handle");
    const timestamp = now();
    this.database.prepare(
      `UPDATE p16_hardware_batches SET status = 'SUBMITTED', error_json = ?,
       heartbeat_at = ?, updated_at = ? WHERE id = ?`,
    ).run(error ? JSON.stringify(error) : null, timestamp, timestamp, batchId);
    return this.getBatch(batchId);
  }

  markCompleted(input: {
    batchId: string;
    rawResultSha256: string;
    correctedResultSha256: string;
  }): P16HardwareBatch {
    const batch = this.getBatch(input.batchId);
    if (batch.queryIds.length !== batch.circuitSha256s.length) {
      throw new Error("P16 terminal result count has no complete Query ID recovery handle");
    }
    const timestamp = now();
    this.database.prepare(
      `UPDATE p16_hardware_batches SET status = 'COMPLETED', raw_result_sha256 = ?,
       corrected_result_sha256 = ?, completed_at = ?, error_json = NULL,
       heartbeat_at = ?, updated_at = ? WHERE id = ?`,
    ).run(
      input.rawResultSha256,
      input.correctedResultSha256,
      timestamp,
      timestamp,
      timestamp,
      input.batchId,
    );
    this.database.prepare(
      "UPDATE p16_hardware_generations SET status = 'COMPLETED', completed_at = ? WHERE id = ?",
    ).run(timestamp, batch.generationId);
    this.database.prepare(
      `UPDATE p16_hardware_campaigns SET status = 'RUNNING', stage = 'hardware_batch_completed',
       heartbeat_at = ?, updated_at = ? WHERE id = ?`,
    ).run(timestamp, timestamp, batch.campaignId);
    return this.getBatch(input.batchId);
  }
}
