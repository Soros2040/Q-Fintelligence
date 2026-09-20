// P16 change authorship category: supervisor_infrastructure

import { createHash } from "node:crypto";
import { copyFile, mkdir, mkdtemp, readFile, readdir, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";

import { afterEach, describe, expect, it } from "vitest";

import { openDatabase } from "../../apps/control-plane/src/db/migrations.js";
import { WorkspaceRepository } from "../../apps/control-plane/src/db/repository.js";
import { P05Repository } from "../../apps/control-plane/src/p05/p05-repository.js";

const temporaryRoots: string[] = [];

afterEach(async () => {
  await Promise.all(temporaryRoots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
});

async function root(): Promise<string> {
  const result = await mkdtemp(path.join(os.tmpdir(), "qf-workspace-test-"));
  temporaryRoots.push(result);
  return result;
}

const CONVERSATION_WRITE_TABLES = [
  "projects",
  "tasks",
  "runs",
  "conversations",
  "messages",
  "events",
  "steps",
  "artifacts",
  "conversation_artifacts",
  "artifact_edges",
  "approval_requests",
  "approvals",
  "agent_sessions",
  "rename_events",
  "project_sources",
  "openhands_tool_call_inbox",
] as const;

function conversationWriteSnapshot(database: DatabaseSync): Record<string, unknown[]> {
  return Object.fromEntries(CONVERSATION_WRITE_TABLES.map((table) => [
    table,
    database.prepare(`SELECT * FROM ${table} ORDER BY rowid`).all(),
  ]));
}

describe("agent workspace migrations and repository", () => {
  it("upgrades an empty database and remains idempotent", async () => {
    const directory = await root();
    const databasePath = path.join(directory, "empty.sqlite3");
    const first = openDatabase(databasePath, path.resolve("infra/sqlite"));
    expect(first.applied).toEqual([
      "0001_initial",
      "0002_agent_workspace",
      "0003_p02_campaign",
      "0004_p03_queue",
      "0005_p04_concurrent_campaign",
      "0006_p05_frontend_batch_optimization",
      "0007_p07_long_campaign",
      "0008_p07_model_continuation",
      "0009_project_sources",
      "0010_history_archive",
      "0011_p15_rename_calibration",
      "0012_p16_hardware_campaign",
      "0013_openhands_runtime",
      "0014_openhands_acceptance_campaign",
      "0015_openhands_acceptance_hardening",
      "0016_openhands_workspace",
    ]);
    first.database.close();
    const second = openDatabase(databasePath, path.resolve("infra/sqlite"));
    expect(second.applied).toEqual([]);
    expect(second.database.prepare("PRAGMA journal_mode").get()).toMatchObject({ journal_mode: "wal" });
    second.database.close();
  });

  it("upgrades a database that contains only 0001", async () => {
    const directory = await root();
    const databasePath = path.join(directory, "initial.sqlite3");
    const database = new DatabaseSync(databasePath);
    database.exec(await readFile("infra/sqlite/0001_initial.sql", "utf8"));
    database.close();
    const upgraded = openDatabase(databasePath, path.resolve("infra/sqlite"));
    expect(upgraded.applied).toEqual(["0002_agent_workspace", "0003_p02_campaign", "0004_p03_queue", "0005_p04_concurrent_campaign", "0006_p05_frontend_batch_optimization", "0007_p07_long_campaign", "0008_p07_model_continuation", "0009_project_sources", "0010_history_archive", "0011_p15_rename_calibration", "0012_p16_hardware_campaign", "0013_openhands_runtime", "0014_openhands_acceptance_campaign", "0015_openhands_acceptance_hardening", "0016_openhands_workspace"]);
    expect(upgraded.database.prepare("SELECT version FROM schema_migrations ORDER BY version").all()).toHaveLength(16);
    upgraded.database.close();
  });

  it("preserves a populated 0007 P07 Campaign and ledger while adding model continuation", async () => {
    const directory = await root();
    const legacyMigrations = path.join(directory, "legacy-migrations");
    await mkdir(legacyMigrations);
    for (const file of (await readdir("infra/sqlite")).filter((name) => /^000[1-7]_.+[.]sql$/u.test(name))) {
      await copyFile(path.join("infra/sqlite", file), path.join(legacyMigrations, file));
    }
    const databasePath = path.join(directory, "legacy-p07.sqlite3");
    const legacy = openDatabase(databasePath, legacyMigrations);
    const repository = new WorkspaceRepository(legacy.database);
    const project = repository.createProject("Legacy P07", "migration evidence");
    const mockConversation = repository.createConversation({
      projectId: project.projectId,
      title: "DeepSeek predecessor",
      mode: "MOCK",
    });
    legacy.database.prepare(
      "UPDATE conversations SET mode = 'PI', provider = 'deepseek', model_id = 'deepseek-v4-pro' WHERE id = ?",
    ).run(mockConversation.conversationId);
    const conversation = repository.getConversation(mockConversation.conversationId);
    const timestamp = "2026-07-23T00:00:00.000Z";
    legacy.database.prepare(
      `INSERT INTO p07_campaigns(
        id, task_id, conversation_id, objective, provider, model_id, status, stage, created_at, updated_at
      ) VALUES ('legacy-campaign', ?, ?, 'legacy', 'deepseek/getoken', 'deepseek-v4-pro',
        'BLOCKED', 'blocked', ?, ?)`,
    ).run(conversation.taskId, conversation.conversationId, timestamp, timestamp);
    legacy.database.prepare(
      `INSERT INTO p07_runs(id, campaign_id, lane, status, created_at, updated_at)
       VALUES ('legacy-run', 'legacy-campaign', 'deepseek_agents', 'BLOCKED', ?, ?)`,
    ).run(timestamp, timestamp);
    legacy.database.prepare(
      `INSERT INTO p07_token_ledger(
        id, campaign_id, run_id, role, purpose, provider, model_id, idempotency_key,
        prompt_sha256, status, total_tokens, started_at, completed_at
      ) VALUES ('legacy-call', 'legacy-campaign', 'legacy-run', 'scientific_critic', 'audit',
        'deepseek/getoken', 'deepseek-v4-pro', 'legacy-key', ?, 'COMPLETED', 123, ?, ?)`,
    ).run("a".repeat(64), timestamp, timestamp);
    legacy.database.prepare(
      `INSERT INTO agent_sessions(
        conversation_id, session_id, relative_session_file, provider, model_id,
        prompt_version, prompt_hash, lifecycle_state, created_at, updated_at
      ) VALUES (?, 'legacy-pi-session', 'legacy/session.jsonl', 'deepseek', 'deepseek-v4-pro',
        'qf-agent-p0.v1', ?, 'READY', ?, ?)`,
    ).run(conversation.conversationId, "b".repeat(64), timestamp, timestamp);
    repository.close();

    const upgraded = openDatabase(databasePath, path.resolve("infra/sqlite"));
    expect(upgraded.applied).toEqual(["0008_p07_model_continuation", "0009_project_sources", "0010_history_archive", "0011_p15_rename_calibration", "0012_p16_hardware_campaign", "0013_openhands_runtime", "0014_openhands_acceptance_campaign", "0015_openhands_acceptance_hardening", "0016_openhands_workspace"]);
    expect(upgraded.database.prepare(
      "SELECT predecessor_campaign_id, provider, model_id FROM p07_campaigns WHERE id = 'legacy-campaign'",
    ).get()).toMatchObject({ predecessor_campaign_id: null, provider: "deepseek/getoken", model_id: "deepseek-v4-pro" });
    expect(upgraded.database.prepare(
      "SELECT provider, model_id, total_tokens FROM p07_token_ledger WHERE id = 'legacy-call'",
    ).get()).toMatchObject({ provider: "deepseek/getoken", model_id: "deepseek-v4-pro", total_tokens: 123 });
    expect(upgraded.database.prepare(
      "SELECT mode, provider, model_id FROM conversations WHERE id = ?",
    ).get(conversation.conversationId)).toMatchObject({ mode: "PI", provider: "deepseek", model_id: "deepseek-v4-pro" });
    expect(upgraded.database.prepare(
      "SELECT runtime_kind, runtime_revision, config_hash, recovery_cursor FROM agent_sessions WHERE conversation_id = ?",
    ).get(conversation.conversationId)).toMatchObject({
      runtime_kind: "PI",
      runtime_revision: "pi-agent-session-legacy",
      config_hash: "b".repeat(64),
      recovery_cursor: 0,
    });
    expect(upgraded.database.prepare(
      "SELECT name FROM sqlite_schema WHERE sql LIKE '%conversations_openhands_migration%'",
    ).all()).toEqual([]);
    expect(upgraded.database.prepare("PRAGMA foreign_key_check").all()).toEqual([]);
    expect(upgraded.database.prepare("PRAGMA integrity_check").get()).toMatchObject({ integrity_check: "ok" });
    upgraded.database.close();
  });

  it("persists projects, messages, events and human-only approvals", async () => {
    const directory = await root();
    const opened = openDatabase(path.join(directory, "repo.sqlite3"), path.resolve("infra/sqlite"));
    const repository = new WorkspaceRepository(opened.database);
    const project = repository.createProject("六股票风险图原型", "Mock acceptance");
    const conversation = repository.createConversation({ projectId: project.projectId, title: "P0", mode: "MOCK" });
    repository.addMessage({ conversationId: conversation.conversationId, role: "USER", content: "hello" });
    const first = repository.appendEvent({ conversationId: conversation.conversationId, type: "agent.started", payload: {} });
    const second = repository.appendEvent({ conversationId: conversation.conversationId, type: "turn.started", payload: {} });
    expect(second.sequence).toBe((first.sequence ?? 0) + 1);
    expect(repository.listEventsAfter(conversation.conversationId, first.sequence ?? 0).map((event) => event.sequence)).toEqual([second.sequence]);
    const approval = repository.createApprovalRequest({
      conversationId: conversation.conversationId,
      action: "FREEZE_PROTOCOL",
      subjectHash: "a".repeat(64),
      rationale: "needs a human",
    });
    expect(approval.status).toBe("PENDING");
    expect(() => repository.decideApproval(approval.approvalId, "APPROVE", "AGENT" as "HUMAN")).toThrow(/human/);
    expect(repository.decideApproval(approval.approvalId, "APPROVE", "HUMAN").status).toBe("APPROVED");
    repository.close();
  });

  it("persists OpenHands revision metadata and provides exactly-once tool-call replay", async () => {
    const directory = await root();
    const opened = openDatabase(path.join(directory, "openhands.sqlite3"), path.resolve("infra/sqlite"));
    const repository = new WorkspaceRepository(opened.database);
    const project = repository.createProject("OpenHands inbox");
    expect(() => repository.createConversation({
      projectId: project.projectId,
      title: "legacy write",
      mode: "PI",
      provider: "deepseek",
      modelId: "deepseek-v4-pro",
    })).toThrow(/historical read-only/);
    const conversation = repository.createConversation({
      projectId: project.projectId,
      title: "OpenHands runtime",
      mode: "OPENHANDS",
      provider: "deepseek",
      modelId: "deepseek-v4-pro",
    });
    expect(() => repository.upsertSession({
      conversationId: conversation.conversationId,
      sessionId: "underspecified-openhands-session",
      relativeSessionFile: null,
      provider: "deepseek",
      modelId: "deepseek-v4-pro",
      promptVersion: "qf-openhands.v1",
      promptHash: "a".repeat(64),
      lifecycleState: "READY",
    })).toThrow(/explicit runtime revision and config hash/);
    const session = repository.upsertOpenHandsSession({
      conversationId: conversation.conversationId,
      sessionId: "openhands-conversation-1",
      relativeSessionFile: "openhands/conversation-1",
      provider: "deepseek",
      modelId: "deepseek-v4-pro",
      promptVersion: "qf-openhands.v1",
      promptHash: "a".repeat(64),
      lifecycleState: "READY",
      runtimeRevision: "openhands-sdk==1.39.0+qf.noobservability.1",
      configHash: "b".repeat(64),
    });
    expect(session).toMatchObject({
      runtimeKind: "OPENHANDS",
      runtimeRevision: "openhands-sdk==1.39.0+qf.noobservability.1",
      configHash: "b".repeat(64),
      recoveryCursor: 0,
    });

    const identity = {
      conversationId: conversation.conversationId,
      runtimeSessionId: session.sessionId,
      toolCallId: "tool-call-1",
      toolName: "get_task_context",
      requestHash: "c".repeat(64),
    };
    expect(repository.claimOpenHandsToolCall(identity)).toMatchObject({
      disposition: "CLAIMED",
      entry: { status: "RUNNING", result: null, error: null },
    });
    expect(repository.claimOpenHandsToolCall(identity).disposition).toBe("IN_FLIGHT");
    expect(() => repository.claimOpenHandsToolCall({ ...identity, requestHash: "d".repeat(64) })).toThrow(/different request/);
    expect(repository.markOpenHandsToolCallsUnknown(conversation.conversationId, session.sessionId)).toBe(1);
    expect(repository.listOpenHandsToolCallsForRecovery(conversation.conversationId, session.sessionId)).toEqual([
      expect.objectContaining({ toolCallId: "tool-call-1", status: "UNKNOWN" }),
    ]);
    expect(repository.completeOpenHandsToolCall({
      conversationId: conversation.conversationId,
      runtimeSessionId: session.sessionId,
      toolCallId: "tool-call-1",
      result: { taskId: conversation.taskId, formalTest: "SEALED" },
    })).toMatchObject({ status: "COMPLETED", result: { taskId: conversation.taskId, formalTest: "SEALED" } });
    expect(repository.claimOpenHandsToolCall(identity).disposition).toBe("REPLAY");
    expect(repository.replayOpenHandsToolCall(
      conversation.conversationId,
      session.sessionId,
      "tool-call-1",
    )).toMatchObject({ status: "COMPLETED", result: { taskId: conversation.taskId } });

    const failedIdentity = { ...identity, toolCallId: "tool-call-2", requestHash: "e".repeat(64) };
    expect(repository.claimOpenHandsToolCall(failedIdentity).disposition).toBe("CLAIMED");
    expect(repository.failOpenHandsToolCall({
      conversationId: conversation.conversationId,
      runtimeSessionId: session.sessionId,
      toolCallId: failedIdentity.toolCallId,
      error: { category: "TOOL", code: "READ_FAILED", retryable: false },
    })).toMatchObject({ status: "FAILED", error: { code: "READ_FAILED" } });
    expect(repository.claimOpenHandsToolCall(failedIdentity).disposition).toBe("REPLAY");
    expect(repository.updateOpenHandsRecoveryCursor(
      conversation.conversationId,
      session.sessionId,
      17,
    ).recoveryCursor).toBe(17);
    expect(repository.updateOpenHandsRecoveryCursor(
      conversation.conversationId,
      session.sessionId,
      4,
    ).recoveryCursor).toBe(17);
    expect(opened.database.prepare("PRAGMA foreign_key_check").all()).toEqual([]);
    repository.close();
  });

  it.each(["conversation mode", "session runtime"] as const)(
    "rejects every generic conversation write when legacy Pi is signaled by %s",
    async (legacySignal) => {
      const directory = await root();
      const opened = openDatabase(path.join(directory, `legacy-${legacySignal.replace(" ", "-")}.sqlite3`), path.resolve("infra/sqlite"));
      const repository = new WorkspaceRepository(opened.database);
      const project = repository.createProject(`Legacy ${legacySignal}`);
      const conversation = repository.createConversation({
        projectId: project.projectId,
        title: "Legacy immutable history",
        mode: "OPENHANDS",
        provider: "deepseek",
        modelId: "deepseek-v4-pro",
      });
      const session = repository.upsertOpenHandsSession({
        conversationId: conversation.conversationId,
        sessionId: `session-${legacySignal.replace(" ", "-")}`,
        relativeSessionFile: "openhands/legacy-history",
        provider: "deepseek",
        modelId: "deepseek-v4-pro",
        promptVersion: "qf-openhands.v1",
        promptHash: "1".repeat(64),
        lifecycleState: "READY",
        runtimeRevision: "openhands-sdk==1.39.0+qf.noobservability.1",
        configHash: "2".repeat(64),
      });
      const approval = repository.createApprovalRequest({
        conversationId: conversation.conversationId,
        action: "FREEZE_PROTOCOL",
        subjectHash: "3".repeat(64),
        rationale: "seed pending decision before migration",
      });
      repository.applyRename({
        scope: "CONVERSATION",
        subjectId: conversation.conversationId,
        name: "Legacy immutable history renamed",
        actor: "HUMAN",
      });
      for (const toolCallId of ["complete-me", "fail-me", "unknown-me"]) {
        repository.claimOpenHandsToolCall({
          conversationId: conversation.conversationId,
          runtimeSessionId: session.sessionId,
          toolCallId,
          toolName: "get_task_context",
          requestHash: createHash("sha256").update(toolCallId).digest("hex"),
        });
      }
      if (legacySignal === "conversation mode") {
        opened.database.prepare("UPDATE conversations SET mode = 'PI' WHERE id = ?").run(conversation.conversationId);
      } else {
        opened.database.prepare("UPDATE agent_sessions SET runtime_kind = 'PI' WHERE conversation_id = ?")
          .run(conversation.conversationId);
      }

      const manifest = {
        schemaVersion: "qf.v1" as const,
        sha256: "4".repeat(64),
        mediaType: "application/json",
        bytes: 2,
        relativePath: "legacy/blocked.json",
        producer: "legacy-read-only-test",
        createdAt: "2026-07-29T00:00:00.000Z",
        parentHashes: [],
      };
      const attempts: Array<[string, () => unknown]> = [
        ["model", () => repository.updateConversationModel(conversation.conversationId, "openai", "gpt-5.6")],
        ["state", () => repository.setConversationState(conversation.conversationId, "RUNNING")],
        ["message", () => repository.addMessage({ conversationId: conversation.conversationId, role: "USER", content: "blocked" })],
        ["event", () => repository.appendEvent({ conversationId: conversation.conversationId, type: "run.started", payload: {} })],
        ["step", () => repository.upsertStep({ conversationId: conversation.conversationId, name: "blocked", state: "RUNNING" })],
        ["artifact", () => repository.registerArtifact(conversation.conversationId, manifest)],
        ["approval request", () => repository.createApprovalRequest({
          conversationId: conversation.conversationId,
          action: "FREEZE_PROTOCOL",
          subjectHash: "5".repeat(64),
          rationale: "blocked",
        })],
        ["approval decision", () => repository.decideApproval(approval.approvalId, "APPROVE", "HUMAN")],
        ["rename suggestion", () => repository.createRenameSuggestion({
          scope: "CONVERSATION",
          projectId: project.projectId,
          conversationId: conversation.conversationId,
          newName: "Blocked suggestion",
          provider: "deepseek/getoken",
          modelId: "deepseek-v4-pro",
          contextSha256: "6".repeat(64),
          promptTokens: 1,
          completionTokens: 1,
        })],
        ["rename apply", () => repository.applyRename({
          scope: "CONVERSATION",
          subjectId: conversation.conversationId,
          name: "Blocked apply",
          actor: "HUMAN",
        })],
        ["rename undo", () => repository.undoRename("CONVERSATION", conversation.conversationId)],
        ["generated project source", () => repository.registerGeneratedProjectSource({
          projectId: project.projectId,
          conversationId: conversation.conversationId,
          fileName: "blocked.csv",
          extension: ".csv",
          mediaType: "text/csv",
          byteSize: 4,
          sha256: "7".repeat(64),
          parser: "csv",
          parseResult: { status: "PASS" },
        })],
        ["session", () => repository.upsertSession({
          conversationId: conversation.conversationId,
          sessionId: "replacement",
          relativeSessionFile: null,
          provider: "deepseek",
          modelId: "deepseek-v4-pro",
          promptVersion: "qf-openhands.v1",
          promptHash: "8".repeat(64),
          lifecycleState: "READY",
          runtimeKind: "OPENHANDS",
          runtimeRevision: "replacement",
          configHash: "9".repeat(64),
        })],
        ["recovery cursor", () => repository.updateOpenHandsRecoveryCursor(conversation.conversationId, session.sessionId, 1)],
        ["tool claim", () => repository.claimOpenHandsToolCall({
          conversationId: conversation.conversationId,
          runtimeSessionId: session.sessionId,
          toolCallId: "blocked-claim",
          toolName: "get_task_context",
          requestHash: "a".repeat(64),
        })],
        ["tool complete", () => repository.completeOpenHandsToolCall({
          conversationId: conversation.conversationId,
          runtimeSessionId: session.sessionId,
          toolCallId: "complete-me",
          result: { blocked: true },
        })],
        ["tool fail", () => repository.failOpenHandsToolCall({
          conversationId: conversation.conversationId,
          runtimeSessionId: session.sessionId,
          toolCallId: "fail-me",
          error: { code: "BLOCKED" },
        })],
        ["tool unknown", () => repository.markOpenHandsToolCallsUnknown(conversation.conversationId, session.sessionId)],
      ];
      const before = conversationWriteSnapshot(opened.database);
      for (const [name, attempt] of attempts) {
        expect(attempt, name).toThrow(/LEGACY_READ_ONLY/u);
        expect(conversationWriteSnapshot(opened.database), `${name} changed durable state`).toEqual(before);
      }
      repository.close();
    },
  );

  it("archives all active projects and conversations without deleting their evidence", async () => {
    const directory = await root();
    const opened = openDatabase(path.join(directory, "archive.sqlite3"), path.resolve("infra/sqlite"));
    const repository = new WorkspaceRepository(opened.database);
    const firstProject = repository.createProject("Historical A");
    const secondProject = repository.createProject("Historical B");
    const firstConversation = repository.createConversation({
      projectId: firstProject.projectId,
      title: "A1",
      mode: "MOCK",
    });
    const secondConversation = repository.createConversation({
      projectId: secondProject.projectId,
      title: "B1",
      mode: "MOCK",
    });
    const legacyConversation = repository.createConversation({
      projectId: firstProject.projectId,
      title: "Pi child remains byte-for-byte archived-state stable",
      mode: "MOCK",
    });
    const legacySessionConversation = repository.createConversation({
      projectId: firstProject.projectId,
      title: "Pi session child also remains stable",
      mode: "OPENHANDS",
      provider: "deepseek",
      modelId: "deepseek-v4-pro",
    });
    repository.upsertOpenHandsSession({
      conversationId: legacySessionConversation.conversationId,
      sessionId: "archive-test-session",
      relativeSessionFile: "openhands/archive-test",
      provider: "deepseek",
      modelId: "deepseek-v4-pro",
      promptVersion: "qf-openhands.v1",
      promptHash: "a".repeat(64),
      lifecycleState: "READY",
      runtimeRevision: "openhands-sdk==1.39.0+qf.noobservability.1",
      configHash: "b".repeat(64),
    });
    opened.database.prepare(
      "UPDATE conversations SET mode = 'PI', status = 'RUNNING', provider = 'deepseek', model_id = 'deepseek-v4-pro' WHERE id = ?",
    ).run(legacyConversation.conversationId);
    opened.database.prepare("UPDATE agent_sessions SET runtime_kind = 'PI' WHERE conversation_id = ?")
      .run(legacySessionConversation.conversationId);
    repository.addMessage({ conversationId: firstConversation.conversationId, role: "USER", content: "keep me" });
    repository.appendEvent({ conversationId: secondConversation.conversationId, type: "run.completed", payload: { summary: "keep event" } });

    expect(repository.archiveAllProjects()).toMatchObject({
      projectsArchived: 2,
      conversationsArchived: 2,
      archivedAt: expect.any(String),
    });
    expect(repository.listProjects()).toEqual([]);
    expect(repository.listProjects(true)).toHaveLength(2);
    expect(repository.listConversations(firstProject.projectId)).toEqual([]);
    expect(repository.listConversations(firstProject.projectId, true)).toEqual(expect.arrayContaining([
      expect.objectContaining({ conversationId: firstConversation.conversationId, archived: true }),
      expect.objectContaining({ conversationId: legacyConversation.conversationId, archived: false, mode: "PI" }),
      expect.objectContaining({ conversationId: legacySessionConversation.conversationId, archived: false, mode: "OPENHANDS" }),
    ]));
    expect(repository.listMessages(firstConversation.conversationId)).toHaveLength(1);
    expect(repository.listEventsAfter(secondConversation.conversationId, 0)).toHaveLength(1);

    repository.updateProject(firstProject.projectId, { archived: false });
    expect(repository.listProjects()).toEqual([
      expect.objectContaining({ projectId: firstProject.projectId, archived: false }),
    ]);
    expect(repository.listConversations(firstProject.projectId)).toEqual(expect.arrayContaining([
      expect.objectContaining({ conversationId: firstConversation.conversationId, archived: false }),
      expect.objectContaining({ conversationId: legacyConversation.conversationId, archived: false, mode: "PI" }),
      expect.objectContaining({ conversationId: legacySessionConversation.conversationId, archived: false, mode: "OPENHANDS" }),
    ]));
    expect(repository.listConversations(firstProject.projectId)).toHaveLength(3);
    repository.close();
  });

  it("audits manual and Agent-assisted renames while preserving stable IDs and undo history", async () => {
    const directory = await root();
    const opened = openDatabase(path.join(directory, "rename.sqlite3"), path.resolve("infra/sqlite"));
    const repository = new WorkspaceRepository(opened.database);
    const project = repository.createProject("原项目");
    const conversation = repository.createConversation({
      projectId: project.projectId,
      title: "原对话",
      mode: "OPENHANDS",
      provider: "deepseek",
      modelId: "deepseek-v4-pro",
    });
    repository.addMessage({
      conversationId: conversation.conversationId,
      role: "USER",
      content: "请基于六股票来源冻结 QUBO 并执行噪声感知 QAOA。",
    });

    const context = repository.getRenameContext("CONVERSATION", conversation.conversationId);
    expect(context.userQuestions).toEqual([
      "请基于六股票来源冻结 QUBO 并执行噪声感知 QAOA。",
    ]);
    expect(context.contextSha256).toMatch(/^[a-f0-9]{64}$/u);
    const suggestion = repository.createRenameSuggestion({
      scope: "CONVERSATION",
      projectId: project.projectId,
      conversationId: conversation.conversationId,
      newName: "六股票噪声感知 QAOA",
      provider: "deepseek/getoken",
      modelId: "deepseek-v4-pro",
      contextSha256: context.contextSha256,
      promptTokens: 120,
      completionTokens: 9,
    });
    const applied = repository.applyRename({
      scope: "CONVERSATION",
      subjectId: conversation.conversationId,
      name: suggestion.newName,
      actor: "AGENT",
      suggestionEventId: suggestion.renameEventId,
    });
    expect(applied).toMatchObject({
      status: "APPLIED",
      actor: "AGENT",
      oldName: "原对话",
      newName: "六股票噪声感知 QAOA",
      promptTokens: 120,
    });
    expect(repository.getConversation(conversation.conversationId)).toMatchObject({
      conversationId: conversation.conversationId,
      title: "六股票噪声感知 QAOA",
    });
    const reverted = repository.undoRename("CONVERSATION", conversation.conversationId);
    expect(reverted).toMatchObject({
      actor: "HUMAN",
      oldName: "六股票噪声感知 QAOA",
      newName: "原对话",
      undoOfEventId: applied.renameEventId,
    });
    expect(repository.getConversation(conversation.conversationId).title).toBe("原对话");
    repository.close();
  });

  it("shares project sources across conversations and archives them without deleting the file record", async () => {
    const directory = await root();
    const opened = openDatabase(path.join(directory, "project-sources.sqlite3"), path.resolve("infra/sqlite"));
    const workspace = new WorkspaceRepository(opened.database);
    const project = workspace.createProject("Shared sources", "project scope");
    const first = workspace.createConversation({ projectId: project.projectId, title: "First", mode: "MOCK" });
    const second = workspace.createConversation({ projectId: project.projectId, title: "Second", mode: "MOCK" });
    const repository = new P05Repository(opened.database);
    const source = repository.addProjectSource({
      projectId: project.projectId,
      conversationId: first.conversationId,
      fileName: "validation.csv",
      extension: ".csv",
      declaredMediaType: "text/csv",
      detectedMediaType: "text/csv",
      byteSize: 18,
      sha256: "a".repeat(64),
      relativePath: ".local/project-sources/shared/validation.csv",
      parser: "csv",
      riskLevel: "LOW",
      quarantined: false,
      parseResult: { status: "PASS" },
    });
    expect(repository.listProjectSources(project.projectId)).toEqual([source]);
    expect(repository.listUploads(second.conversationId)).toEqual([
      expect.objectContaining({
        uploadId: source.sourceId,
        conversationId: second.conversationId,
        projectId: project.projectId,
        sourceScope: "PROJECT",
        fileName: "validation.csv",
      }),
    ]);
    repository.archiveProjectSource(project.projectId, source.sourceId);
    expect(repository.listProjectSources(project.projectId)).toEqual([]);
    expect(opened.database.prepare(
      "SELECT archived_at FROM project_sources WHERE id = ?",
    ).get(source.sourceId)).toMatchObject({ archived_at: expect.any(String) });
    workspace.close();
  });

  it("loads a named P15 calibration snapshot for one-time completeness upgrades", async () => {
    const directory = await root();
    const opened = openDatabase(path.join(directory, "p15-calibration.sqlite3"), path.resolve("infra/sqlite"));
    const workspace = new WorkspaceRepository(opened.database);
    const project = workspace.createProject("P15 calibration", "upgrade replay");
    const conversation = workspace.createConversation({
      projectId: project.projectId,
      title: "P15",
      mode: "OPENHANDS",
      provider: "deepseek",
      modelId: "deepseek-v4-pro",
    });
    const snapshotId = workspace.registerP15Calibration({
      projectId: project.projectId,
      conversationId: conversation.conversationId,
      machineStatus: "running",
      retrievedAt: "2026-07-25T05:40:58.000Z",
      calibrationAt: "2026-07-25 13:31:17",
      cqlibVersion: "1.3.11",
      rawSha256: "a".repeat(64),
      normalizedSha256: "b".repeat(64),
      manifestSha256: "c".repeat(64),
      diffSha256: "d".repeat(64),
      csvSha256: "e".repeat(64),
      topologySha256: "f".repeat(64),
      completeness: "COMPLETE",
      missingFields: [],
      warnings: ["FSIM calibration unavailable"],
      sourceIds: [],
      previousSnapshotId: null,
    });
    expect(workspace.getP15Calibration(snapshotId)).toMatchObject({
      snapshotId,
      projectId: project.projectId,
      completeness: "COMPLETE",
      normalizedSha256: "b".repeat(64),
    });
    expect(workspace.getLatestP15Calibration(project.projectId)?.snapshotId).toBe(snapshotId);
    workspace.close();
  });

  it("recovers P05 concurrency probes and the next token wave beyond the UI event window", async () => {
    const directory = await root();
    const opened = openDatabase(path.join(directory, "p05-recovery.sqlite3"), path.resolve("infra/sqlite"));
    const workspace = new WorkspaceRepository(opened.database);
    const project = workspace.createProject("P05 recovery", "checkpoint recovery");
    const conversation = workspace.createConversation({
      projectId: project.projectId,
      title: "P05",
      mode: "OPENHANDS",
      provider: "openai/getoken",
      modelId: "gpt-5.6-sol",
    });
    const repository = new P05Repository(opened.database);
    const p05TaskId = repository.createTask({
      conversationId: conversation.conversationId,
      taskId: conversation.taskId,
      objective: "recovery",
      taskStartedAt: "2026-07-22T00:00:00.000Z",
      hardDeadlineAt: "2026-07-22T06:00:00.000Z",
    });
    repository.appendEvent({
      p05TaskId,
      eventType: "model.usage",
      summary: "并发 128 探测完成：16/128，usage 100 tokens",
      detail: { concurrency: 128 },
    });
    repository.appendEvent({
      p05TaskId,
      eventType: "model.usage",
      summary: "Token wave 14：新增 100，累计 200",
      detail: { wave: 14 },
    });
    for (let index = 0; index < 600; index += 1) {
      repository.appendEvent({
        p05TaskId,
        eventType: "run.heartbeat",
        summary: `heartbeat ${index}`,
      });
    }
    expect(repository.getTask(p05TaskId).events).toHaveLength(500);
    expect(repository.getTokenCampaignRecoveryState(p05TaskId)).toEqual({
      attemptedConcurrency: [128],
      nextWave: 15,
    });
    workspace.close();
  });
});
