import { randomUUID } from "node:crypto";
import type { DatabaseSync, StatementSync } from "node:sqlite";

import {
  CONTRACT_SCHEMA_VERSION,
  type JsonObject,
  type P05BatchSummary,
  type P05CircuitSummary,
  type P05EventSummary,
  type P05ModelCallSummary,
  type P05TaskDetail,
  type P05TaskState,
  type P05ToolSummary,
  type P05UploadSummary,
  type P05VendorCandidateSummary,
  type ProjectSourceSummary,
} from "@q-fintelligence/contracts";

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

function json(value: string | null): JsonObject | null {
  if (value === null) return null;
  const parsed = JSON.parse(value) as unknown;
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error("persisted P05 JSON must be an object");
  }
  return parsed as JsonObject;
}

export class P05Repository {
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

  createTask(input: {
    conversationId: string;
    taskId: string;
    objective: string;
    taskStartedAt: string;
    hardDeadlineAt: string;
  }): string {
    const existing = this.database.prepare(
      "SELECT id FROM p05_tasks WHERE conversation_id = ?",
    ).get(input.conversationId) as SqlRow | undefined;
    if (existing) return text(existing, "id");
    const p05TaskId = `p05_task_${randomUUID()}`;
    const timestamp = now();
    this.transaction(() => {
      this.database.prepare(
        `INSERT INTO p05_tasks(
          id, conversation_id, task_id, provider, model_id, status, stage, objective,
          task_started_at, hard_deadline_at, created_at, updated_at
        ) VALUES (?, ?, ?, 'openai/getoken', 'gpt-5.6-sol', 'CREATED', 'frontend_intake', ?, ?, ?, ?, ?)`,
      ).run(
        p05TaskId,
        input.conversationId,
        input.taskId,
        input.objective,
        input.taskStartedAt,
        input.hardDeadlineAt,
        timestamp,
        timestamp,
      );
      this.database.prepare(
        "UPDATE p05_uploads SET p05_task_id = ? WHERE conversation_id = ?",
      ).run(p05TaskId, input.conversationId);
    });
    return p05TaskId;
  }

  getTaskIdForConversation(conversationId: string): string | null {
    const row = this.database.prepare(
      "SELECT id FROM p05_tasks WHERE conversation_id = ?",
    ).get(conversationId) as SqlRow | undefined;
    return row ? text(row, "id") : null;
  }

  getTokenCampaignRecoveryState(p05TaskId: string): {
    attemptedConcurrency: number[];
    nextWave: number;
  } {
    const attemptedConcurrency = rows(
      this.database.prepare(
        `SELECT DISTINCT CAST(json_extract(detail_json, '$.concurrency') AS INTEGER) AS concurrency
         FROM p05_events
         WHERE p05_task_id = ?
           AND summary LIKE '%探测完成%'
           AND json_extract(detail_json, '$.concurrency') IS NOT NULL
         ORDER BY concurrency`,
      ),
      p05TaskId,
    ).map((row) => numeric(row, "concurrency"));
    const waveRow = this.database.prepare(
      `SELECT MAX(CAST(json_extract(detail_json, '$.wave') AS INTEGER)) AS wave
       FROM p05_events
       WHERE p05_task_id = ?
         AND summary LIKE 'Token wave %'`,
    ).get(p05TaskId) as SqlRow;
    const lastWave = waveRow.wave;
    return {
      attemptedConcurrency,
      nextWave: typeof lastWave === "number" ? lastWave + 1 : 0,
    };
  }

