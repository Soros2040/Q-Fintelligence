// P16 change authorship category: supervisor_infrastructure

import { createHash, randomUUID } from "node:crypto";
import type { DatabaseSync, StatementSync } from "node:sqlite";

import type {
  AgentSessionSummary,
  AgentUiEvent,
  AgentUiEventType,
  ApprovalRequest,
  ArtifactManifest,
  ArtifactSummary,
  ArchiveHistoryResult,
  ConversationSnapshot,
  ConversationState,
  ConversationSummary,
  JsonObject,
  JsonValue,
  MessageRecord,
  OpenHandsWorkspaceSummary,
  ProjectSummary,
  ProjectSourceSummary,
  RenameEvent,
  RenameScope,
  RuntimeMode,
  StepState,
  StepStatus,
  ToolCallClaim,
  ToolCallInboxEntry,
} from "@q-fintelligence/contracts";
import { CONTRACT_SCHEMA_VERSION, toAgentWorkspaceEvent } from "@q-fintelligence/contracts";

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

function integer(row: SqlRow, key: string): number {
  const value = row[key];
  if (typeof value === "number") return value;
  if (typeof value === "bigint") return Number(value);
  throw new Error(`expected integer column ${key}`);
}

function assertSha256(value: string, label: string): void {
  if (!/^[a-f0-9]{64}$/u.test(value)) {
    throw new StateConflictError(`${label} must be a lowercase SHA-256 digest`);
  }
}

function jsonValue(row: SqlRow, key: string): JsonValue | null {
  const value = optionalText(row, key);
  return value === null ? null : JSON.parse(value) as JsonValue;
}

function jsonObject(row: SqlRow, key: string): JsonObject | null {
  const value = jsonValue(row, key);
  if (value === null) return null;
  if (Array.isArray(value) || typeof value !== "object") {
    throw new Error(`expected JSON object column ${key}`);
  }
  return value;
}

function rows(statement: StatementSync, ...parameters: (string | number | null)[]): SqlRow[] {
  return statement.all(...parameters) as SqlRow[];
}

export class NotFoundError extends Error {}
export class StateConflictError extends Error {}

export interface OpenHandsSessionMetadataInput {
  conversationId: string;
  sessionId: string;
  relativeSessionFile: string | null;
  provider: string;
  modelId: string;
  promptVersion: string;
  promptHash: string;
  lifecycleState: string;
  runtimeRevision: string;
  configHash: string;
  recoveryCursor?: number;
}

export interface OpenHandsWorkspaceMetadataInput {
  conversationId: string;
  runtimeSessionId: string;
  manifestHash: string;
  backend: "hardened-docker-agent-server";
  hostLocalWorkspace: false;
  workspaceId: string;
  snapshot: JsonObject;
  imageReference: string;
  imageDigest: string;
  containerId: string;
  serverReused: boolean;
  secretEnvironmentNames: string[];
}

const RENAME_INJECTION_PATTERN =
  /(ignore|override|replace|disregard).{0,40}(system|developer|instruction)|忽略.{0,24}(系统|开发者|指令)|覆盖.{0,24}(提示词|指令)|reveal.{0,30}(key|token|secret)/isu;

function normalizeRenameName(value: string, scope: RenameScope): string {
  const maximum = scope === "PROJECT" ? 120 : 160;
  const normalized = value.normalize("NFKC")
    .replace(/[\u0000-\u001f\u007f]/gu, " ")
    .replace(/\s+/gu, " ")
    .trim();
  if (!normalized || normalized.length > maximum) {
    throw new StateConflictError(`${scope.toLocaleLowerCase()} name must be between 1 and ${maximum} characters`);
  }
  if (RENAME_INJECTION_PATTERN.test(normalized)) {
    throw new StateConflictError("rename content failed the prompt-injection safety gate");
  }
  return normalized;
}

export class WorkspaceRepository {
  constructor(private readonly database: DatabaseSync) {}

  assertConversationWritable(conversationId: string): ConversationSummary {
    const row = this.database.prepare(
      `SELECT conversations.*, agent_sessions.runtime_kind AS session_runtime_kind
       FROM conversations
       LEFT JOIN agent_sessions ON agent_sessions.conversation_id = conversations.id
       WHERE conversations.id = ?`,
    ).get(conversationId) as SqlRow | undefined;
    if (!row) throw new NotFoundError(`conversation ${conversationId} was not found`);
    if (text(row, "mode") === "PI" || optionalText(row, "session_runtime_kind") === "PI") {
      throw new StateConflictError(
        "LEGACY_READ_ONLY: Pi conversations and Pi runtime sessions are immutable historical records",
      );
    }
    return this.conversationFromRow(row);
  }

  close(): void {
    this.database.close();
  }

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

  createProject(name: string, description = ""): ProjectSummary {
    const projectId = `project_${randomUUID()}`;
    const timestamp = now();
    this.database.prepare(
      "INSERT INTO projects(id, name, description, archived, created_at, updated_at) VALUES (?, ?, ?, 0, ?, ?)",
    ).run(projectId, name, description, timestamp, timestamp);
    return this.getProject(projectId);
  }

  getProject(projectId: string): ProjectSummary {
    const row = this.database.prepare("SELECT * FROM projects WHERE id = ?").get(projectId) as SqlRow | undefined;
    if (!row) throw new NotFoundError(`project ${projectId} was not found`);
    return {
      schemaVersion: CONTRACT_SCHEMA_VERSION,
      projectId: text(row, "id"),
      name: text(row, "name"),
      description: text(row, "description"),
      archived: integer(row, "archived") === 1,
      createdAt: text(row, "created_at"),
      updatedAt: text(row, "updated_at"),
    };
  }

  listProjects(includeArchived = false): ProjectSummary[] {
    const statement = this.database.prepare(
      `SELECT * FROM projects ${includeArchived ? "" : "WHERE archived = 0"} ORDER BY updated_at DESC, id`,
    );
    return rows(statement).map((row) => this.getProject(text(row, "id")));
  }

  private renameEventFromRow(row: SqlRow): RenameEvent {
    return {
      schemaVersion: CONTRACT_SCHEMA_VERSION,
      renameEventId: text(row, "id"),
      scope: text(row, "scope") as RenameScope,
      projectId: text(row, "project_id"),
      conversationId: optionalText(row, "conversation_id"),
      oldName: text(row, "old_name"),
      newName: text(row, "new_name"),
      actor: text(row, "actor") as RenameEvent["actor"],
      provider: optionalText(row, "provider"),
      modelId: optionalText(row, "model_id"),
      contextSha256: optionalText(row, "context_sha256"),
      promptTokens: integer(row, "prompt_tokens"),
      completionTokens: integer(row, "completion_tokens"),
      status: text(row, "status") as RenameEvent["status"],
      undoOfEventId: optionalText(row, "undo_of_event_id"),
      createdAt: text(row, "created_at"),
      appliedAt: optionalText(row, "applied_at"),
    };
  }

  getRenameEvent(renameEventId: string): RenameEvent {
    const row = this.database.prepare("SELECT * FROM rename_events WHERE id = ?").get(renameEventId) as SqlRow | undefined;
    if (!row) throw new NotFoundError(`rename event ${renameEventId} was not found`);
    return this.renameEventFromRow(row);
  }

  listRenameEvents(scope: RenameScope, subjectId: string): RenameEvent[] {
    const column = scope === "PROJECT" ? "project_id" : "conversation_id";
    return rows(
      this.database.prepare(
        `SELECT * FROM rename_events WHERE scope = ? AND ${column} = ? ORDER BY created_at DESC, id DESC`,
      ),
      scope,
      subjectId,
    ).map((row) => this.renameEventFromRow(row));
  }

