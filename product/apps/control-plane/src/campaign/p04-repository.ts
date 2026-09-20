import { createHash, randomUUID } from "node:crypto";
import type { DatabaseSync, StatementSync } from "node:sqlite";

import {
  CONTRACT_SCHEMA_VERSION,
  type CampaignSummary,
  type JsonObject,
  type P04CampaignDetail,
  type P04FaultInjectionSummary,
  type P04GeneratedToolSummary,
  type P04RunLane,
  type P04RunState,
  type P04RunSummary,
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

function numberValue(row: SqlRow, key: string): number {
  const value = row[key];
  if (typeof value !== "number") throw new Error(`expected ${key} to be numeric`);
  return value;
}

function hash(value: unknown): string {
  return createHash("sha256").update(typeof value === "string" ? value : JSON.stringify(value)).digest("hex");
}

export class P04Repository {
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

  ensureRuns(campaignId: string): P04RunSummary[] {
    const timestamp = now();
    this.transaction(() => {
      for (const lane of ["finance", "tool_factory", "evidence_audit"] as const) {
        const runId = `p04_run_${lane}_${randomUUID()}`;
        this.database.prepare(
          `INSERT OR IGNORE INTO p04_runs(
            id, campaign_id, lane, status, relative_workspace, created_at, updated_at
          ) VALUES (?, ?, ?, 'CREATED', ?, ?, ?)`,
        ).run(runId, campaignId, lane, `.local/campaigns/${campaignId}/runs/${runId}`, timestamp, timestamp);
      }
    });
    return this.listRuns(campaignId);
  }

  private runFromRow(row: SqlRow): P04RunSummary {
    return {
      schemaVersion: CONTRACT_SCHEMA_VERSION,
      runId: text(row, "id"),
      campaignId: text(row, "campaign_id"),
      lane: text(row, "lane") as P04RunLane,
      status: text(row, "status") as P04RunState,
      relativeWorkspace: text(row, "relative_workspace"),
      workerId: optionalText(row, "worker_id"),
      processId: row.process_id === null ? null : numberValue(row, "process_id"),
      heartbeatAt: optionalText(row, "heartbeat_at"),
      eventCount: numberValue(row, "event_count"),
      artifactCount: numberValue(row, "artifact_count"),
      recoveryCount: numberValue(row, "recovery_count"),
      startedAt: optionalText(row, "started_at"),
      completedAt: optionalText(row, "completed_at"),
      createdAt: text(row, "created_at"),
      updatedAt: text(row, "updated_at"),
    };
  }

  getRun(runId: string): P04RunSummary {
    const row = this.database.prepare(
      `SELECT p04_runs.*, p04_run_leases.heartbeat_at,
       (SELECT COUNT(*) FROM p04_run_events WHERE p04_run_events.run_id = p04_runs.id) AS event_count,
       (SELECT COUNT(*) FROM p04_artifact_claims WHERE p04_artifact_claims.run_id = p04_runs.id) AS artifact_count
       FROM p04_runs LEFT JOIN p04_run_leases ON p04_run_leases.run_id = p04_runs.id
       WHERE p04_runs.id = ?`,
    ).get(runId) as SqlRow | undefined;
    if (!row) throw new Error(`P04 run ${runId} was not found`);
    return this.runFromRow(row);
  }

  listRuns(campaignId: string): P04RunSummary[] {
    return rows(this.database.prepare(
      `SELECT p04_runs.*, p04_run_leases.heartbeat_at,
       (SELECT COUNT(*) FROM p04_run_events WHERE p04_run_events.run_id = p04_runs.id) AS event_count,
       (SELECT COUNT(*) FROM p04_artifact_claims WHERE p04_artifact_claims.run_id = p04_runs.id) AS artifact_count
       FROM p04_runs LEFT JOIN p04_run_leases ON p04_run_leases.run_id = p04_runs.id
       WHERE p04_runs.campaign_id = ?
       ORDER BY CASE p04_runs.lane WHEN 'finance' THEN 1 WHEN 'tool_factory' THEN 2 ELSE 3 END`,
    ), campaignId).map((row) => this.runFromRow(row));
  }

  acquireRunLease(runId: string, workerId: string, processId: number, recovery: boolean, ttlSeconds = 120): P04RunSummary {
    if (!Number.isInteger(processId) || processId <= 1) throw new Error("P04 process id is invalid");
    const timestamp = now();
    const expiresAt = new Date(Date.now() + ttlSeconds * 1000).toISOString();
    this.transaction(() => {
      const current = this.database.prepare("SELECT * FROM p04_run_leases WHERE run_id = ?").get(runId) as SqlRow | undefined;
      if (current) {
        const released = optionalText(current, "released_at") !== null;
        const expired = Date.parse(text(current, "expires_at")) <= Date.now();
        if (!released && !expired && !recovery && text(current, "worker_id") !== workerId) {
          throw new Error(`P04 run ${runId} has a live lease`);
        }
        this.database.prepare("DELETE FROM p04_run_leases WHERE run_id = ?").run(runId);
      }
      this.database.prepare(
        `INSERT INTO p04_run_leases(
          run_id, worker_id, process_id, acquired_at, heartbeat_at, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?)`,
      ).run(runId, workerId, processId, timestamp, timestamp, expiresAt);
      this.database.prepare(
        `UPDATE p04_runs SET status = ?, worker_id = ?, process_id = ?,
         recovery_count = recovery_count + ?, started_at = COALESCE(started_at, ?), completed_at = NULL, updated_at = ?
         WHERE id = ?`,
      ).run(recovery ? "RECOVERING" : "RUNNING", workerId, processId, recovery ? 1 : 0, timestamp, timestamp, runId);
    });
    return this.getRun(runId);
  }

  heartbeatRun(runId: string, workerId: string, ttlSeconds = 120): void {
    const timestamp = now();
    const expiresAt = new Date(Date.now() + ttlSeconds * 1000).toISOString();
    const result = this.database.prepare(
      `UPDATE p04_run_leases SET heartbeat_at = ?, expires_at = ?
       WHERE run_id = ? AND worker_id = ? AND released_at IS NULL`,
    ).run(timestamp, expiresAt, runId, workerId);
    if (result.changes !== 1) throw new Error("P04 run heartbeat was rejected");
    this.database.prepare("UPDATE p04_runs SET updated_at = ? WHERE id = ?").run(timestamp, runId);
  }

  releaseRunLease(runId: string, workerId: string): void {
    this.database.prepare(
      "UPDATE p04_run_leases SET released_at = ?, expires_at = ? WHERE run_id = ? AND worker_id = ?",
    ).run(now(), now(), runId, workerId);
  }

  setRunState(runId: string, state: P04RunState): P04RunSummary {
    const timestamp = now();
    const terminal = new Set<P04RunState>(["COMPLETED", "BLOCKED", "FAILED"]).has(state);
    this.database.prepare(
      `UPDATE p04_runs SET status = ?, completed_at = CASE WHEN ? THEN COALESCE(completed_at, ?) ELSE NULL END,
       updated_at = ? WHERE id = ?`,
    ).run(state, terminal ? 1 : 0, timestamp, timestamp, runId);
    return this.getRun(runId);
  }

  appendEvent(runId: string, eventType: string, payload: JsonObject): number {
    return this.transaction(() => {
      const row = this.database.prepare(
        "SELECT COALESCE(MAX(sequence), 0) AS maximum FROM p04_run_events WHERE run_id = ?",
      ).get(runId) as SqlRow;
      const sequence = numberValue(row, "maximum") + 1;
      this.database.prepare(
        `INSERT INTO p04_run_events(run_id, sequence, event_type, payload_hash, payload_json, created_at)
         VALUES (?, ?, ?, ?, ?, ?)`,
      ).run(runId, sequence, eventType, hash(payload), JSON.stringify(payload), now());
      return sequence;
    });
  }

  latestEvent(runId: string): { sequence: number; eventType: string; payload: JsonObject; createdAt: string } | null {
    const row = this.database.prepare(
      "SELECT * FROM p04_run_events WHERE run_id = ? ORDER BY sequence DESC LIMIT 1",
    ).get(runId) as SqlRow | undefined;
    return row ? {
      sequence: numberValue(row, "sequence"),
      eventType: text(row, "event_type"),
      payload: JSON.parse(text(row, "payload_json")) as JsonObject,
      createdAt: text(row, "created_at"),
    } : null;
  }

  claimArtifact(runId: string, artifactSha256: string, logicalName: string, relativeOutputPath: string): void {
    this.database.prepare(
      `INSERT OR IGNORE INTO p04_artifact_claims(
        run_id, artifact_sha256, logical_name, relative_output_path, created_at
      ) VALUES (?, ?, ?, ?, ?)`,
    ).run(runId, artifactSha256, logicalName, relativeOutputPath, now());
  }

  registerTool(input: {
    campaignId: string;
    runId: string;
    name: P04GeneratedToolSummary["name"];
    specHash: string;
    codeHash: string;
    testHash: string;
  }): P04GeneratedToolSummary {
    const existing = this.database.prepare(
      "SELECT id FROM p04_generated_tools WHERE campaign_id = ? AND name = ?",
    ).get(input.campaignId, input.name) as SqlRow | undefined;
    if (existing) return this.getTool(text(existing, "id"));
    const id = `p04_tool_${randomUUID()}`;
    const timestamp = now();
    this.database.prepare(
      `INSERT INTO p04_generated_tools(
        id, campaign_id, run_id, name, status, spec_hash, code_hash, test_hash, created_at, updated_at
      ) VALUES (?, ?, ?, ?, 'GENERATED', ?, ?, ?, ?, ?)`,
    ).run(id, input.campaignId, input.runId, input.name, input.specHash, input.codeHash, input.testHash, timestamp, timestamp);
    return this.getTool(id);
  }

  updateTool(input: {
    toolId: string;
    status: P04GeneratedToolSummary["status"];
    sourceArtifactSha256?: string;
    testArtifactSha256?: string;
  }): P04GeneratedToolSummary {
    this.database.prepare(
      `UPDATE p04_generated_tools SET status = ?,
       source_artifact_sha256 = COALESCE(?, source_artifact_sha256),
       test_artifact_sha256 = COALESCE(?, test_artifact_sha256), updated_at = ? WHERE id = ?`,
    ).run(input.status, input.sourceArtifactSha256 ?? null, input.testArtifactSha256 ?? null, now(), input.toolId);
    return this.getTool(input.toolId);
  }

  updateToolHashes(toolId: string, codeHash: string, testHash: string): P04GeneratedToolSummary {
    this.database.prepare(
      "UPDATE p04_generated_tools SET code_hash = ?, test_hash = ?, updated_at = ? WHERE id = ?",
    ).run(codeHash, testHash, now(), toolId);
    return this.getTool(toolId);
  }

  private toolFromRow(row: SqlRow): P04GeneratedToolSummary {
    return {
      schemaVersion: CONTRACT_SCHEMA_VERSION,
      toolId: text(row, "id"),
      campaignId: text(row, "campaign_id"),
      runId: text(row, "run_id"),
      name: text(row, "name") as P04GeneratedToolSummary["name"],
      status: text(row, "status") as P04GeneratedToolSummary["status"],
      specHash: text(row, "spec_hash"),
      codeHash: text(row, "code_hash"),
      testHash: text(row, "test_hash"),
      sourceArtifactSha256: optionalText(row, "source_artifact_sha256"),
      testArtifactSha256: optionalText(row, "test_artifact_sha256"),
      invocationCount: numberValue(row, "invocation_count"),
      createdAt: text(row, "created_at"),
      updatedAt: text(row, "updated_at"),
    };
  }

  getTool(toolId: string): P04GeneratedToolSummary {
    const row = this.database.prepare(
      `SELECT p04_generated_tools.*,
       (SELECT COUNT(*) FROM p04_tool_invocations WHERE tool_id = p04_generated_tools.id AND status = 'COMPLETED') AS invocation_count
       FROM p04_generated_tools WHERE id = ?`,
    ).get(toolId) as SqlRow | undefined;
    if (!row) throw new Error(`P04 tool ${toolId} was not found`);
    return this.toolFromRow(row);
  }

  listTools(campaignId: string): P04GeneratedToolSummary[] {
    return rows(this.database.prepare(
      `SELECT p04_generated_tools.*,
       (SELECT COUNT(*) FROM p04_tool_invocations WHERE tool_id = p04_generated_tools.id AND status = 'COMPLETED') AS invocation_count
       FROM p04_generated_tools WHERE campaign_id = ? ORDER BY name`,
    ), campaignId).map((row) => this.toolFromRow(row));
  }

  beginToolInvocation(toolId: string, runId: string, idempotencyKey: string, inputHash: string): string {
    const existing = this.database.prepare("SELECT id FROM p04_tool_invocations WHERE idempotency_key = ?")
      .get(idempotencyKey) as SqlRow | undefined;
    if (existing) return text(existing, "id");
    const id = `p04_invocation_${randomUUID()}`;
    this.database.prepare(
      `INSERT INTO p04_tool_invocations(
        id, tool_id, run_id, idempotency_key, input_hash, status, created_at
      ) VALUES (?, ?, ?, ?, ?, 'RUNNING', ?)`,
    ).run(id, toolId, runId, idempotencyKey, inputHash, now());
    return id;
  }

  completeToolInvocation(input: {
    invocationId: string;
    status: "COMPLETED" | "FAILED";
    outputArtifactSha256?: string;
    exitCode: number;
    durationSeconds: number;
  }): void {
    this.database.prepare(
      `UPDATE p04_tool_invocations SET status = ?, output_artifact_sha256 = ?, exit_code = ?,
       duration_seconds = ?, completed_at = ? WHERE id = ?`,
    ).run(
      input.status,
      input.outputArtifactSha256 ?? null,
      input.exitCode,
      input.durationSeconds,
      now(),
      input.invocationId,
    );
  }

  planFault(input: {
    campaignId: string;
    runId: string;
    kind: P04FaultInjectionSummary["kind"];
    targetProcessId: number;
    checkpointArtifactSha256?: string;
  }): P04FaultInjectionSummary {
    const existing = this.database.prepare(
      "SELECT id FROM p04_fault_injections WHERE campaign_id = ? AND kind = ?",
    ).get(input.campaignId, input.kind) as SqlRow | undefined;
    if (existing) return this.getFault(text(existing, "id"));
    const id = `p04_fault_${randomUUID()}`;
    const timestamp = now();
    this.database.prepare(
      `INSERT INTO p04_fault_injections(
        id, campaign_id, run_id, kind, target_process_id, state, checkpoint_artifact_sha256, created_at, updated_at
      ) VALUES (?, ?, ?, ?, ?, 'PLANNED', ?, ?, ?)`,
    ).run(
      id,
      input.campaignId,
      input.runId,
      input.kind,
      input.targetProcessId,
      input.checkpointArtifactSha256 ?? null,
      timestamp,
      timestamp,
    );
    return this.getFault(id);
  }

  markFaultInjected(faultId: string): P04FaultInjectionSummary {
    const timestamp = now();
    this.database.prepare(
      "UPDATE p04_fault_injections SET state = 'INJECTED', injected_at = ?, updated_at = ? WHERE id = ?",
    ).run(timestamp, timestamp, faultId);
    return this.getFault(faultId);
  }

  recoverFault(faultId: string, recoveryArtifactSha256: string): P04FaultInjectionSummary {
    const timestamp = now();
    this.database.prepare(
      `UPDATE p04_fault_injections SET state = 'RECOVERED', recovery_artifact_sha256 = ?,
       recovered_at = ?, updated_at = ? WHERE id = ?`,
    ).run(recoveryArtifactSha256, timestamp, timestamp, faultId);
    return this.getFault(faultId);
  }

  private faultFromRow(row: SqlRow): P04FaultInjectionSummary {
    return {
      schemaVersion: CONTRACT_SCHEMA_VERSION,
      faultId: text(row, "id"),
      campaignId: text(row, "campaign_id"),
      runId: text(row, "run_id"),
      kind: text(row, "kind") as P04FaultInjectionSummary["kind"],
      targetProcessId: numberValue(row, "target_process_id"),
      state: text(row, "state") as P04FaultInjectionSummary["state"],
      checkpointArtifactSha256: optionalText(row, "checkpoint_artifact_sha256"),
      recoveryArtifactSha256: optionalText(row, "recovery_artifact_sha256"),
      injectedAt: optionalText(row, "injected_at"),
      recoveredAt: optionalText(row, "recovered_at"),
      createdAt: text(row, "created_at"),
      updatedAt: text(row, "updated_at"),
    };
  }

  getFault(faultId: string): P04FaultInjectionSummary {
    const row = this.database.prepare("SELECT * FROM p04_fault_injections WHERE id = ?").get(faultId) as SqlRow | undefined;
    if (!row) throw new Error(`P04 fault ${faultId} was not found`);
    return this.faultFromRow(row);
  }

  listFaults(campaignId: string): P04FaultInjectionSummary[] {
    return rows(this.database.prepare(
      "SELECT * FROM p04_fault_injections WHERE campaign_id = ? ORDER BY created_at",
    ), campaignId).map((row) => this.faultFromRow(row));
  }

  acquireHardwareLease(input: {
    campaignId: string;
    runId: string;
    workerId: string;
    recovery: boolean;
    ttlSeconds?: number;
  }): void {
    const timestamp = now();
    const expiresAt = new Date(Date.now() + (input.ttlSeconds ?? 180) * 1000).toISOString();
    this.transaction(() => {
      const current = this.database.prepare("SELECT * FROM p04_hardware_lease WHERE backend = 'tianyan176'")
        .get() as SqlRow | undefined;
      if (current) {
        const released = optionalText(current, "released_at") !== null;
        const expired = Date.parse(text(current, "expires_at")) <= Date.now();
        const sameRunRecovery = input.recovery && text(current, "run_id") === input.runId;
        if (!released && !expired && !sameRunRecovery && text(current, "worker_id") !== input.workerId) {
          throw new Error("tianyan176 has an active P04 submission lease");
        }
        this.database.prepare("DELETE FROM p04_hardware_lease WHERE backend = 'tianyan176'").run();
      }
      this.database.prepare(
        `INSERT INTO p04_hardware_lease(
          backend, campaign_id, run_id, worker_id, state, acquired_at, heartbeat_at, expires_at
        ) VALUES ('tianyan176', ?, ?, ?, 'PREPARED', ?, ?, ?)`,
      ).run(input.campaignId, input.runId, input.workerId, timestamp, timestamp, expiresAt);
    });
  }

  updateHardwareLease(runId: string, workerId: string, state: string, queryId?: string, ttlSeconds = 180): void {
    const timestamp = now();
    const expiresAt = new Date(Date.now() + ttlSeconds * 1000).toISOString();
    const result = this.database.prepare(
      `UPDATE p04_hardware_lease SET state = ?, query_id = COALESCE(?, query_id),
       heartbeat_at = ?, expires_at = ? WHERE backend = 'tianyan176' AND run_id = ? AND worker_id = ? AND released_at IS NULL`,
    ).run(state, queryId ?? null, timestamp, expiresAt, runId, workerId);
    if (result.changes !== 1) throw new Error("P04 hardware lease update was rejected");
  }

  releaseHardwareLease(runId: string, workerId: string, state: string): void {
    const timestamp = now();
    this.database.prepare(
      `UPDATE p04_hardware_lease SET state = ?, released_at = ?, expires_at = ?, heartbeat_at = ?
       WHERE backend = 'tianyan176' AND run_id = ? AND worker_id = ?`,
    ).run(state, timestamp, timestamp, timestamp, runId, workerId);
  }

  getHardwareLease(campaignId: string): P04CampaignDetail["hardwareLease"] {
    const row = this.database.prepare(
      `SELECT * FROM p04_hardware_lease
       WHERE backend = 'tianyan176' AND campaign_id = ? AND released_at IS NULL`,
    ).get(campaignId) as SqlRow | undefined;
    return row ? {
      backend: "tianyan176",
      campaignId: text(row, "campaign_id"),
      runId: text(row, "run_id"),
      queryId: optionalText(row, "query_id"),
      state: text(row, "state"),
      heartbeatAt: text(row, "heartbeat_at"),
    } : null;
  }

  artifactRows(campaignId: string): Array<{
    runId: string;
    sha256: string;
    relativePath: string;
    relativeOutputPath: string;
    bytes: number;
  }> {
    return rows(this.database.prepare(
      `SELECT p04_artifact_claims.run_id, p04_artifact_claims.artifact_sha256,
       p04_artifact_claims.relative_output_path, artifacts.relative_path, artifacts.bytes
       FROM p04_artifact_claims JOIN p04_runs ON p04_runs.id = p04_artifact_claims.run_id
       JOIN artifacts ON artifacts.sha256 = p04_artifact_claims.artifact_sha256
       WHERE p04_runs.campaign_id = ? ORDER BY p04_artifact_claims.created_at`,
    ), campaignId).map((row) => ({
      runId: text(row, "run_id"),
      sha256: text(row, "artifact_sha256"),
      relativePath: text(row, "relative_path"),
      relativeOutputPath: text(row, "relative_output_path"),
      bytes: numberValue(row, "bytes"),
    }));
  }

  concurrencySnapshot(campaignId: string): JsonObject {
    const journal = this.database.prepare("PRAGMA journal_mode").get() as SqlRow;
    const integrity = this.database.prepare("PRAGMA integrity_check").get() as SqlRow;
    const foreignKeys = this.database.prepare("PRAGMA foreign_keys").get() as SqlRow;
    const eventRows = rows(this.database.prepare(
      `SELECT p04_runs.id AS run_id, COUNT(p04_run_events.sequence) AS event_count,
       COALESCE(MIN(p04_run_events.sequence), 0) AS minimum_sequence,
       COALESCE(MAX(p04_run_events.sequence), 0) AS maximum_sequence,
       COUNT(DISTINCT p04_run_events.sequence) AS distinct_sequences
       FROM p04_runs LEFT JOIN p04_run_events ON p04_run_events.run_id = p04_runs.id
       WHERE p04_runs.campaign_id = ? GROUP BY p04_runs.id ORDER BY p04_runs.id`,
    ), campaignId).map((row) => ({
      runId: text(row, "run_id"),
      eventCount: numberValue(row, "event_count"),
      minimumSequence: numberValue(row, "minimum_sequence"),
      maximumSequence: numberValue(row, "maximum_sequence"),
      distinctSequences: numberValue(row, "distinct_sequences"),
    }));
    const duplicateOutputs = rows(this.database.prepare(
      `SELECT relative_output_path, COUNT(*) AS claim_count
       FROM p04_artifact_claims JOIN p04_runs ON p04_runs.id = p04_artifact_claims.run_id
       WHERE p04_runs.campaign_id = ? GROUP BY relative_output_path HAVING COUNT(*) > 1`,
    ), campaignId).length;
    const activeHardwareLeases = numberValue(
      this.database.prepare(
        `SELECT COUNT(*) AS active_count FROM p04_hardware_lease
         WHERE released_at IS NULL AND expires_at > ?`,
      ).get(now()) as SqlRow,
      "active_count",
    );
    return {
      schemaVersion: "qf.p04.concurrency-snapshot.v1",
      capturedAt: now(),
      journalMode: Object.values(journal)[0] as string,
      integrityCheck: Object.values(integrity)[0] as string,
      foreignKeysEnabled: Object.values(foreignKeys)[0] === 1,
      eventSequences: eventRows,
      duplicateOutputPaths: duplicateOutputs,
      activeHardwareLeases,
      runs: this.listRuns(campaignId),
      tools: this.listTools(campaignId),
      faults: this.listFaults(campaignId),
      artifactClaims: this.artifactRows(campaignId).length,
    } as unknown as JsonObject;
  }

  getDetail(campaign: CampaignSummary): P04CampaignDetail {
    return {
      schemaVersion: CONTRACT_SCHEMA_VERSION,
      campaign,
      runs: this.listRuns(campaign.campaignId),
      tools: this.listTools(campaign.campaignId),
      faults: this.listFaults(campaign.campaignId),
      hardwareLease: this.getHardwareLease(campaign.campaignId),
    };
  }
}