  addUpload(input: {
    conversationId: string;
    fileName: string;
    extension: string;
    declaredMediaType: string;
    detectedMediaType: string;
    byteSize: number;
    sha256: string;
    relativePath: string;
    parser: string;
    riskLevel: "LOW" | "MEDIUM" | "HIGH";
    quarantined: boolean;
    parseResult: JsonObject;
  }): P05UploadSummary {
    const uploadId = `p05_upload_${randomUUID()}`;
    const timestamp = now();
    const taskId = this.getTaskIdForConversation(input.conversationId);
    this.database.prepare(
      `INSERT OR IGNORE INTO p05_uploads(
        id, conversation_id, p05_task_id, file_name, extension, declared_media_type,
        detected_media_type, byte_size, sha256, relative_path, parser, risk_level,
        quarantined, parse_result_json, created_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    ).run(
      uploadId,
      input.conversationId,
      taskId,
      input.fileName,
      input.extension,
      input.declaredMediaType,
      input.detectedMediaType,
      input.byteSize,
      input.sha256,
      input.relativePath,
      input.parser,
      input.riskLevel,
      input.quarantined ? 1 : 0,
      JSON.stringify(input.parseResult),
      timestamp,
    );
    const row = this.database.prepare(
      `SELECT upload.*, conversation.project_id
       FROM p05_uploads upload
       JOIN conversations conversation ON conversation.id = upload.conversation_id
       WHERE upload.conversation_id = ? AND upload.sha256 = ? AND upload.file_name = ?`,
    ).get(input.conversationId, input.sha256, input.fileName) as SqlRow;
    return this.uploadFromRow(row);
  }

  private uploadFromRow(row: SqlRow): P05UploadSummary {
    return {
      uploadId: text(row, "id"),
      conversationId: text(row, "conversation_id"),
      projectId: text(row, "project_id"),
      sourceScope: "CONVERSATION",
      taskId: optionalText(row, "p05_task_id"),
      fileName: text(row, "file_name"),
      extension: text(row, "extension"),
      declaredMediaType: text(row, "declared_media_type"),
      detectedMediaType: text(row, "detected_media_type"),
      byteSize: numeric(row, "byte_size"),
      sha256: text(row, "sha256"),
      parser: text(row, "parser"),
      riskLevel: text(row, "risk_level") as P05UploadSummary["riskLevel"],
      quarantined: numeric(row, "quarantined") === 1,
      parseResult: json(text(row, "parse_result_json"))!,
      createdAt: text(row, "created_at"),
    };
  }

  listUploads(conversationId: string): P05UploadSummary[] {
    const conversation = this.database.prepare(
      "SELECT project_id FROM conversations WHERE id = ?",
    ).get(conversationId) as SqlRow | undefined;
    if (!conversation) throw new Error(`conversation ${conversationId} was not found`);
    const projectId = text(conversation, "project_id");
    const uploads = rows(
      this.database.prepare(
        `SELECT upload.*, conversation.project_id
         FROM p05_uploads upload
         JOIN conversations conversation ON conversation.id = upload.conversation_id
         WHERE upload.conversation_id = ?
         ORDER BY upload.created_at`,
      ),
      conversationId,
    ).map((row) => this.uploadFromRow(row));
    const projectSources = this.listProjectSources(projectId).map<P05UploadSummary>((source) => ({
      uploadId: source.sourceId,
      conversationId,
      projectId,
      sourceScope: "PROJECT",
      taskId: null,
      fileName: source.fileName,
      extension: source.extension,
      declaredMediaType: source.declaredMediaType,
      detectedMediaType: source.detectedMediaType,
      byteSize: source.byteSize,
      sha256: source.sha256,
      parser: source.parser,
      riskLevel: source.riskLevel,
      quarantined: source.quarantined,
      parseResult: this.getProjectSourceParseResult(projectId, source.sourceId),
      createdAt: source.createdAt,
    }));
    return [...new Map([...uploads, ...projectSources].map((upload) => [
      `${upload.sha256}:${upload.fileName}`,
      upload,
    ])).values()];
  }

  addProjectSource(input: {
    projectId: string;
    conversationId?: string;
    fileName: string;
    extension: string;
    declaredMediaType: string;
    detectedMediaType: string;
    byteSize: number;
    sha256: string;
    relativePath: string;
    parser: string;
    riskLevel: "LOW" | "MEDIUM" | "HIGH";
    quarantined: boolean;
    parseResult: JsonObject;
  }): ProjectSourceSummary {
    const sourceId = `project_source_${randomUUID()}`;
    const timestamp = now();
    this.database.prepare(
      `INSERT INTO project_sources(
        id, project_id, added_from_conversation_id, file_name, extension,
        declared_media_type, detected_media_type, byte_size, sha256, relative_path,
        parser, risk_level, quarantined, parse_result_json, archived_at, created_at, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
      ON CONFLICT(project_id, sha256, file_name) DO UPDATE SET
        added_from_conversation_id = COALESCE(excluded.added_from_conversation_id, project_sources.added_from_conversation_id),
        archived_at = NULL,
        updated_at = excluded.updated_at`,
    ).run(
      sourceId,
      input.projectId,
      input.conversationId ?? null,
      input.fileName,
      input.extension,
      input.declaredMediaType,
      input.detectedMediaType,
      input.byteSize,
      input.sha256,
      input.relativePath,
      input.parser,
      input.riskLevel,
      input.quarantined ? 1 : 0,
      JSON.stringify(input.parseResult),
      timestamp,
      timestamp,
    );
    const row = this.database.prepare(
      "SELECT * FROM project_sources WHERE project_id = ? AND sha256 = ? AND file_name = ?",
    ).get(input.projectId, input.sha256, input.fileName) as SqlRow;
    return this.projectSourceFromRow(row);
  }

  getProjectSource(projectId: string, sourceId: string): ProjectSourceSummary {
    const row = this.database.prepare(
      "SELECT * FROM project_sources WHERE project_id = ? AND id = ? AND archived_at IS NULL",
    ).get(projectId, sourceId) as SqlRow | undefined;
    if (!row) throw new Error(`project source ${sourceId} was not found`);
    return this.projectSourceFromRow(row);
  }

  listProjectSources(projectId: string): ProjectSourceSummary[] {
    return rows(
      this.database.prepare(
        "SELECT * FROM project_sources WHERE project_id = ? AND archived_at IS NULL ORDER BY created_at DESC, id",
      ),
      projectId,
    ).map((row) => this.projectSourceFromRow(row));
  }

  archiveProjectSource(projectId: string, sourceId: string): void {
    const timestamp = now();
    const result = this.database.prepare(
      `UPDATE project_sources
       SET archived_at = ?, updated_at = ?
       WHERE project_id = ? AND id = ? AND archived_at IS NULL`,
    ).run(timestamp, timestamp, projectId, sourceId);
    if (result.changes !== 1) throw new Error(`project source ${sourceId} was not found`);
  }

  private getProjectSourceParseResult(projectId: string, sourceId: string): JsonObject {
    const row = this.database.prepare(
      "SELECT parse_result_json FROM project_sources WHERE project_id = ? AND id = ?",
    ).get(projectId, sourceId) as SqlRow | undefined;
    if (!row) throw new Error(`project source ${sourceId} was not found`);
    return json(text(row, "parse_result_json"))!;
  }

  private projectSourceFromRow(row: SqlRow): ProjectSourceSummary {
    const parseResult = json(text(row, "parse_result_json"));
    const artifactSha256 = typeof parseResult?.artifactSha256 === "string"
      && /^[a-f0-9]{64}$/u.test(parseResult.artifactSha256)
      ? parseResult.artifactSha256
      : null;
    return {
      sourceId: text(row, "id"),
      projectId: text(row, "project_id"),
      addedFromConversationId: optionalText(row, "added_from_conversation_id"),
      fileName: text(row, "file_name"),
      extension: text(row, "extension"),
      declaredMediaType: text(row, "declared_media_type"),
      detectedMediaType: text(row, "detected_media_type"),
      byteSize: numeric(row, "byte_size"),
      sha256: text(row, "sha256"),
      parser: text(row, "parser"),
      riskLevel: text(row, "risk_level") as ProjectSourceSummary["riskLevel"],
      quarantined: numeric(row, "quarantined") === 1,
      artifactSha256,
      createdAt: text(row, "created_at"),
      updatedAt: text(row, "updated_at"),
    };
  }

  updateTask(input: {
    p05TaskId: string;
    status?: P05TaskState;
    stage?: string;
    stableConcurrency?: number;
    lastArtifactPath?: string;
    checkpoint?: JsonObject;
    error?: JsonObject | null;
  }): void {
    const current = this.database.prepare("SELECT * FROM p05_tasks WHERE id = ?").get(
      input.p05TaskId,
    ) as SqlRow | undefined;
    if (!current) throw new Error(`P05 task ${input.p05TaskId} was not found`);
    this.database.prepare(
      `UPDATE p05_tasks SET status = ?, stage = ?, stable_concurrency = ?,
       last_artifact_path = ?, checkpoint_json = ?, error_json = ?, updated_at = ?
       WHERE id = ?`,
    ).run(
      input.status ?? text(current, "status"),
      input.stage ?? text(current, "stage"),
      input.stableConcurrency ?? numeric(current, "stable_concurrency"),
      input.lastArtifactPath ?? optionalText(current, "last_artifact_path"),
      JSON.stringify(input.checkpoint ?? json(text(current, "checkpoint_json")) ?? {}),
      input.error === undefined
        ? optionalText(current, "error_json")
        : input.error === null ? null : JSON.stringify(input.error),
      now(),
      input.p05TaskId,
    );
  }

  appendEvent(input: {
    p05TaskId: string;
    eventType: string;
    summary: string;
    detail?: JsonObject;
    runId?: string;
    agentId?: string;
    strategy?: string;
  }): P05EventSummary {
    return this.transaction(() => {
      const task = this.database.prepare(
        "SELECT next_event_sequence FROM p05_tasks WHERE id = ?",
      ).get(input.p05TaskId) as SqlRow | undefined;
      if (!task) throw new Error(`P05 task ${input.p05TaskId} was not found`);
      const sequence = numeric(task, "next_event_sequence");
      const eventId = `p05_event_${randomUUID()}`;
      const timestamp = now();
      this.database.prepare(
        `INSERT INTO p05_events(
          id, p05_task_id, sequence, event_type, run_id, agent_id, strategy,
          summary, detail_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      ).run(
        eventId,
        input.p05TaskId,
        sequence,
        input.eventType,
        input.runId ?? null,
        input.agentId ?? null,
        input.strategy ?? null,
        input.summary,
        JSON.stringify(input.detail ?? {}),
        timestamp,
      );
      this.database.prepare(
        "UPDATE p05_tasks SET next_event_sequence = ?, updated_at = ? WHERE id = ?",
      ).run(sequence + 1, timestamp, input.p05TaskId);
      return {
        eventId,
        sequence,
        eventType: input.eventType,
        runId: input.runId ?? null,
        agentId: input.agentId ?? null,
        strategy: input.strategy ?? null,
        summary: input.summary,
        detail: input.detail ?? {},
        createdAt: timestamp,
      };
    });
  }