  createRenameSuggestion(input: {
    scope: RenameScope;
    projectId: string;
    conversationId?: string;
    newName: string;
    provider: string;
    modelId: string;
    contextSha256: string;
    promptTokens: number;
    completionTokens: number;
  }): RenameEvent {
    const project = this.getProject(input.projectId);
    const conversation = input.scope === "CONVERSATION"
      ? this.assertConversationWritable(input.conversationId ?? "")
      : null;
    if (conversation && conversation.projectId !== input.projectId) {
      throw new StateConflictError("rename conversation does not belong to the selected project");
    }
    const oldName = conversation?.title ?? project.name;
    const newName = normalizeRenameName(input.newName, input.scope);
    const renameEventId = `rename_${randomUUID()}`;
    const timestamp = now();
    this.transaction(() => {
      this.database.prepare(
        `UPDATE rename_events SET status = 'SUPERSEDED'
         WHERE scope = ? AND project_id = ?
           AND COALESCE(conversation_id, '') = COALESCE(?, '')
           AND status = 'SUGGESTED'`,
      ).run(input.scope, input.projectId, input.conversationId ?? null);
      this.database.prepare(
        `INSERT INTO rename_events(
          id, scope, project_id, conversation_id, old_name, new_name, actor,
          provider, model_id, context_sha256, prompt_tokens, completion_tokens,
          status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'AGENT', ?, ?, ?, ?, ?, 'SUGGESTED', ?)`,
      ).run(
        renameEventId,
        input.scope,
        input.projectId,
        input.conversationId ?? null,
        oldName,
        newName,
        input.provider,
        input.modelId,
        input.contextSha256,
        Math.max(0, Math.trunc(input.promptTokens)),
        Math.max(0, Math.trunc(input.completionTokens)),
        timestamp,
      );
    });
    return this.getRenameEvent(renameEventId);
  }

  private assertRenameCheckpoint(scope: RenameScope, projectId: string, conversationId?: string): void {
    if (scope === "CONVERSATION") {
      const conversation = this.getConversation(conversationId ?? "");
      if (conversation.state === "RUNNING") {
        throw new StateConflictError("rename suggestion is retained until the running conversation reaches a safe checkpoint");
      }
      return;
    }
    const active = this.database.prepare(
      `SELECT COUNT(*) AS count
       FROM conversations
       WHERE project_id = ? AND status = 'RUNNING' AND mode <> 'PI'
         AND NOT EXISTS (
           SELECT 1 FROM agent_sessions
           WHERE agent_sessions.conversation_id = conversations.id
             AND agent_sessions.runtime_kind = 'PI'
         )`,
    ).get(projectId) as SqlRow;
    if (integer(active, "count") > 0) {
      throw new StateConflictError("project rename suggestion is retained until all conversations reach a safe checkpoint");
    }
  }

  applyRename(input: {
    scope: RenameScope;
    subjectId: string;
    name: string;
    actor: "HUMAN" | "AGENT";
    suggestionEventId?: string;
  }): RenameEvent {
    const conversation = input.scope === "CONVERSATION"
      ? this.assertConversationWritable(input.subjectId)
      : null;
    const project = conversation ? this.getProject(conversation.projectId) : this.getProject(input.subjectId);
    this.assertRenameCheckpoint(input.scope, project.projectId, conversation?.conversationId);
    const oldName = conversation?.title ?? project.name;
    const newName = normalizeRenameName(input.name, input.scope);
    if (newName === oldName) throw new StateConflictError("new name must differ from the current name");
    const timestamp = now();
    let renameEventId = `rename_${randomUUID()}`;
    this.transaction(() => {
      if (input.actor === "AGENT") {
        if (!input.suggestionEventId) throw new StateConflictError("agent rename requires a retained suggestion event");
        const suggestion = this.getRenameEvent(input.suggestionEventId);
        if (
          suggestion.status !== "SUGGESTED"
          || suggestion.scope !== input.scope
          || suggestion.projectId !== project.projectId
          || suggestion.conversationId !== (conversation?.conversationId ?? null)
          || suggestion.newName !== newName
        ) {
          throw new StateConflictError("agent rename suggestion no longer matches the selected subject");
        }
        renameEventId = suggestion.renameEventId;
        this.database.prepare(
          `UPDATE rename_events
           SET old_name = ?, new_name = ?, status = 'APPLIED', applied_at = ?
           WHERE id = ?`,
        ).run(oldName, newName, timestamp, renameEventId);
      } else {
        this.database.prepare(
          `INSERT INTO rename_events(
            id, scope, project_id, conversation_id, old_name, new_name, actor,
            prompt_tokens, completion_tokens, status, created_at, applied_at
          ) VALUES (?, ?, ?, ?, ?, ?, 'HUMAN', 0, 0, 'APPLIED', ?, ?)`,
        ).run(
          renameEventId,
          input.scope,
          project.projectId,
          conversation?.conversationId ?? null,
          oldName,
          newName,
          timestamp,
          timestamp,
        );
      }
      if (conversation) {
        this.database.prepare(
          "UPDATE conversations SET title = ?, last_activity_at = ? WHERE id = ?",
        ).run(newName, timestamp, conversation.conversationId);
      } else {
        this.database.prepare(
          "UPDATE projects SET name = ?, updated_at = ? WHERE id = ?",
        ).run(newName, timestamp, project.projectId);
      }
      this.database.prepare(
        `UPDATE rename_events SET status = 'SUPERSEDED'
         WHERE scope = ? AND project_id = ?
           AND COALESCE(conversation_id, '') = COALESCE(?, '')
           AND status = 'SUGGESTED' AND id <> ?`,
      ).run(input.scope, project.projectId, conversation?.conversationId ?? null, renameEventId);
    });
    return this.getRenameEvent(renameEventId);
  }

  undoRename(scope: RenameScope, subjectId: string): RenameEvent {
    if (scope === "CONVERSATION") this.assertConversationWritable(subjectId);
    const latest = this.listRenameEvents(scope, subjectId).find((event) => event.status === "APPLIED");
    if (!latest) throw new StateConflictError("there is no applied rename to undo");
    const currentName = scope === "PROJECT"
      ? this.getProject(subjectId).name
      : this.getConversation(subjectId).title;
    if (currentName !== latest.newName) {
      throw new StateConflictError("the latest rename cannot be undone after a newer name change");
    }
    const reverted = this.applyRename({
      scope,
      subjectId,
      name: latest.oldName,
      actor: "HUMAN",
    });
    this.database.prepare(
      "UPDATE rename_events SET undo_of_event_id = ? WHERE id = ?",
    ).run(latest.renameEventId, reverted.renameEventId);
    return this.getRenameEvent(reverted.renameEventId);
  }