  startModelCall(input: {
    p05TaskId: string;
    runId: string;
    agentId: string;
    strategy: string;
    purpose: string;
    promptSha256: string;
  }): string {
    const callId = `p05_call_${randomUUID()}`;
    this.database.prepare(
      `INSERT INTO p05_model_calls(
        id, p05_task_id, run_id, agent_id, strategy, purpose, prompt_sha256,
        status, created_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, 'STARTED', ?)`,
    ).run(
      callId,
      input.p05TaskId,
      input.runId,
      input.agentId,
      input.strategy,
      input.purpose,
      input.promptSha256,
      now(),
    );
    return callId;
  }

  finishModelCall(input: {
    callId: string;
    status: "COMPLETED" | "FAILED";
    responseSha256?: string;
    httpStatus?: number;
    promptTokens?: number;
    completionTokens?: number;
    cachedTokens?: number;
    totalTokens?: number;
    latencyMs: number;
    artifactPath?: string;
    errorCode?: string;
  }): void {
    this.transaction(() => {
      const row = this.database.prepare(
        "SELECT p05_task_id FROM p05_model_calls WHERE id = ?",
      ).get(input.callId) as SqlRow | undefined;
      if (!row) throw new Error(`P05 model call ${input.callId} was not found`);
      this.database.prepare(
        `UPDATE p05_model_calls SET status = ?, response_sha256 = ?, http_status = ?,
         prompt_tokens = ?, completion_tokens = ?, cached_tokens = ?, total_tokens = ?,
         latency_ms = ?, artifact_path = ?, error_code = ?, completed_at = ?
         WHERE id = ?`,
      ).run(
        input.status,
        input.responseSha256 ?? null,
        input.httpStatus ?? null,
        input.promptTokens ?? 0,
        input.completionTokens ?? 0,
        input.cachedTokens ?? 0,
        input.totalTokens ?? 0,
        input.latencyMs,
        input.artifactPath ?? null,
        input.errorCode ?? null,
        now(),
        input.callId,
      );
      if (input.status === "COMPLETED") {
        this.database.prepare(
          `UPDATE p05_tasks SET
           verified_total_tokens = verified_total_tokens + ?,
           prompt_tokens = prompt_tokens + ?,
           completion_tokens = completion_tokens + ?,
           cached_tokens = cached_tokens + ?,
           updated_at = ?
           WHERE id = ?`,
        ).run(
          input.totalTokens ?? 0,
          input.promptTokens ?? 0,
          input.completionTokens ?? 0,
          input.cachedTokens ?? 0,
          now(),
          text(row, "p05_task_id"),
        );
      }
    });
  }

  restartModelCall(callId: string): void {
    const result = this.database.prepare(
      `UPDATE p05_model_calls SET status = 'STARTED', response_sha256 = NULL,
       http_status = NULL, prompt_tokens = 0, completion_tokens = 0,
       cached_tokens = 0, total_tokens = 0, latency_ms = NULL,
       artifact_path = NULL, error_code = NULL, completed_at = NULL
       WHERE id = ?`,
    ).run(callId);
    if (result.changes !== 1) throw new Error(`P05 model call ${callId} was not found`);
  }

  registerTool(input: {
    p05TaskId: string;
    name: string;
    status: string;
    codeSha256: string;
    testSha256: string;
    validation: JsonObject;
    invocation: JsonObject;
    relativeWorkspace: string;
  }): void {
    this.database.prepare(
      `INSERT OR REPLACE INTO p05_tools(
        id, p05_task_id, name, status, code_sha256, test_sha256,
        validation_json, invocation_json, relative_workspace, created_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    ).run(
      `p05_tool_${randomUUID()}`,
      input.p05TaskId,
      input.name,
      input.status,
      input.codeSha256,
      input.testSha256,
      JSON.stringify(input.validation),
      JSON.stringify(input.invocation),
      input.relativeWorkspace,
      now(),
    );
  }

  registerVendorCandidate(input: {
    p05TaskId?: string;
    name: string;
    repositoryUrl: string;
    commitSha: string;
    licenseSpdx: string;
    decision: "ACCEPT" | "REJECT" | "AUDIT_ONLY";
    evidence: JsonObject;
  }): void {
    this.database.prepare(
      `INSERT OR IGNORE INTO p05_vendor_candidates(
        id, p05_task_id, name, repository_url, commit_sha, license_spdx,
        decision, evidence_json, created_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    ).run(
      `p05_vendor_${randomUUID()}`,
      input.p05TaskId ?? null,
      input.name,
      input.repositoryUrl,
      input.commitSha,
      input.licenseSpdx,
      input.decision,
      JSON.stringify(input.evidence),
      now(),
    );
  }

  createBatch(input: {
    p05TaskId: string;
    batchIndex: number;
    batchKind: P05BatchSummary["batchKind"];
    shots: number;
    circuits: Array<{ family: string; manifest: JsonObject; qcis: string; qcisSha256: string }>;
  }): string {
    if (input.circuits.length !== 50) throw new Error("P05 batch requires exactly 50 circuits");
    return this.transaction(() => {
      const existing = this.database.prepare(
        "SELECT id FROM p05_batches WHERE p05_task_id = ? AND batch_index = ?",
      ).get(input.p05TaskId, input.batchIndex) as SqlRow | undefined;
      if (existing) return text(existing, "id");
      const batchId = `p05_batch_${randomUUID()}`;
      const timestamp = now();
      this.database.prepare(
        `INSERT INTO p05_batches(
          id, p05_task_id, batch_index, batch_kind, status, shots, backend,
          lease_key, created_at
        ) VALUES (?, ?, ?, ?, 'REGISTERED', ?, 'tianyan176', ?, ?)`,
      ).run(
        batchId,
        input.p05TaskId,
        input.batchIndex,
        input.batchKind,
        input.shots,
        `${input.p05TaskId}:batch:${input.batchIndex}:tianyan176`,
        timestamp,
      );
      for (const [index, circuit] of input.circuits.entries()) {
        this.database.prepare(
          `INSERT INTO p05_circuits(
            id, p05_batch_id, circuit_index, family, manifest_json, qcis,
            qcis_sha256, idempotency_key, state, created_at, updated_at
          ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'REGISTERED', ?, ?)`,
        ).run(
          `p05_circuit_${randomUUID()}`,
          batchId,
          index,
          circuit.family,
          JSON.stringify(circuit.manifest),
          circuit.qcis,
          circuit.qcisSha256,
          `${input.p05TaskId}:batch:${input.batchIndex}:circuit:${index}:${circuit.qcisSha256}`,
          timestamp,
          timestamp,
        );
      }
      return batchId;
    });
  }

  failStartedModelCallsForRecovery(p05TaskId: string): number {
    const result = this.database.prepare(
      `UPDATE p05_model_calls
       SET status = 'FAILED', error_code = 'CONTROL_PLANE_RESTART_RECOVERY',
           latency_ms = 0, completed_at = ?
       WHERE p05_task_id = ? AND status = 'STARTED'`,
    ).run(now(), p05TaskId);
    return Number(result.changes);
  }

  replaceRegisteredBatchCircuits(input: {
    batchId: string;
    p05TaskId: string;
    batchIndex: number;
    circuits: Array<{ family: string; manifest: JsonObject; qcis: string; qcisSha256: string }>;
  }): void {
    if (input.circuits.length !== 50) throw new Error("P05 replacement requires exactly 50 circuits");
    this.transaction(() => {
      const batch = this.database.prepare(
        "SELECT status, receipt_json FROM p05_batches WHERE id = ? AND p05_task_id = ? AND batch_index = ?",
      ).get(input.batchId, input.p05TaskId, input.batchIndex) as SqlRow | undefined;
      if (!batch || text(batch, "status") !== "REGISTERED" || optionalText(batch, "receipt_json") !== null) {
        throw new Error("P05 circuits may only be replaced before any submission attempt");
      }
      const submitted = this.database.prepare(
        "SELECT COUNT(*) AS count FROM p05_circuits WHERE p05_batch_id = ? AND query_id IS NOT NULL",
      ).get(input.batchId) as SqlRow;
      if (numeric(submitted, "count") !== 0) {
        throw new Error("P05 registered batch unexpectedly contains Query IDs");
      }
      this.database.prepare("DELETE FROM p05_circuits WHERE p05_batch_id = ?").run(input.batchId);
      const timestamp = now();
      for (const [index, circuit] of input.circuits.entries()) {
        this.database.prepare(
          `INSERT INTO p05_circuits(
            id, p05_batch_id, circuit_index, family, manifest_json, qcis,
            qcis_sha256, idempotency_key, state, created_at, updated_at
          ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'REGISTERED', ?, ?)`,
        ).run(
          `p05_circuit_${randomUUID()}`,
          input.batchId,
          index,
          circuit.family,
          JSON.stringify(circuit.manifest),
          circuit.qcis,
          circuit.qcisSha256,
          `${input.p05TaskId}:batch:${input.batchIndex}:circuit:${index}:${circuit.qcisSha256}`,
          timestamp,
          timestamp,
        );
      }
    });
  }

  markBatchSubmitting(batchId: string): void {
    const result = this.database.prepare(
      "UPDATE p05_batches SET status = 'SUBMITTING' WHERE id = ? AND status = 'REGISTERED'",
    ).run(batchId);
    if (result.changes !== 1) throw new Error("P05 batch lease is not in a submit-ready state");
  }

  persistBatchSubmission(batchId: string, queryIds: string[], receipt: JsonObject): void {
    if (queryIds.length !== 50) throw new Error("P05 batch receipt requires 50 Query IDs");
    this.transaction(() => {
      const circuits = rows(
        this.database.prepare(
          "SELECT id FROM p05_circuits WHERE p05_batch_id = ? ORDER BY circuit_index",
        ),
        batchId,
      );
      if (circuits.length !== 50) throw new Error("P05 registered circuit count changed");
      for (const [index, circuit] of circuits.entries()) {
        const queryId = queryIds[index];
        if (!queryId) throw new Error(`P05 batch receipt is missing Query ID ${index}`);
        this.database.prepare(
          `UPDATE p05_circuits SET query_id = ?, state = 'SUBMITTED',
           receipt_json = ?, updated_at = ? WHERE id = ? AND query_id IS NULL`,
        ).run(
          queryId,
          JSON.stringify({ query_id: queryId, batch_receipt: receipt }),
          now(),
          text(circuit, "id"),
        );
      }
      this.database.prepare(
        `UPDATE p05_batches SET status = 'SUBMITTED', receipt_json = ?,
         submitted_at = ? WHERE id = ?`,
      ).run(JSON.stringify(receipt), now(), batchId);
    });
  }

  markBatchQuerying(batchId: string): void {
    this.database.prepare(
      "UPDATE p05_batches SET status = 'QUERYING' WHERE id = ? AND status IN ('SUBMITTED', 'QUERYING')",
    ).run(batchId);
  }

  persistBatchResults(batchId: string, results: JsonObject[], metrics: JsonObject): void {
    this.transaction(() => {
      const circuits = rows(
        this.database.prepare(
          "SELECT id FROM p05_circuits WHERE p05_batch_id = ? ORDER BY circuit_index",
        ),
        batchId,
      );
      for (const [index, circuit] of circuits.entries()) {
        const result = results[index];
        this.database.prepare(
          `UPDATE p05_circuits SET state = ?, raw_result_json = ?, updated_at = ?
           WHERE id = ?`,
        ).run(result ? "COMPLETED" : "UNKNOWN", result ? JSON.stringify(result) : null, now(), text(circuit, "id"));
      }
      this.database.prepare(
        `UPDATE p05_batches SET status = ?, metrics_json = ?, completed_at = ?
         WHERE id = ?`,
      ).run(results.length === 50 ? "COMPLETED" : "PARTIAL", JSON.stringify(metrics), now(), batchId);
    });
  }

  getBatchCircuits(batchId: string): Array<{
    circuitId: string;
    family: string;
    qcis: string;
    qcisSha256: string;
    queryId: string | null;
  }> {
    return rows(
      this.database.prepare("SELECT * FROM p05_circuits WHERE p05_batch_id = ? ORDER BY circuit_index"),
      batchId,
    ).map((row) => ({
      circuitId: text(row, "id"),
      family: text(row, "family"),
      qcis: text(row, "qcis"),
      qcisSha256: text(row, "qcis_sha256"),
      queryId: optionalText(row, "query_id"),
    }));
  }

  private eventFromRow(row: SqlRow): P05EventSummary {
    return {
      eventId: text(row, "id"),
      sequence: numeric(row, "sequence"),
      eventType: text(row, "event_type"),
      runId: optionalText(row, "run_id"),
      agentId: optionalText(row, "agent_id"),
      strategy: optionalText(row, "strategy"),
      summary: text(row, "summary"),
      detail: json(text(row, "detail_json"))!,
      createdAt: text(row, "created_at"),
    };
  }

  private modelCallFromRow(row: SqlRow): P05ModelCallSummary {
    return {
      callId: text(row, "id"),
      runId: text(row, "run_id"),
      agentId: text(row, "agent_id"),
      strategy: text(row, "strategy"),
      purpose: text(row, "purpose"),
      promptSha256: text(row, "prompt_sha256"),
      responseSha256: optionalText(row, "response_sha256"),
      status: text(row, "status") as P05ModelCallSummary["status"],
      httpStatus: row.http_status === null ? null : numeric(row, "http_status"),
      promptTokens: numeric(row, "prompt_tokens"),
      completionTokens: numeric(row, "completion_tokens"),
      cachedTokens: numeric(row, "cached_tokens"),
      totalTokens: numeric(row, "total_tokens"),
      latencyMs: row.latency_ms === null ? null : numeric(row, "latency_ms"),
      artifactPath: optionalText(row, "artifact_path"),
      errorCode: optionalText(row, "error_code"),
      createdAt: text(row, "created_at"),
      completedAt: optionalText(row, "completed_at"),
    };
  }

  private toolFromRow(row: SqlRow): P05ToolSummary {
    return {
      toolId: text(row, "id"),
      name: text(row, "name"),
      status: text(row, "status"),
      codeSha256: text(row, "code_sha256"),
      testSha256: text(row, "test_sha256"),
      validation: json(text(row, "validation_json"))!,
      invocation: json(text(row, "invocation_json"))!,
      relativeWorkspace: text(row, "relative_workspace"),
      createdAt: text(row, "created_at"),
    };
  }

  private circuitFromRow(row: SqlRow): P05CircuitSummary {
    return {
      circuitId: text(row, "id"),
      circuitIndex: numeric(row, "circuit_index"),
      family: text(row, "family"),
      manifest: json(text(row, "manifest_json"))!,
      qcis: text(row, "qcis"),
      qcisSha256: text(row, "qcis_sha256"),
      idempotencyKey: text(row, "idempotency_key"),
      queryId: optionalText(row, "query_id"),
      state: text(row, "state") as P05CircuitSummary["state"],
      receipt: json(optionalText(row, "receipt_json")),
      rawResult: json(optionalText(row, "raw_result_json")),
      createdAt: text(row, "created_at"),
      updatedAt: text(row, "updated_at"),
    };
  }

  private batchFromRow(row: SqlRow): P05BatchSummary {
    const batchId = text(row, "id");
    return {
      batchId,
      batchIndex: numeric(row, "batch_index"),
      batchKind: text(row, "batch_kind") as P05BatchSummary["batchKind"],
      status: text(row, "status") as P05BatchSummary["status"],
      shots: numeric(row, "shots"),
      backend: "tianyan176",
      leaseKey: text(row, "lease_key"),
      receipt: json(optionalText(row, "receipt_json")),
      metrics: json(optionalText(row, "metrics_json")),
      circuits: rows(
        this.database.prepare(
          "SELECT * FROM p05_circuits WHERE p05_batch_id = ? ORDER BY circuit_index",
        ),
        batchId,
      ).map((circuit) => this.circuitFromRow(circuit)),
      createdAt: text(row, "created_at"),
      submittedAt: optionalText(row, "submitted_at"),
      completedAt: optionalText(row, "completed_at"),
    };
  }

  getTask(p05TaskId: string): P05TaskDetail {
    const row = this.database.prepare("SELECT * FROM p05_tasks WHERE id = ?").get(
      p05TaskId,
    ) as SqlRow | undefined;
    if (!row) throw new Error(`P05 task ${p05TaskId} was not found`);
    const hardDeadlineAt = text(row, "hard_deadline_at");
    const remainingSeconds = Math.max(0, (Date.parse(hardDeadlineAt) - Date.now()) / 1000);
    const verifiedTotalTokens = numeric(row, "verified_total_tokens");
    const remainingTokens = Math.max(0, 100_000_000 - verifiedTotalTokens);
    const vendorCandidates: P05VendorCandidateSummary[] = rows(
      this.database.prepare(
        "SELECT * FROM p05_vendor_candidates WHERE p05_task_id = ? OR p05_task_id IS NULL ORDER BY name",
      ),
      p05TaskId,
    ).map((candidate) => ({
      name: text(candidate, "name"),
      repositoryUrl: text(candidate, "repository_url"),
      commitSha: text(candidate, "commit_sha"),
      licenseSpdx: text(candidate, "license_spdx"),
      decision: text(candidate, "decision") as P05VendorCandidateSummary["decision"],
      evidence: json(text(candidate, "evidence_json"))!,
      createdAt: text(candidate, "created_at"),
    }));
    return {
      schemaVersion: CONTRACT_SCHEMA_VERSION,
      p05TaskId,
      conversationId: text(row, "conversation_id"),
      taskId: text(row, "task_id"),
      provider: "openai/getoken",
      modelId: "gpt-5.6-sol",
      status: text(row, "status") as P05TaskState,
      stage: text(row, "stage"),
      objective: text(row, "objective"),
      taskStartedAt: text(row, "task_started_at"),
      hardDeadlineAt,
      remainingSeconds,
      formalTestSealed: true,
      verifiedTotalTokens,
      promptTokens: numeric(row, "prompt_tokens"),
      completionTokens: numeric(row, "completion_tokens"),
      cachedTokens: numeric(row, "cached_tokens"),
      remainingTokens,
      requiredTokensPerSecond: remainingSeconds > 0 ? remainingTokens / remainingSeconds : Number.POSITIVE_INFINITY,
      stableConcurrency: numeric(row, "stable_concurrency"),
      lastArtifactPath: optionalText(row, "last_artifact_path"),
      checkpoint: json(text(row, "checkpoint_json"))!,
      error: json(optionalText(row, "error_json")),
      uploads: this.listUploads(text(row, "conversation_id")),
      events: rows(
        this.database.prepare(
          "SELECT * FROM p05_events WHERE p05_task_id = ? ORDER BY sequence DESC LIMIT 500",
        ),
        p05TaskId,
      ).reverse().map((event) => this.eventFromRow(event)),
      modelCalls: rows(
        this.database.prepare(
          "SELECT * FROM p05_model_calls WHERE p05_task_id = ? ORDER BY created_at DESC LIMIT 500",
        ),
        p05TaskId,
      ).map((call) => this.modelCallFromRow(call)),
      tools: rows(
        this.database.prepare("SELECT * FROM p05_tools WHERE p05_task_id = ? ORDER BY name"),
        p05TaskId,
      ).map((tool) => this.toolFromRow(tool)),
      vendorCandidates,
      batches: rows(
        this.database.prepare("SELECT * FROM p05_batches WHERE p05_task_id = ? ORDER BY batch_index"),
        p05TaskId,
      ).map((batch) => this.batchFromRow(batch)),
      createdAt: text(row, "created_at"),
      updatedAt: text(row, "updated_at"),
    };
  }

  listTasks(): P05TaskDetail[] {
    return rows(this.database.prepare("SELECT id FROM p05_tasks ORDER BY created_at DESC")).map(
      (row) => this.getTask(text(row, "id")),
    );
  }
}