  getRenameContext(scope: RenameScope, subjectId: string): {
    projectId: string;
    conversationId: string | null;
    currentName: string;
    summaries: string[];
    userQuestions: string[];
    keyTopics: string[];
    contextSha256: string;
  } {
    const conversations = scope === "PROJECT"
      ? this.listConversations(subjectId)
      : [this.getConversation(subjectId)];
    const projectId = scope === "PROJECT" ? subjectId : conversations[0]!.projectId;
    const currentName = scope === "PROJECT" ? this.getProject(subjectId).name : conversations[0]!.title;
    const messageRows = conversations.flatMap((conversation) => rows(
      this.database.prepare(
        `SELECT role, content FROM messages
         WHERE conversation_id = ?
         ORDER BY created_at DESC, id DESC LIMIT 16`,
      ),
      conversation.conversationId,
    ).map((row) => ({ role: text(row, "role"), content: text(row, "content") })));
    const clean = (value: string) => value.normalize("NFKC")
      .replace(/authorization\s*:\s*bearer\s+\S+/giu, "Authorization: Bearer [REDACTED]")
      .replace(/(["']?(?:api[_ -]?key|access[_ -]?token|connection[_ -]?key)["']?\s*[:=]\s*["']?)[^"'\s,;}]+/giu, "$1[REDACTED]")
      .replace(/\s+/gu, " ")
      .trim()
      .slice(0, 360);
    const userQuestions = messageRows.filter((row) => row.role === "USER").slice(0, 12).map((row) => clean(row.content));
    const summaries = [
      ...conversations.slice(0, 12).map((conversation) => clean(conversation.title)),
      ...messageRows.filter((row) => row.role === "ASSISTANT").slice(0, 8).map((row) => clean(row.content)),
    ].filter(Boolean);
    const tokens = [...userQuestions, ...summaries]
      .flatMap((value) => value.match(/[\p{Script=Han}]{2,8}|[A-Za-z][A-Za-z0-9_-]{2,24}/gu) ?? [])
      .map((value) => value.toLocaleLowerCase())
      .filter((value) => !new Set(["the", "and", "with", "this", "that", "任务", "项目", "对话", "完成", "进行"]).has(value));
    const frequencies = new Map<string, number>();
    for (const token of tokens) frequencies.set(token, (frequencies.get(token) ?? 0) + 1);
    const keyTopics = [...frequencies.entries()]
      .sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]))
      .slice(0, 12)
      .map(([token]) => token);
    const context = {
      projectId,
      conversationId: scope === "CONVERSATION" ? subjectId : null,
      currentName,
      summaries: summaries.slice(0, 12),
      userQuestions,
      keyTopics,
    };
    return {
      ...context,
      contextSha256: createHash("sha256").update(JSON.stringify(context)).digest("hex"),
    };
  }

  listProjectSourcesWithParseResult(projectId: string): Array<ProjectSourceSummary & {
    parseResult: JsonObject;
    relativePath: string;
  }> {
    this.getProject(projectId);
    return rows(
      this.database.prepare(
        "SELECT * FROM project_sources WHERE project_id = ? AND archived_at IS NULL ORDER BY created_at DESC, id",
      ),
      projectId,
    ).map((row) => ({
      sourceId: text(row, "id"),
      projectId: text(row, "project_id"),
      addedFromConversationId: optionalText(row, "added_from_conversation_id"),
      fileName: text(row, "file_name"),
      extension: text(row, "extension"),
      declaredMediaType: text(row, "declared_media_type"),
      detectedMediaType: text(row, "detected_media_type"),
      byteSize: integer(row, "byte_size"),
      sha256: text(row, "sha256"),
      parser: text(row, "parser"),
      riskLevel: text(row, "risk_level") as ProjectSourceSummary["riskLevel"],
      quarantined: integer(row, "quarantined") === 1,
      createdAt: text(row, "created_at"),
      updatedAt: text(row, "updated_at"),
      parseResult: JSON.parse(text(row, "parse_result_json")) as JsonObject,
      relativePath: text(row, "relative_path"),
    }));
  }

  registerGeneratedProjectSource(input: {
    projectId: string;
    conversationId: string;
    fileName: string;
    extension: string;
    mediaType: string;
    byteSize: number;
    sha256: string;
    parser: string;
    parseResult: JsonObject;
  }): ProjectSourceSummary {
    const conversation = this.assertConversationWritable(input.conversationId);
    if (conversation.projectId !== input.projectId) {
      throw new StateConflictError("generated source conversation must belong to the selected project");
    }
    const sourceId = `project_source_${randomUUID()}`;
    const timestamp = now();
    this.database.prepare(
      `INSERT INTO project_sources(
        id, project_id, added_from_conversation_id, file_name, extension,
        declared_media_type, detected_media_type, byte_size, sha256, relative_path,
        parser, risk_level, quarantined, parse_result_json, archived_at, created_at, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'LOW', 0, ?, NULL, ?, ?)
      ON CONFLICT(project_id, sha256, file_name) DO UPDATE SET
        added_from_conversation_id = excluded.added_from_conversation_id,
        archived_at = NULL,
        updated_at = excluded.updated_at`,
    ).run(
      sourceId,
      input.projectId,
      input.conversationId,
      input.fileName,
      input.extension,
      input.mediaType,
      input.mediaType,
      input.byteSize,
      input.sha256,
      `virtual:artifact/${input.projectId}/${input.sha256}/${input.fileName}`,
      input.parser,
      JSON.stringify(input.parseResult),
      timestamp,
      timestamp,
    );
    const row = this.database.prepare(
      "SELECT * FROM project_sources WHERE project_id = ? AND sha256 = ? AND file_name = ?",
    ).get(input.projectId, input.sha256, input.fileName) as SqlRow;
    return {
      sourceId: text(row, "id"),
      projectId: text(row, "project_id"),
      addedFromConversationId: optionalText(row, "added_from_conversation_id"),
      fileName: text(row, "file_name"),
      extension: text(row, "extension"),
      declaredMediaType: text(row, "declared_media_type"),
      detectedMediaType: text(row, "detected_media_type"),
      byteSize: integer(row, "byte_size"),
      sha256: text(row, "sha256"),
      parser: text(row, "parser"),
      riskLevel: text(row, "risk_level") as ProjectSourceSummary["riskLevel"],
      quarantined: integer(row, "quarantined") === 1,
      createdAt: text(row, "created_at"),
      updatedAt: text(row, "updated_at"),
    };
  }

  getLatestP15Calibration(projectId: string): {
    snapshotId: string;
    normalizedSha256: string;
    rawSha256: string;
    retrievedAt: string;
    calibrationAt: string | null;
    completeness: "COMPLETE" | "PARTIAL" | "UNAVAILABLE";
  } | null {
    const row = this.database.prepare(
      `SELECT * FROM p15_calibration_snapshots
       WHERE project_id = ? ORDER BY created_at DESC, id DESC LIMIT 1`,
    ).get(projectId) as SqlRow | undefined;
    if (!row) return null;
    return {
      snapshotId: text(row, "id"),
      normalizedSha256: text(row, "normalized_sha256"),
      rawSha256: text(row, "raw_sha256"),
      retrievedAt: text(row, "retrieved_at"),
      calibrationAt: optionalText(row, "calibration_at"),
      completeness: text(row, "completeness") as "COMPLETE" | "PARTIAL" | "UNAVAILABLE",
    };
  }

  getP15Calibration(snapshotId: string): {
    snapshotId: string;
    projectId: string;
    normalizedSha256: string;
    rawSha256: string;
    retrievedAt: string;
    calibrationAt: string | null;
    completeness: "COMPLETE" | "PARTIAL" | "UNAVAILABLE";
  } | null {
    const row = this.database.prepare(
      "SELECT * FROM p15_calibration_snapshots WHERE id = ?",
    ).get(snapshotId) as SqlRow | undefined;
    if (!row) return null;
    return {
      snapshotId: text(row, "id"),
      projectId: text(row, "project_id"),
      normalizedSha256: text(row, "normalized_sha256"),
      rawSha256: text(row, "raw_sha256"),
      retrievedAt: text(row, "retrieved_at"),
      calibrationAt: optionalText(row, "calibration_at"),
      completeness: text(row, "completeness") as "COMPLETE" | "PARTIAL" | "UNAVAILABLE",
    };
  }

  registerP15Calibration(input: {
    projectId: string;
    conversationId: string;
    machineStatus: string;
    retrievedAt: string;
    calibrationAt: string | null;
    cqlibVersion: string;
    rawSha256: string;
    normalizedSha256: string;
    manifestSha256: string;
    diffSha256: string;
    csvSha256: string;
    topologySha256: string;
    completeness: "COMPLETE" | "PARTIAL" | "UNAVAILABLE";
    missingFields: string[];
    warnings: string[];
    sourceIds: string[];
    previousSnapshotId: string | null;
  }): string {
    const existing = this.database.prepare(
      "SELECT id FROM p15_calibration_snapshots WHERE project_id = ? AND raw_sha256 = ?",
    ).get(input.projectId, input.rawSha256) as SqlRow | undefined;
    if (existing) return text(existing, "id");
    const snapshotId = `calibration_${randomUUID()}`;
    this.database.prepare(
      `INSERT INTO p15_calibration_snapshots(
        id, project_id, conversation_id, machine_name, machine_status, retrieved_at,
        calibration_at, cqlib_version, raw_sha256, normalized_sha256, manifest_sha256,
        diff_sha256, csv_sha256, topology_sha256, completeness, missing_fields_json,
        warnings_json, source_ids_json, previous_snapshot_id, created_at
      ) VALUES (?, ?, ?, 'tianyan176', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    ).run(
      snapshotId,
      input.projectId,
      input.conversationId,
      input.machineStatus,
      input.retrievedAt,
      input.calibrationAt,
      input.cqlibVersion,
      input.rawSha256,
      input.normalizedSha256,
      input.manifestSha256,
      input.diffSha256,
      input.csvSha256,
      input.topologySha256,
      input.completeness,
      JSON.stringify(input.missingFields),
      JSON.stringify(input.warnings),
      JSON.stringify(input.sourceIds),
      input.previousSnapshotId,
      now(),
    );
    return snapshotId;
  }

  updateProject(projectId: string, update: { name?: string; description?: string; archived?: boolean }): ProjectSummary {
    const current = this.getProject(projectId);
    const archived = update.archived ?? current.archived;
    const timestamp = now();
    this.transaction(() => {
      this.database.prepare(
        "UPDATE projects SET name = ?, description = ?, archived = ?, updated_at = ? WHERE id = ?",
      ).run(
        update.name ?? current.name,
        update.description ?? current.description,
        archived ? 1 : 0,
        timestamp,
        projectId,
      );
      if (update.archived !== undefined) {
        this.database.prepare(
          `UPDATE conversations SET archived_at = ?
           WHERE project_id = ? AND mode <> 'PI'
             AND NOT EXISTS (
               SELECT 1 FROM agent_sessions
               WHERE agent_sessions.conversation_id = conversations.id
                 AND agent_sessions.runtime_kind = 'PI'
             )`,
        ).run(archived ? timestamp : null, projectId);
      }
    });
    return this.getProject(projectId);
  }

  archiveAllProjects(): ArchiveHistoryResult {
    const activeRuns = this.database.prepare(
      `SELECT COUNT(*) AS count
       FROM conversations
       JOIN projects ON projects.id = conversations.project_id
       WHERE projects.archived = 0 AND conversations.status = 'RUNNING'
         AND conversations.mode <> 'PI'
         AND NOT EXISTS (
           SELECT 1 FROM agent_sessions
           WHERE agent_sessions.conversation_id = conversations.id
             AND agent_sessions.runtime_kind = 'PI'
         )`,
    ).get() as SqlRow;
    if (integer(activeRuns, "count") > 0) {
      throw new StateConflictError("running conversations must finish or be stopped before history can be archived");
    }
    const archivedAt = now();
    return this.transaction(() => {
      const conversations = this.database.prepare(
        `UPDATE conversations
         SET archived_at = ?
         WHERE archived_at IS NULL
           AND mode <> 'PI'
           AND NOT EXISTS (
             SELECT 1 FROM agent_sessions
             WHERE agent_sessions.conversation_id = conversations.id
               AND agent_sessions.runtime_kind = 'PI'
           )
           AND project_id IN (SELECT id FROM projects WHERE archived = 0)`,
      ).run(archivedAt);
      const projects = this.database.prepare(
        "UPDATE projects SET archived = 1, updated_at = ? WHERE archived = 0",
      ).run(archivedAt);
      return {
        projectsArchived: Number(projects.changes),
        conversationsArchived: Number(conversations.changes),
        archivedAt,
      };
    });
  }

  createConversation(input: {
    projectId: string;
    title: string;
    mode: RuntimeMode;
    provider?: string;
    modelId?: string;
  }): ConversationSummary {
    const project = this.getProject(input.projectId);
    if (project.archived) throw new StateConflictError("cannot create a conversation in an archived project");
    if (input.mode === "PI") {
      throw new StateConflictError("PI conversations are historical read-only records and cannot be created");
    }
    if (input.mode === "OPENHANDS" && (!input.provider || !input.modelId)) {
      throw new StateConflictError("OpenHands conversations require an explicit provider and modelId");
    }
    const conversationId = `conversation_${randomUUID()}`;
    const taskId = `task_${randomUUID()}`;
    const runId = `run_${randomUUID()}`;
    const timestamp = now();
    const provider = input.mode === "MOCK" ? "mock" : input.provider!;
    const modelId = input.mode === "MOCK" ? "qf-mock-v1" : input.modelId!;

    this.transaction(() => {
      this.database.prepare(
        "INSERT INTO tasks(id, selected_provider, selected_model, state, created_at, updated_at) VALUES (?, ?, ?, 'DRAFT', ?, ?)",
      ).run(taskId, provider, modelId, timestamp, timestamp);
      this.database.prepare(
        "INSERT INTO runs(id, task_id, iteration, code_revision, environment_hash, status, started_at) VALUES (?, ?, 0, 'working-tree', 'local-runtime', 'IDLE', ?)",
      ).run(runId, taskId, timestamp);
      this.database.prepare(
        "INSERT INTO conversations(id, project_id, task_id, title, mode, status, provider, model_id, last_activity_at, created_at) VALUES (?, ?, ?, ?, ?, 'DRAFT', ?, ?, ?, ?)",
      ).run(conversationId, input.projectId, taskId, input.title, input.mode, provider, modelId, timestamp, timestamp);
    });
    return this.getConversation(conversationId);
  }

  private conversationFromRow(row: SqlRow): ConversationSummary {
    return {
      schemaVersion: CONTRACT_SCHEMA_VERSION,
      conversationId: text(row, "id"),
      projectId: text(row, "project_id"),
      taskId: text(row, "task_id"),
      title: text(row, "title"),
      mode: text(row, "mode") as RuntimeMode,
      state: text(row, "status") as ConversationState,
      provider: optionalText(row, "provider"),
      modelId: optionalText(row, "model_id"),
      archived: optionalText(row, "archived_at") !== null,
      lastActivityAt: text(row, "last_activity_at"),
      createdAt: text(row, "created_at"),
    };
  }

  getConversation(conversationId: string): ConversationSummary {
    const row = this.database.prepare("SELECT * FROM conversations WHERE id = ?").get(conversationId) as SqlRow | undefined;
    if (!row) throw new NotFoundError(`conversation ${conversationId} was not found`);
    return this.conversationFromRow(row);
  }

  getConversationByTaskId(taskId: string): ConversationSummary {
    const row = this.database.prepare("SELECT * FROM conversations WHERE task_id = ?").get(taskId) as SqlRow | undefined;
    if (!row) throw new NotFoundError(`conversation for task ${taskId} was not found`);
    return this.conversationFromRow(row);
  }

  listConversations(projectId: string, includeArchived = false): ConversationSummary[] {
    const project = this.getProject(projectId);
    if (project.archived && !includeArchived) return [];
    return rows(
      this.database.prepare(
        `SELECT * FROM conversations
         WHERE project_id = ? ${includeArchived ? "" : "AND archived_at IS NULL"}
         ORDER BY last_activity_at DESC, id`,
      ),
      projectId,
    ).map((row) => this.conversationFromRow(row));
  }

  updateConversationModel(conversationId: string, provider: string, modelId: string): ConversationSummary {
    const conversation = this.assertConversationWritable(conversationId);
    if (conversation.mode !== "OPENHANDS") {
      throw new StateConflictError("only OpenHands conversations can switch models");
    }
    if (conversation.state === "RUNNING") throw new StateConflictError("cannot switch models while a prompt is running");
    const timestamp = now();
    this.transaction(() => {
      this.database.prepare(
        "UPDATE conversations SET provider = ?, model_id = ?, last_activity_at = ? WHERE id = ?",
      ).run(provider, modelId, timestamp, conversationId);
      this.database.prepare(
        "UPDATE tasks SET selected_provider = ?, selected_model = ?, updated_at = ? WHERE id = ?",
      ).run(provider, modelId, timestamp, conversation.taskId);
      this.database.prepare(
        "UPDATE projects SET updated_at = ? WHERE id = ?",
      ).run(timestamp, conversation.projectId);
    });
    return this.getConversation(conversationId);
  }

  setConversationState(conversationId: string, state: ConversationState): void {
    this.assertConversationWritable(conversationId);
    const timestamp = now();
    const result = this.database.prepare(
      "UPDATE conversations SET status = ?, last_activity_at = ? WHERE id = ?",
    ).run(state, timestamp, conversationId);
    if (result.changes !== 1) throw new NotFoundError(`conversation ${conversationId} was not found`);
    this.database.prepare(
      "UPDATE tasks SET state = ?, updated_at = ? WHERE id = (SELECT task_id FROM conversations WHERE id = ?)",
    ).run(state === "COMPLETED" ? "VALIDATED" : state === "ABORTED" ? "PAUSED" : state, timestamp, conversationId);
    this.database.prepare(
      "UPDATE projects SET updated_at = ? WHERE id = (SELECT project_id FROM conversations WHERE id = ?)",
    ).run(timestamp, conversationId);
  }

  getRunId(conversationId: string): string {
    const row = this.database.prepare(
      "SELECT runs.id FROM runs JOIN conversations ON conversations.task_id = runs.task_id WHERE conversations.id = ? ORDER BY runs.started_at DESC LIMIT 1",
    ).get(conversationId) as SqlRow | undefined;
    if (!row) throw new NotFoundError(`run for conversation ${conversationId} was not found`);
    return text(row, "id");
  }

  addMessage(input: {
    conversationId: string;
    role: MessageRecord["role"];
    content: string;
    provider?: string | null;
    modelId?: string | null;
    piMessageId?: string | null;
  }): MessageRecord {
    const conversation = this.assertConversationWritable(input.conversationId);
    const messageId = `message_${randomUUID()}`;
    const timestamp = now();
    this.database.prepare(
      "INSERT INTO messages(id, conversation_id, role, content, provider, model_id, pi_message_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
    ).run(
      messageId,
      input.conversationId,
      input.role,
      input.content,
      input.provider ?? conversation.provider,
      input.modelId ?? conversation.modelId,
      input.piMessageId ?? null,
      timestamp,
    );
    return this.getMessage(messageId);
  }

  private getMessage(messageId: string): MessageRecord {
    const row = this.database.prepare("SELECT * FROM messages WHERE id = ?").get(messageId) as SqlRow | undefined;
    if (!row) throw new NotFoundError(`message ${messageId} was not found`);
    return {
      schemaVersion: CONTRACT_SCHEMA_VERSION,
      messageId: text(row, "id"),
      conversationId: text(row, "conversation_id"),
      role: text(row, "role") as MessageRecord["role"],
      content: text(row, "content"),
      provider: optionalText(row, "provider"),
      modelId: optionalText(row, "model_id"),
      piMessageId: optionalText(row, "pi_message_id"),
      createdAt: text(row, "created_at"),
    };
  }

  listMessages(conversationId: string): MessageRecord[] {
    return rows(
      this.database.prepare("SELECT id FROM messages WHERE conversation_id = ? ORDER BY created_at, id"),
      conversationId,
    ).map((row) => this.getMessage(text(row, "id")));
  }

  appendEvent(input: {
    conversationId: string;
    type: AgentUiEventType;
    payload: JsonObject;
    runId?: string | null;
  }): AgentUiEvent {
    const conversation = this.assertConversationWritable(input.conversationId);
    const runId = input.runId === undefined ? this.getRunId(input.conversationId) : input.runId;
    const createdAt = now();
    const result = this.database.prepare(
      "INSERT INTO events(task_id, run_id, event_type, payload_json, created_at, conversation_id) VALUES (?, ?, ?, ?, ?, ?)",
    ).run(conversation.taskId, runId, input.type, JSON.stringify(input.payload), createdAt, input.conversationId);
    const event: AgentUiEvent = {
      schemaVersion: CONTRACT_SCHEMA_VERSION,
      eventId: `event_${result.lastInsertRowid}`,
      conversationId: input.conversationId,
      taskId: conversation.taskId,
      runId,
      sequence: Number(result.lastInsertRowid),
      persistent: true,
      type: input.type,
      payload: input.payload,
      createdAt,
    };
    return { ...event, workspace: toAgentWorkspaceEvent(event) };
  }

  listEventsAfter(conversationId: string, after: number): AgentUiEvent[] {
    const conversation = this.getConversation(conversationId);
    return rows(
      this.database.prepare("SELECT * FROM events WHERE conversation_id = ? AND sequence > ? ORDER BY sequence"),
      conversationId,
      after,
    ).map((row) => this.eventFromRow(conversation, row));
  }

  listRecentEvents(conversationId: string, limit = 30): AgentUiEvent[] {
    const conversation = this.getConversation(conversationId);
    const recent = rows(
      this.database.prepare(
        "SELECT * FROM (SELECT * FROM events WHERE conversation_id = ? ORDER BY sequence DESC LIMIT ?) ORDER BY sequence",
      ),
      conversationId,
      limit,
    );
    return recent.map((row) => this.eventFromRow(conversation, row));
  }

  private eventFromRow(conversation: ConversationSummary, row: SqlRow): AgentUiEvent {
    const event: AgentUiEvent = {
      schemaVersion: CONTRACT_SCHEMA_VERSION,
      eventId: `event_${integer(row, "sequence")}`,
      conversationId: conversation.conversationId,
      taskId: conversation.taskId,
      runId: optionalText(row, "run_id"),
      sequence: integer(row, "sequence"),
      persistent: true,
      type: text(row, "event_type") as AgentUiEventType,
      payload: JSON.parse(text(row, "payload_json")) as JsonObject,
      createdAt: text(row, "created_at"),
    };
    return { ...event, workspace: toAgentWorkspaceEvent(event) };
  }

  latestSequence(conversationId: string): number {
    const row = this.database.prepare(
      "SELECT COALESCE(MAX(sequence), 0) AS latest FROM events WHERE conversation_id = ?",
    ).get(conversationId) as SqlRow;
    return integer(row, "latest");
  }

  upsertStep(input: {
    conversationId: string;
    name: string;
    state: StepState;
    errorCategory?: string | null;
  }): StepStatus {
    this.assertConversationWritable(input.conversationId);
    const runId = this.getRunId(input.conversationId);
    const stepId = `step_${input.conversationId}_${input.name.replace(/[^a-z0-9]+/giu, "_").toLowerCase()}`;
    const existing = this.database.prepare("SELECT id FROM steps WHERE id = ?").get(stepId);
    const timestamp = now();
    if (existing) {
      this.database.prepare(
        "UPDATE steps SET status = ?, error_category = ?, started_at = COALESCE(started_at, ?), finished_at = ? WHERE id = ?",
      ).run(
        input.state,
        input.errorCategory ?? null,
        input.state === "PENDING" ? null : timestamp,
        ["COMPLETED", "FAILED", "BLOCKED", "ABORTED"].includes(input.state) ? timestamp : null,
        stepId,
      );
    } else {
      this.database.prepare(
        "INSERT INTO steps(id, run_id, name, idempotency_key, status, error_category, started_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
      ).run(
        stepId,
        runId,
        input.name,
        `${input.conversationId}:${input.name}:v1`,
        input.state,
        input.errorCategory ?? null,
        input.state === "PENDING" ? null : timestamp,
        ["COMPLETED", "FAILED", "BLOCKED", "ABORTED"].includes(input.state) ? timestamp : null,
      );
    }
    return this.listSteps(input.conversationId).find((step) => step.stepId === stepId)!;
  }

  listSteps(conversationId: string): StepStatus[] {
    const runId = this.getRunId(conversationId);
    return rows(this.database.prepare("SELECT * FROM steps WHERE run_id = ? ORDER BY rowid"), runId).map((row) => ({
      schemaVersion: CONTRACT_SCHEMA_VERSION,
      stepId: text(row, "id"),
      runId,
      name: text(row, "name"),
      status: text(row, "status") as StepState,
      errorCategory: optionalText(row, "error_category"),
      startedAt: optionalText(row, "started_at"),
      finishedAt: optionalText(row, "finished_at"),
    }));
  }

  registerArtifact(conversationId: string, manifest: ArtifactManifest): ArtifactSummary {
    this.assertConversationWritable(conversationId);
    this.transaction(() => {
      this.database.prepare(
        "INSERT OR IGNORE INTO artifacts(sha256, media_type, bytes, relative_path, producer, created_at) VALUES (?, ?, ?, ?, ?, ?)",
      ).run(manifest.sha256, manifest.mediaType, manifest.bytes, manifest.relativePath, manifest.producer, manifest.createdAt);
      this.database.prepare(
        "INSERT OR IGNORE INTO conversation_artifacts(conversation_id, sha256, linked_at) VALUES (?, ?, ?)",
      ).run(conversationId, manifest.sha256, now());
      for (const parent of manifest.parentHashes) {
        const present = this.database.prepare("SELECT 1 FROM artifacts WHERE sha256 = ?").get(parent);
        if (present) {
          this.database.prepare(
            "INSERT OR IGNORE INTO artifact_edges(child_sha256, parent_sha256) VALUES (?, ?)",
          ).run(manifest.sha256, parent);
        }
      }
    });
    return this.getArtifact(manifest.sha256);
  }

  getArtifact(sha256: string): ArtifactSummary {
    const row = this.database.prepare("SELECT * FROM artifacts WHERE sha256 = ?").get(sha256) as SqlRow | undefined;
    if (!row) throw new NotFoundError(`artifact ${sha256} was not found`);
    const parents = rows(
      this.database.prepare("SELECT parent_sha256 FROM artifact_edges WHERE child_sha256 = ? ORDER BY parent_sha256"),
      sha256,
    ).map((parent) => text(parent, "parent_sha256"));
    const mediaType = text(row, "media_type");
    const bytes = integer(row, "bytes");
    return {
      schemaVersion: CONTRACT_SCHEMA_VERSION,
      sha256,
      mediaType,
      bytes,
      producer: text(row, "producer"),
      createdAt: text(row, "created_at"),
      parentHashes: parents,
      previewable: (
        mediaType === "application/json"
        || mediaType.endsWith("+json")
        || ["text/plain", "text/markdown", "text/csv", "text/vnd.qf.qcis", "text/vnd.qcis", "image/svg+xml"].includes(mediaType)
      ) && bytes <= 2_000_000,
    };
  }

  getArtifactManifest(sha256: string): ArtifactManifest {
    const summary = this.getArtifact(sha256);
    const row = this.database.prepare("SELECT relative_path FROM artifacts WHERE sha256 = ?").get(sha256) as SqlRow;
    return { ...summary, relativePath: text(row, "relative_path"), parentHashes: summary.parentHashes };
  }

  listArtifacts(conversationId: string): ArtifactSummary[] {
    return rows(
      this.database.prepare("SELECT sha256 FROM conversation_artifacts WHERE conversation_id = ? ORDER BY linked_at, sha256"),
      conversationId,
    ).map((row) => this.getArtifact(text(row, "sha256")));
  }

  createApprovalRequest(input: {
    conversationId: string;
    action: ApprovalRequest["action"];
    subjectHash: string;
    rationale: string;
    requestedBy?: ApprovalRequest["requestedBy"];
  }): ApprovalRequest {
    const conversation = this.assertConversationWritable(input.conversationId);
    const approvalId = `approval_${randomUUID()}`;
    const timestamp = now();
    this.database.prepare(
      "INSERT INTO approval_requests(id, task_id, conversation_id, action, subject_hash, rationale, status, requested_by, requested_at) VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)",
    ).run(
      approvalId,
      conversation.taskId,
      input.conversationId,
      input.action,
      input.subjectHash,
      input.rationale,
      input.requestedBy ?? "AGENT",
      timestamp,
    );
    return this.getApproval(approvalId);
  }

  private approvalFromRow(row: SqlRow): ApprovalRequest {
    return {
      schemaVersion: CONTRACT_SCHEMA_VERSION,
      approvalId: text(row, "id"),
      taskId: text(row, "task_id"),
      conversationId: text(row, "conversation_id"),
      action: text(row, "action") as ApprovalRequest["action"],
      subjectHash: text(row, "subject_hash"),
      rationale: text(row, "rationale"),
      status: text(row, "status") as ApprovalRequest["status"],
      requestedBy: text(row, "requested_by") as ApprovalRequest["requestedBy"],
      requestedAt: text(row, "requested_at"),
      decidedAt: optionalText(row, "decided_at"),
    };
  }

  getApproval(approvalId: string): ApprovalRequest {
    const row = this.database.prepare("SELECT * FROM approval_requests WHERE id = ?").get(approvalId) as SqlRow | undefined;
    if (!row) throw new NotFoundError(`approval ${approvalId} was not found`);
    return this.approvalFromRow(row);
  }

  listApprovals(conversationId: string): ApprovalRequest[] {
    return rows(
      this.database.prepare("SELECT * FROM approval_requests WHERE conversation_id = ? ORDER BY requested_at, id"),
      conversationId,
    ).map((row) => this.approvalFromRow(row));
  }

  decideApproval(approvalId: string, decision: "APPROVE" | "REJECT", actor: "HUMAN"): ApprovalRequest {
    if (actor !== "HUMAN") throw new StateConflictError("only a human actor may decide approvals");
    const request = this.getApproval(approvalId);
    this.assertConversationWritable(request.conversationId);
    if (request.status !== "PENDING") throw new StateConflictError("approval is already resolved");
    const timestamp = now();
    this.transaction(() => {
      this.database.prepare("UPDATE approval_requests SET status = ?, decided_at = ? WHERE id = ?").run(
        decision === "APPROVE" ? "APPROVED" : "REJECTED",
        timestamp,
        approvalId,
      );
      if (decision === "APPROVE") {
        this.database.prepare(
          "INSERT INTO approvals(id, task_id, action, subject_hash, approved_by, approved_at) VALUES (?, ?, ?, ?, 'HUMAN', ?)",
        ).run(`decision_${randomUUID()}`, request.taskId, request.action, request.subjectHash, timestamp);
      }
    });
    return this.getApproval(approvalId);
  }

  upsertSession(input: {
    conversationId: string;
    sessionId: string;
    relativeSessionFile: string | null;
    provider: string;
    modelId: string;
    promptVersion: string;
    promptHash: string;
    lifecycleState: string;
    runtimeKind?: RuntimeMode;
    runtimeRevision?: string;
    configHash?: string;
    recoveryCursor?: number;
  }): AgentSessionSummary {
    const conversation = this.assertConversationWritable(input.conversationId);
    const existing = this.getSession(input.conversationId);
    const runtimeKind = input.runtimeKind ?? existing?.runtimeKind ?? conversation.mode;
    if (runtimeKind !== conversation.mode) {
      throw new StateConflictError("session runtime kind must match its conversation runtime mode");
    }
    if (runtimeKind === "OPENHANDS" && existing === null && (!input.runtimeRevision || !input.configHash)) {
      throw new StateConflictError("new OpenHands sessions require an explicit runtime revision and config hash");
    }
    const runtimeRevision = input.runtimeRevision ?? existing?.runtimeRevision
      ?? (runtimeKind === "MOCK" ? "qf-mock-v1" : runtimeKind === "PI" ? "pi-agent-session-legacy" : "openhands-unknown");
    const configHash = input.configHash ?? existing?.configHash ?? createHash("sha256").update(JSON.stringify({
      runtimeKind,
      provider: input.provider,
      modelId: input.modelId,
      promptVersion: input.promptVersion,
      promptHash: input.promptHash,
    })).digest("hex");
    const recoveryCursor = input.recoveryCursor ?? existing?.recoveryCursor ?? 0;
    assertSha256(input.promptHash, "promptHash");
    assertSha256(configHash, "configHash");
    if (!runtimeRevision.trim()) throw new StateConflictError("runtimeRevision must not be empty");
    if (!Number.isSafeInteger(recoveryCursor) || recoveryCursor < 0) {
      throw new StateConflictError("recoveryCursor must be a non-negative safe integer");
    }
    const timestamp = now();
    this.database.prepare(
      `INSERT INTO agent_sessions(
         conversation_id, session_id, relative_session_file, provider, model_id,
         prompt_version, prompt_hash, lifecycle_state, runtime_kind, runtime_revision,
         config_hash, recovery_cursor, created_at, updated_at
       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT(conversation_id) DO UPDATE SET
         session_id = excluded.session_id,
         relative_session_file = excluded.relative_session_file,
         provider = excluded.provider,
         model_id = excluded.model_id,
         prompt_version = excluded.prompt_version,
         prompt_hash = excluded.prompt_hash,
         lifecycle_state = excluded.lifecycle_state,
         runtime_kind = excluded.runtime_kind,
         runtime_revision = excluded.runtime_revision,
         config_hash = excluded.config_hash,
         recovery_cursor = MAX(agent_sessions.recovery_cursor, excluded.recovery_cursor),
         updated_at = excluded.updated_at`,
    ).run(
      input.conversationId,
      input.sessionId,
      input.relativeSessionFile,
      input.provider,
      input.modelId,
      input.promptVersion,
      input.promptHash,
      input.lifecycleState,
      runtimeKind,
      runtimeRevision,
      configHash,
      recoveryCursor,
      timestamp,
      timestamp,
    );
    return this.getSession(input.conversationId)!;
  }

  upsertOpenHandsSession(input: OpenHandsSessionMetadataInput): AgentSessionSummary {
    const conversation = this.getConversation(input.conversationId);
    if (conversation.mode !== "OPENHANDS") {
      throw new StateConflictError("OpenHands session metadata requires an OpenHands conversation");
    }
    return this.upsertSession({ ...input, runtimeKind: "OPENHANDS" });
  }

  getSession(conversationId: string): AgentSessionSummary | null {
    const row = this.database.prepare("SELECT * FROM agent_sessions WHERE conversation_id = ?").get(conversationId) as SqlRow | undefined;
    if (!row) return null;
    return {
      schemaVersion: CONTRACT_SCHEMA_VERSION,
      sessionId: text(row, "session_id"),
      runtimeKind: text(row, "runtime_kind") as RuntimeMode,
      runtimeRevision: text(row, "runtime_revision"),
      configHash: text(row, "config_hash"),
      recoveryCursor: integer(row, "recovery_cursor"),
      provider: text(row, "provider"),
      modelId: text(row, "model_id"),
      promptVersion: text(row, "prompt_version"),
      promptHash: text(row, "prompt_hash"),
      lifecycleState: text(row, "lifecycle_state"),
      relativeSessionFile: optionalText(row, "relative_session_file"),
    };
  }

  updateOpenHandsRecoveryCursor(conversationId: string, runtimeSessionId: string, recoveryCursor: number): AgentSessionSummary {
    this.assertConversationWritable(conversationId);
    if (!Number.isSafeInteger(recoveryCursor) || recoveryCursor < 0) {
      throw new StateConflictError("recoveryCursor must be a non-negative safe integer");
    }
    const result = this.database.prepare(
      `UPDATE agent_sessions
       SET recovery_cursor = MAX(recovery_cursor, ?), updated_at = ?
       WHERE conversation_id = ? AND session_id = ? AND runtime_kind = 'OPENHANDS'`,
    ).run(recoveryCursor, now(), conversationId, runtimeSessionId);
    if (result.changes !== 1) {
      throw new StateConflictError("OpenHands runtime session metadata does not match the active conversation session");
    }
    return this.getSession(conversationId)!;
  }

  recordOpenHandsWorkspace(input: OpenHandsWorkspaceMetadataInput): OpenHandsWorkspaceSummary {
    this.assertConversationWritable(input.conversationId);
    const session = this.getSession(input.conversationId);
    if (
      session?.runtimeKind !== "OPENHANDS"
      || session.sessionId !== input.runtimeSessionId
      || input.backend !== "hardened-docker-agent-server"
      || input.hostLocalWorkspace !== false
      || input.workspaceId !== input.runtimeSessionId
    ) {
      throw new StateConflictError("OpenHands workspace identity does not match the active runtime session");
    }
    assertSha256(input.manifestHash, "manifestHash");
    if (!/^sha256:[a-f0-9]{64}$/u.test(input.imageDigest)) {
      throw new StateConflictError("OpenHands workspace imageDigest must be a sha256 image ID");
    }
    if (
      !input.imageReference.trim()
      || input.imageReference.includes(":latest")
      || !input.containerId.trim()
      || input.secretEnvironmentNames.length !== 0
    ) {
      throw new StateConflictError("OpenHands workspace isolation proof is invalid");
    }
    const snapshotJson = JSON.stringify(input.snapshot);
    const existing = this.database.prepare(
      `SELECT * FROM openhands_workspace_snapshots
       WHERE conversation_id = ? AND runtime_session_id = ?`,
    ).get(input.conversationId, input.runtimeSessionId) as SqlRow | undefined;
    if (existing) {
      const immutableMatches = text(existing, "manifest_hash") === input.manifestHash
        && text(existing, "backend") === input.backend
        && text(existing, "workspace_id") === input.workspaceId
        && text(existing, "snapshot_json") === snapshotJson
        && text(existing, "image_reference") === input.imageReference
        && text(existing, "image_digest") === input.imageDigest;
      if (!immutableMatches) {
        throw new StateConflictError("OpenHands durable workspace proof changed during recovery");
      }
      return this.getOpenHandsWorkspace(input.conversationId)!;
    } else {
      const timestamp = now();
      this.database.prepare(
        `INSERT INTO openhands_workspace_snapshots(
           conversation_id, runtime_session_id, manifest_hash, backend, workspace_id,
           snapshot_json, image_reference, image_digest, initial_container_id,
           server_reused_at_creation, secret_environment_names_json, created_at, updated_at
         ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?)`,
      ).run(
        input.conversationId,
        input.runtimeSessionId,
        input.manifestHash,
        input.backend,
        input.workspaceId,
        snapshotJson,
        input.imageReference,
        input.imageDigest,
        input.containerId,
        input.serverReused ? 1 : 0,
        timestamp,
        timestamp,
      );
    }
    return this.getOpenHandsWorkspace(input.conversationId)!;
  }

  getOpenHandsWorkspace(conversationId: string): OpenHandsWorkspaceSummary | null {
    const row = this.database.prepare(
      `SELECT workspace.*
       FROM openhands_workspace_snapshots AS workspace
       JOIN agent_sessions AS session
         ON session.conversation_id = workspace.conversation_id
        AND session.session_id = workspace.runtime_session_id
       WHERE workspace.conversation_id = ?`,
    ).get(conversationId) as SqlRow | undefined;
    if (!row) return null;
    const secretNames = jsonValue(row, "secret_environment_names_json");
    if (!Array.isArray(secretNames) || secretNames.some((name) => typeof name !== "string")) {
      throw new Error("stored OpenHands workspace secret-environment proof is invalid");
    }
    return {
      schemaVersion: CONTRACT_SCHEMA_VERSION,
      conversationId: text(row, "conversation_id"),
      runtimeSessionId: text(row, "runtime_session_id"),
      manifestHash: text(row, "manifest_hash"),
      backend: text(row, "backend") as "hardened-docker-agent-server",
      hostLocalWorkspace: false,
      workspaceId: text(row, "workspace_id"),
      snapshot: jsonObject(row, "snapshot_json")!,
      imageReference: text(row, "image_reference"),
      imageDigest: text(row, "image_digest"),
      initialContainerId: text(row, "initial_container_id"),
      serverReusedAtCreation: integer(row, "server_reused_at_creation") === 1,
      secretEnvironmentNames: secretNames as string[],
      createdAt: text(row, "created_at"),
      updatedAt: text(row, "updated_at"),
    };
  }

  private toolCallInboxFromRow(row: SqlRow): ToolCallInboxEntry {
    return {
      schemaVersion: CONTRACT_SCHEMA_VERSION,
      conversationId: text(row, "conversation_id"),
      runtimeSessionId: text(row, "runtime_session_id"),
      toolCallId: text(row, "tool_call_id"),
      toolName: text(row, "tool_name"),
      requestHash: text(row, "request_hash"),
      status: text(row, "status") as ToolCallInboxEntry["status"],
      result: jsonValue(row, "result_json"),
      error: jsonObject(row, "error_json"),
      claimedAt: text(row, "claimed_at"),
      finishedAt: optionalText(row, "finished_at"),
      updatedAt: text(row, "updated_at"),
    };
  }

  private getOpenHandsToolCall(
    conversationId: string,
    runtimeSessionId: string,
    toolCallId: string,
  ): ToolCallInboxEntry | null {
    const row = this.database.prepare(
      `SELECT * FROM openhands_tool_call_inbox
       WHERE conversation_id = ? AND runtime_session_id = ? AND tool_call_id = ?`,
    ).get(conversationId, runtimeSessionId, toolCallId) as SqlRow | undefined;
    return row ? this.toolCallInboxFromRow(row) : null;
  }

  claimOpenHandsToolCall(input: {
    conversationId: string;
    runtimeSessionId: string;
    toolCallId: string;
    toolName: string;
    requestHash: string;
  }): ToolCallClaim {
    this.assertConversationWritable(input.conversationId);
    assertSha256(input.requestHash, "requestHash");
    if (!input.toolCallId.trim() || !input.toolName.trim() || !input.runtimeSessionId.trim()) {
      throw new StateConflictError("runtime session, tool call, and tool name identifiers must not be empty");
    }
    const conversation = this.getConversation(input.conversationId);
    const session = this.getSession(input.conversationId);
    if (
      conversation.mode !== "OPENHANDS"
      || session?.runtimeKind !== "OPENHANDS"
      || session.sessionId !== input.runtimeSessionId
    ) {
      throw new StateConflictError("tool-call claims require the active OpenHands runtime session");
    }
    const timestamp = now();
    const inserted = this.database.prepare(
      `INSERT OR IGNORE INTO openhands_tool_call_inbox(
         conversation_id, runtime_session_id, tool_call_id, tool_name, request_hash,
         status, claimed_at, updated_at
       ) VALUES (?, ?, ?, ?, ?, 'RUNNING', ?, ?)`,
    ).run(
      input.conversationId,
      input.runtimeSessionId,
      input.toolCallId,
      input.toolName,
      input.requestHash,
      timestamp,
      timestamp,
    );
    const entry = this.getOpenHandsToolCall(input.conversationId, input.runtimeSessionId, input.toolCallId)!;
    if (entry.requestHash !== input.requestHash || entry.toolName !== input.toolName) {
      throw new StateConflictError("tool-call identity was reused with a different request");
    }
    return {
      disposition: inserted.changes === 1
        ? "CLAIMED"
        : entry.status === "COMPLETED" || entry.status === "FAILED"
          ? "REPLAY"
          : "IN_FLIGHT",
      entry,
    };
  }

  replayOpenHandsToolCall(
    conversationId: string,
    runtimeSessionId: string,
    toolCallId: string,
  ): ToolCallInboxEntry | null {
    const entry = this.getOpenHandsToolCall(conversationId, runtimeSessionId, toolCallId);
    return entry?.status === "COMPLETED" || entry?.status === "FAILED" ? entry : null;
  }

  completeOpenHandsToolCall(input: {
    conversationId: string;
    runtimeSessionId: string;
    toolCallId: string;
    result: JsonValue;
  }): ToolCallInboxEntry {
    this.assertConversationWritable(input.conversationId);
    const current = this.getOpenHandsToolCall(input.conversationId, input.runtimeSessionId, input.toolCallId);
    if (!current) throw new NotFoundError(`tool call ${input.toolCallId} was not claimed`);
    if (current.status === "COMPLETED") return current;
    if (current.status === "FAILED") throw new StateConflictError("a failed tool call cannot be completed");
    const timestamp = now();
    const resultJson = JSON.stringify(input.result);
    const updated = this.database.prepare(
      `UPDATE openhands_tool_call_inbox
       SET status = 'COMPLETED', result_json = ?, error_json = NULL, finished_at = ?, updated_at = ?
       WHERE conversation_id = ? AND runtime_session_id = ? AND tool_call_id = ?
         AND status IN ('RUNNING','UNKNOWN')`,
    ).run(resultJson, timestamp, timestamp, input.conversationId, input.runtimeSessionId, input.toolCallId);
    if (updated.changes !== 1) throw new StateConflictError("tool-call completion lost its durable claim");
    return this.getOpenHandsToolCall(input.conversationId, input.runtimeSessionId, input.toolCallId)!;
  }

  failOpenHandsToolCall(input: {
    conversationId: string;
    runtimeSessionId: string;
    toolCallId: string;
    error: JsonObject;
  }): ToolCallInboxEntry {
    this.assertConversationWritable(input.conversationId);
    const current = this.getOpenHandsToolCall(input.conversationId, input.runtimeSessionId, input.toolCallId);
    if (!current) throw new NotFoundError(`tool call ${input.toolCallId} was not claimed`);
    if (current.status === "FAILED") return current;
    if (current.status === "COMPLETED") throw new StateConflictError("a completed tool call cannot be failed");
    const timestamp = now();
    const updated = this.database.prepare(
      `UPDATE openhands_tool_call_inbox
       SET status = 'FAILED', result_json = NULL, error_json = ?, finished_at = ?, updated_at = ?
       WHERE conversation_id = ? AND runtime_session_id = ? AND tool_call_id = ?
         AND status IN ('RUNNING','UNKNOWN')`,
    ).run(JSON.stringify(input.error), timestamp, timestamp, input.conversationId, input.runtimeSessionId, input.toolCallId);
    if (updated.changes !== 1) throw new StateConflictError("tool-call failure lost its durable claim");
    return this.getOpenHandsToolCall(input.conversationId, input.runtimeSessionId, input.toolCallId)!;
  }

  markOpenHandsToolCallsUnknown(conversationId: string, runtimeSessionId: string): number {
    this.assertConversationWritable(conversationId);
    const result = this.database.prepare(
      `UPDATE openhands_tool_call_inbox
       SET status = 'UNKNOWN', updated_at = ?
       WHERE conversation_id = ? AND runtime_session_id = ? AND status = 'RUNNING'`,
    ).run(now(), conversationId, runtimeSessionId);
    return Number(result.changes);
  }

  listOpenHandsToolCallsForRecovery(conversationId: string, runtimeSessionId: string): ToolCallInboxEntry[] {
    return rows(
      this.database.prepare(
        `SELECT * FROM openhands_tool_call_inbox
         WHERE conversation_id = ? AND runtime_session_id = ? AND status IN ('RUNNING','UNKNOWN')
         ORDER BY claimed_at, tool_call_id`,
      ),
      conversationId,
      runtimeSessionId,
    ).map((row) => this.toolCallInboxFromRow(row));
  }

  getSnapshot(conversationId: string): ConversationSnapshot {
    return {
      schemaVersion: CONTRACT_SCHEMA_VERSION,
      conversation: this.getConversation(conversationId),
      messages: this.listMessages(conversationId),
      steps: this.listSteps(conversationId),
      artifacts: this.listArtifacts(conversationId),
      approvals: this.listApprovals(conversationId),
      session: this.getSession(conversationId),
      workspace: this.getOpenHandsWorkspace(conversationId),
      recentEvents: this.listRecentEvents(conversationId, 240),
      latestSequence: this.latestSequence(conversationId),
      capturedAt: now(),
    };
  }
}
