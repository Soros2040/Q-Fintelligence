// P16 change authorship category: supervisor_infrastructure

import { mkdir, mkdtemp, readdir, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";

import { afterEach, describe, expect, it, vi } from "vitest";

import { createApplication, type QfApplication } from "../../apps/control-plane/src/app.js";
import type { RuntimeConfig } from "../../apps/control-plane/src/config.js";

const temporaryRoots: string[] = [];
const applications: QfApplication[] = [];
const FAKE_OPENHANDS_SIDECAR = path.resolve("tests/fixtures/openhands-sidecar-fake.mjs");

afterEach(async () => {
  await Promise.all(applications.splice(0).map(({ app }) => app.close()));
  await Promise.all(temporaryRoots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
});

async function setup(agentMode: RuntimeConfig["agentMode"] = "mock"): Promise<{ application: QfApplication; directory: string }> {
  const directory = await mkdtemp(path.join(os.tmpdir(), "qf-api-test-"));
  temporaryRoots.push(directory);
  const localRoot = path.resolve(".local");
  await mkdir(localRoot, { recursive: true });
  const openHandsSessionRoot = await mkdtemp(path.join(localRoot, "qf-api-openhands-test-"));
  temporaryRoots.push(openHandsSessionRoot);
  const config: RuntimeConfig = {
    project: "q-fintelligence",
    processNamespace: "qfintelligence",
    apiHost: "127.0.0.1",
    apiPort: 27_872,
    webPort: 27_871,
    artifactRoot: path.join(directory, "artifacts"),
    sqlitePath: path.join(directory, "state.sqlite3"),
    stateRoot: directory,
    openHandsSessionRoot,
    openHandsSidecarPath: process.execPath,
    openHandsSidecarArguments: [FAKE_OPENHANDS_SIDECAR],
    systemPromptVersion: "qf-agent-p0.v1",
    agentMode,
    hardwarePolicy: {
      mode: "READ_ONLY",
      target: "tianyan176",
      maxNewHardwareJobs: 0,
      shotsPerJob: 0,
      authorizationBasis: null,
    },
  };
  const application = createApplication({ projectRoot: process.cwd(), config, databasePath: config.sqlitePath, logger: false });
  applications.push(application);
  await application.app.ready();
  return { application, directory };
}

function durableDatabaseSnapshot(databasePath: string): Record<string, unknown[]> {
  const database = new DatabaseSync(databasePath);
  try {
    const tables = (database.prepare(
      "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name",
    ).all() as Array<{ name: string }>).map((row) => row.name);
    return Object.fromEntries(tables.map((table) => [
      table,
      database.prepare(`SELECT * FROM ${table} ORDER BY rowid`).all(),
    ]));
  } finally {
    database.close();
  }
}

async function fileTree(root: string): Promise<string[]> {
  try {
    return (await readdir(root, { recursive: true })).map(String).sort();
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return [];
    throw error;
  }
}

describe("P0 Mock API closure", () => {
  it("archives every visible project and conversation through the human frontend endpoint", async () => {
    const { application } = await setup();
    const first = application.repository.createProject("History A");
    const second = application.repository.createProject("History B");
    application.repository.createConversation({ projectId: first.projectId, title: "A1", mode: "MOCK" });
    application.repository.createConversation({ projectId: second.projectId, title: "B1", mode: "MOCK" });

    const archived = await application.app.inject({ method: "POST", url: "/api/projects/archive-all" });
    expect(archived.statusCode).toBe(200);
    expect(archived.json()).toMatchObject({ projectsArchived: 2, conversationsArchived: 2 });
    expect((await application.app.inject({ method: "GET", url: "/api/projects" })).json()).toEqual({ projects: [] });
    expect(application.repository.listProjects(true)).toHaveLength(2);
    expect(application.repository.listConversations(first.projectId, true)).toEqual([
      expect.objectContaining({ archived: true }),
    ]);
  });

  it("keeps historical Pi conversations readable while rejecting new writes", async () => {
    const { application } = await setup("hybrid");
    const project = application.repository.createProject("Legacy Pi history");
    const legacy = application.repository.createConversation({
      projectId: project.projectId,
      title: "Historical session",
      mode: "MOCK",
    });
    const database = new DatabaseSync(application.config.sqlitePath);
    database.prepare(
      "UPDATE conversations SET mode = 'PI', provider = 'deepseek', model_id = 'deepseek-v4-pro' WHERE id = ?",
    ).run(legacy.conversationId);
    database.close();

    const snapshot = await application.app.inject({
      method: "GET",
      url: `/api/conversations/${legacy.conversationId}/snapshot`,
    });
    expect(snapshot.statusCode).toBe(200);
    expect(snapshot.json().conversation).toMatchObject({ mode: "PI", title: "Historical session" });
    expect((await application.app.inject({
      method: "POST",
      url: `/api/conversations/${legacy.conversationId}/messages`,
      payload: { content: "must remain read-only" },
    })).statusCode).toBe(409);
    expect((await application.app.inject({
      method: "PATCH",
      url: `/api/conversations/${legacy.conversationId}/model`,
      payload: { provider: "openai", modelId: "gpt-5.6", actor: "HUMAN" },
    })).statusCode).toBe(409);
  });

  it("rejects legacy API and Campaign writes before Provider, parser, files, artifacts, or external actions", async () => {
    const { application } = await setup("hybrid");
    const project = application.repository.createProject("Legacy API immutability");
    const legacy = application.repository.createConversation({
      projectId: project.projectId,
      title: "Historical session",
      mode: "OPENHANDS",
      provider: "deepseek",
      modelId: "deepseek-v4-pro",
    });
    const approval = application.repository.createApprovalRequest({
      conversationId: legacy.conversationId,
      action: "FREEZE_PROTOCOL",
      subjectHash: "a".repeat(64),
      rationale: "seed pending approval",
    });
    application.repository.applyRename({
      scope: "CONVERSATION",
      subjectId: legacy.conversationId,
      name: "Historical session renamed",
      actor: "HUMAN",
    });
    const p05TaskId = application.p05Repository.createTask({
      conversationId: legacy.conversationId,
      taskId: legacy.taskId,
      objective: "historical P05",
      taskStartedAt: "2026-07-29T00:00:00.000Z",
      hardDeadlineAt: "2099-07-29T00:00:00.000Z",
    });
    const p07CampaignId = application.p07Repository.createCampaign({
      conversationId: legacy.conversationId,
      taskId: legacy.taskId,
      objective: "historical P07",
    });
    const p04Seed = await application.app.inject({ method: "POST", url: "/api/campaigns/p04" });
    expect(p04Seed.statusCode, p04Seed.body).toBe(201);
    const p04ConversationId = p04Seed.json().campaign.conversationId as string;

    const database = new DatabaseSync(application.config.sqlitePath);
    database.prepare(
      "UPDATE conversations SET mode = 'PI', provider = 'deepseek', model_id = 'deepseek-v4-pro' WHERE id IN (?, ?)",
    ).run(legacy.conversationId, p04ConversationId);
    database.close();

    const providerSpy = vi.spyOn(application.providers, "generateRenameSuggestion")
      .mockRejectedValue(new Error("PROVIDER_MUST_NOT_BE_CALLED"));
    const uploadRoot = path.join(process.cwd(), ".local", "p05", "uploads", legacy.conversationId);
    const sourceRoot = path.join(process.cwd(), ".local", "project-sources", project.projectId);
    const beforeDatabase = durableDatabaseSnapshot(application.config.sqlitePath);
    const beforeFiles = {
      uploads: await fileTree(uploadRoot),
      sources: await fileTree(sourceRoot),
    };
    const commands: Array<[string, () => Promise<Awaited<ReturnType<typeof application.app.inject>>>]> = [
      ["abort", () => application.app.inject({
        method: "POST", url: `/api/conversations/${legacy.conversationId}/abort`,
      })],
      ["rename suggest", () => application.app.inject({
        method: "POST", url: `/api/conversations/${legacy.conversationId}/rename/suggest`,
      })],
      ["rename apply", () => application.app.inject({
        method: "PATCH",
        url: `/api/conversations/${legacy.conversationId}/rename`,
        payload: { name: "blocked", actor: "HUMAN" },
      })],
      ["rename undo", () => application.app.inject({
        method: "POST", url: `/api/conversations/${legacy.conversationId}/rename/undo`,
      })],
      ["approval", () => application.app.inject({
        method: "POST",
        url: `/api/tasks/${legacy.taskId}/approvals/${approval.approvalId}/decision`,
        payload: { decision: "APPROVE", actor: "HUMAN" },
      })],
      ["P05 upload", () => application.app.inject({
        method: "POST",
        url: "/api/p05/uploads",
        payload: {
          conversationId: legacy.conversationId,
          fileName: "blocked.csv",
          declaredMediaType: "text/csv",
          bytesBase64: Buffer.from("a,b\n1,2\n").toString("base64"),
        },
      })],
      ["project source upload", () => application.app.inject({
        method: "POST",
        url: `/api/projects/${project.projectId}/sources`,
        payload: {
          conversationId: legacy.conversationId,
          fileName: "blocked-source.csv",
          declaredMediaType: "text/csv",
          bytesBase64: Buffer.from("a,b\n1,2\n").toString("base64"),
        },
      })],
      ["P05 resume", () => application.app.inject({
        method: "POST", url: `/api/p05/tasks/${p05TaskId}/resume`,
      })],
      ["P07 authorize", () => application.app.inject({
        method: "POST",
        url: `/api/p07/campaigns/${p07CampaignId}/authorize`,
        payload: { phrase: "AUTHORIZE P07 DEEPSEEK AND TIANYAN176" },
      })],
      ["P07 resume", () => application.app.inject({
        method: "POST", url: `/api/p07/campaigns/${p07CampaignId}/resume`,
      })],
      ["P07 gate", () => application.app.inject({
        method: "POST",
        url: `/api/p07/campaigns/${p07CampaignId}/gates`,
        payload: { gate: "browser", status: "PASS" },
      })],
      ["P07 finalize", () => application.app.inject({
        method: "POST", url: `/api/p07/campaigns/${p07CampaignId}/finalize-blocked`,
      })],
      ["P04 prepare", () => application.app.inject({ method: "POST", url: "/api/campaigns/p04" })],
    ];
    try {
      for (const [name, command] of commands) {
        const response = await command();
        expect(response.statusCode, `${name}: ${response.body}`).toBe(409);
        expect(response.body, name).toContain("LEGACY_READ_ONLY");
        expect(durableDatabaseSnapshot(application.config.sqlitePath), `${name} changed durable state`)
          .toEqual(beforeDatabase);
        expect({ uploads: await fileTree(uploadRoot), sources: await fileTree(sourceRoot) }, `${name} changed files`)
          .toEqual(beforeFiles);
      }
      expect(providerSpy).not.toHaveBeenCalled();
    } finally {
      providerSpy.mockRestore();
    }
  });

  it("keeps project rename available without appending an event to a Pi child", async () => {
    const { application } = await setup("hybrid");
    const project = application.repository.createProject("Project rename remains available");
    const legacy = application.repository.createConversation({
      projectId: project.projectId,
      title: "Historical child",
      mode: "MOCK",
    });
    const database = new DatabaseSync(application.config.sqlitePath);
    database.prepare("UPDATE conversations SET mode = 'PI' WHERE id = ?").run(legacy.conversationId);
    database.close();

    const renamed = await application.app.inject({
      method: "PATCH",
      url: `/api/projects/${project.projectId}/rename`,
      payload: { name: "Renamed project", actor: "HUMAN" },
    });
    expect(renamed.statusCode, renamed.body).toBe(200);
    expect(application.repository.getProject(project.projectId).name).toBe("Renamed project");
    expect(application.repository.listEventsAfter(legacy.conversationId, 0)).toEqual([]);

    const undone = await application.app.inject({
      method: "POST", url: `/api/projects/${project.projectId}/rename/undo`,
    });
    expect(undone.statusCode, undone.body).toBe(200);
    expect(application.repository.getProject(project.projectId).name).toBe("Project rename remains available");
    expect(application.repository.listEventsAfter(legacy.conversationId, 0)).toEqual([]);
  });

  it("creates an OpenHands conversation without a provider probe and switches to a fresh DeepSeek runtime revision", async () => {
    const { application } = await setup("hybrid");
    const project = application.repository.createProject("P11 one-click conversation");
    const rejectedLegacyCreate = await application.app.inject({
      method: "POST",
      url: `/api/projects/${project.projectId}/conversations`,
      payload: { title: "legacy", mode: "PI", provider: "deepseek", modelId: "deepseek-v4-pro" },
    });
    expect(rejectedLegacyCreate.statusCode).toBe(400);
    const create = await application.app.inject({
      method: "POST",
      url: `/api/projects/${project.projectId}/conversations`,
      payload: {
        title: "新对话",
        mode: "OPENHANDS",
        provider: "openai",
        modelId: "gpt-5.6",
      },
    });
    expect(create.statusCode).toBe(201);
    const created = create.json();
    const previousSessionId = "11111111-1111-4111-8111-111111111111";
    const previousConfigHash = "b".repeat(64);
    application.repository.upsertOpenHandsSession({
      conversationId: created.conversationId,
      sessionId: previousSessionId,
      relativeSessionFile: "p11/conversation-state",
      provider: "openai",
      modelId: "gpt-5.6",
      promptVersion: "qf-agent-p0.v1",
      promptHash: "a".repeat(64),
      lifecycleState: "READY",
      runtimeRevision: "qf-openhands-adapter.v2/1.39.0+qf.noobservability.1/r1",
      configHash: previousConfigHash,
    });

    const previousDeepseekKey = process.env.DEEPSEEK_API_KEY;
    const previousDeepseekBaseUrl = process.env.DEEPSEEK_BASE_URL;
    process.env.DEEPSEEK_API_KEY = "fake-api-integration-key";
    process.env.DEEPSEEK_BASE_URL = "https://fake-provider.invalid/compatible-mode/v1";
    let switched;
    try {
      switched = await application.app.inject({
        method: "PATCH",
        url: `/api/conversations/${created.conversationId}/model`,
        payload: { provider: "deepseek", modelId: "deepseek-v4-pro", actor: "HUMAN" },
      });
    } finally {
      if (previousDeepseekKey === undefined) delete process.env.DEEPSEEK_API_KEY;
      else process.env.DEEPSEEK_API_KEY = previousDeepseekKey;
      if (previousDeepseekBaseUrl === undefined) delete process.env.DEEPSEEK_BASE_URL;
      else process.env.DEEPSEEK_BASE_URL = previousDeepseekBaseUrl;
    }
    expect(switched.statusCode, switched.body).toBe(200);
    expect(switched.json()).toMatchObject({
      conversationId: created.conversationId,
      taskId: created.taskId,
      mode: "OPENHANDS",
      provider: "deepseek",
      modelId: "deepseek-v4-pro",
    });
    const switchedSession = application.repository.getSession(created.conversationId)!;
    expect(switchedSession).toMatchObject({
      runtimeKind: "OPENHANDS",
      runtimeRevision: "qf-openhands-adapter.v2/1.39.0+qf.noobservability.1/r2",
      provider: "deepseek",
      modelId: "deepseek-v4-pro",
    });
    expect(switchedSession.sessionId).not.toBe(previousSessionId);
    expect(switchedSession.configHash).not.toBe(previousConfigHash);
    const modelEvents = application.repository.listEventsAfter(created.conversationId, 0)
      .filter((event) => event.type === "model.changed");
    expect(modelEvents).toHaveLength(1);
    expect(modelEvents[0]?.payload).toMatchObject({
      previousProvider: "openai",
      previousModelId: "gpt-5.6",
      provider: "deepseek",
      modelId: "deepseek-v4-pro",
      actor: "HUMAN",
      sessionId: switchedSession.sessionId,
      runtimeRevision: switchedSession.runtimeRevision,
      configHash: switchedSession.configHash,
    });

    const rejectedPair = await application.app.inject({
      method: "PATCH",
      url: `/api/conversations/${created.conversationId}/model`,
      payload: { provider: "openai", modelId: "deepseek-v4-pro", actor: "HUMAN" },
    });
    expect(rejectedPair.statusCode).toBe(409);
  });

  it("replays every P07 event after a stable cursor even when the snapshot exceeds the socket high-water mark", async () => {
    const { application } = await setup();
    const project = application.repository.createProject("P07 SSE", "backpressure replay");
    const conversation = application.repository.createConversation({
      projectId: project.projectId,
      title: "P07 SSE",
      mode: "OPENHANDS",
      provider: "deepseek",
      modelId: "deepseek-v4-pro",
    });
    const campaignId = application.p07Repository.createCampaign({
      conversationId: conversation.conversationId,
      taskId: conversation.taskId,
      objective: "SSE replay",
    });
    for (let index = 0; index < 400; index += 1) {
      application.p07Repository.appendEvent({
        campaignId,
        lane: "visualization_archive",
        eventType: "monitor.checkpoint",
        idempotencyKey: `large-event-${index}`,
        payload: { summary: `event ${index}`, evidence: "x".repeat(2_048) },
      });
    }
    const replay = await application.app.inject({
      method: "GET",
      url: `/api/p07/campaigns/${campaignId}/events?after=100&once=1`,
      headers: { "last-event-id": "200" },
    });
    const replayedIds = [...replay.body.matchAll(/^id: (\d+)$/gmu)].map((match) => Number(match[1]));
    expect(replayedIds).toHaveLength(200);
    expect(replayedIds[0]).toBe(201);
    expect(replayedIds.at(-1)).toBe(400);
    expect(replayedIds).toEqual([...new Set(replayedIds)].sort((left, right) => left - right));
  });

  it("creates one idempotent P04 Campaign with three isolated Run lanes through the API", async () => {
    const { application } = await setup();
    const first = await application.app.inject({ method: "POST", url: "/api/campaigns/p04" });
    expect(first.statusCode).toBe(201);
    const detail = first.json();
    expect(detail.campaign.minimumRuntimeSeconds).toBe(21_600);
    expect(detail.runs.map((run: { lane: string }) => run.lane)).toEqual(["finance", "tool_factory", "evidence_audit"]);
    expect(new Set(detail.runs.map((run: { relativeWorkspace: string }) => run.relativeWorkspace)).size).toBe(3);
    const second = await application.app.inject({ method: "POST", url: "/api/campaigns/p04" });
    expect(second.json().campaign.campaignId).toBe(detail.campaign.campaignId);
    expect(second.json().runs.map((run: { runId: string }) => run.runId)).toEqual(detail.runs.map((run: { runId: string }) => run.runId));
  });

  it("persists a Mock run, rejects concurrency and replays SSE without duplicate sequences", async () => {
    const { application } = await setup();
    const projectResponse = await application.app.inject({ method: "POST", url: "/api/projects", payload: { name: "六股票风险图原型" } });
    expect(projectResponse.statusCode).toBe(201);
    const project = projectResponse.json();
    const conversationResponse = await application.app.inject({
      method: "POST",
      url: `/api/projects/${project.projectId}/conversations`,
      payload: { title: "TaskSpec", mode: "MOCK" },
    });
    expect(conversationResponse.statusCode).toBe(201);
    const conversation = conversationResponse.json();
    const concurrent = await Promise.all([
      application.app.inject({
        method: "POST",
        url: `/api/conversations/${conversation.conversationId}/messages`,
        payload: { content: "为六股票风险图任务起草 TaskSpec，并检查当前工程状态", intent: "WORK" },
      }),
      application.app.inject({
        method: "POST",
        url: `/api/conversations/${conversation.conversationId}/messages`,
        payload: { content: "duplicate" },
      }),
    ]);
    expect(concurrent.map((response) => response.statusCode).sort()).toEqual([202, 409]);
    await application.runtimeManager.waitForIdle(conversation.conversationId);
    const snapshot = application.repository.getSnapshot(conversation.conversationId);
    expect(snapshot.conversation.state).toBe("COMPLETED");
    expect(snapshot.messages.map((message) => message.role)).toEqual(["USER", "ASSISTANT"]);
    expect(snapshot.messages[0]?.content).toBe("为六股票风险图任务起草 TaskSpec，并检查当前工程状态");
    expect(snapshot.recentEvents.find((event) => event.type === "run.started")?.payload.promptIntent).toBe("WORK");
    expect(snapshot.artifacts).toHaveLength(1);
    expect(snapshot.approvals).toHaveLength(1);
    expect(snapshot.approvals[0]?.status).toBe("PENDING");
    const sequences = application.repository.listEventsAfter(conversation.conversationId, 0).map((event) => event.sequence);
    expect(sequences).toEqual([...sequences].sort((left, right) => (left ?? 0) - (right ?? 0)));
    expect(new Set(sequences).size).toBe(sequences.length);
    const after = sequences[Math.floor(sequences.length / 2)] ?? 0;
    const replay = await application.app.inject({
      method: "GET",
      url: `/api/conversations/${conversation.conversationId}/events?after=${after}&once=1`,
    });
    expect(replay.statusCode).toBe(200);
    const replayedIds = [...replay.body.matchAll(/^id: (\d+)$/gmu)].map((match) => Number(match[1]));
    expect(replayedIds.every((sequence) => sequence > after)).toBe(true);
    expect(new Set(replayedIds).size).toBe(replayedIds.length);
    const unknownArtifact = await application.app.inject({
      method: "GET",
      url: `/api/artifacts/${"0".repeat(64)}`,
    });
    expect(unknownArtifact.statusCode).toBe(404);
    const invalidIntent = await application.app.inject({
      method: "POST",
      url: `/api/conversations/${conversation.conversationId}/messages`,
      payload: { content: "invalid", intent: "TOOLS_ONLY" },
    });
    expect(invalidIntent.statusCode).toBe(400);
  });

  it("keeps approval decisions human-only and records controlled failure/abort states", async () => {
    const { application } = await setup();
    const project = application.repository.createProject("failure paths");
    const conversation = application.repository.createConversation({ projectId: project.projectId, title: "failure", mode: "MOCK" });
    await application.runtimeManager.startPrompt(conversation.conversationId, "触发可控失败");
    await application.runtimeManager.waitForIdle(conversation.conversationId);
    expect(application.repository.getConversation(conversation.conversationId).state).toBe("FAILED");
    const failureEvents = application.repository
      .listEventsAfter(conversation.conversationId, 0)
      .filter((event) => event.type === "run.failed");
    expect(failureEvents).toHaveLength(1);
    expect(failureEvents[0]?.payload.code).toBe("MOCK_CONTROLLED_FAILURE");
    const failedSnapshot = application.repository.getSnapshot(conversation.conversationId);
    expect(failedSnapshot.recentEvents.filter((event) => event.type === "run.failed")).toHaveLength(1);
    await application.runtimeManager.startPrompt(conversation.conversationId, "normal run to abort");
    await application.runtimeManager.abort(conversation.conversationId);
    await application.runtimeManager.waitForIdle(conversation.conversationId);
    expect(application.repository.getConversation(conversation.conversationId).state).toBe("ABORTED");
  });

  it("reconciles a stale RUNNING database state after the in-memory runtime was lost", async () => {
    const { application } = await setup();
    const project = application.repository.createProject("stale runtime recovery");
    const conversation = application.repository.createConversation({
      projectId: project.projectId,
      title: "stale runtime",
      mode: "MOCK",
    });
    application.repository.setConversationState(conversation.conversationId, "RUNNING");

    await application.runtimeManager.abort(conversation.conversationId);

    expect(application.repository.getConversation(conversation.conversationId).state).toBe("ABORTED");
    expect(application.repository.listEventsAfter(conversation.conversationId, 0)).toEqual([
      expect.objectContaining({
        type: "agent.aborted",
        payload: expect.objectContaining({
          code: "STALE_RUNTIME_RECONCILED",
          externalRequestsResubmitted: false,
        }),
      }),
    ]);
  });

  it("makes CHAT a tool-free runtime route while preserving the original user message", async () => {
    const { application } = await setup();
    const project = application.repository.createProject("chat route");
    const conversation = application.repository.createConversation({
      projectId: project.projectId,
      title: "tool-free chat",
      mode: "MOCK",
    });
    await application.runtimeManager.startPrompt(conversation.conversationId, "只回答这一句话", "CHAT");
    await application.runtimeManager.waitForIdle(conversation.conversationId);
    const snapshot = application.repository.getSnapshot(conversation.conversationId);
    expect(snapshot.messages[0]?.content).toBe("只回答这一句话");
    expect(snapshot.recentEvents.find((event) => event.type === "run.started")?.payload.promptIntent).toBe("CHAT");
    expect(snapshot.recentEvents.find((event) => event.type === "model.request")?.payload.tools).toEqual([]);
    expect(snapshot.recentEvents.filter((event) => event.type.startsWith("tool."))).toHaveLength(0);
    expect(snapshot.artifacts).toHaveLength(0);
    expect(snapshot.approvals).toHaveLength(0);
  });

  it("persists one run.failed event when the sidecar event and terminal response describe the same failure", async () => {
    const { application } = await setup("hybrid");
    const project = application.repository.createProject("OpenHands failure boundary");
    const conversation = application.repository.createConversation({
      projectId: project.projectId,
      title: "single terminal failure",
      mode: "OPENHANDS",
      provider: "deepseek",
      modelId: "deepseek-v4-pro",
    });
    const previousKey = process.env.DEEPSEEK_API_KEY;
    const previousBaseUrl = process.env.DEEPSEEK_BASE_URL;
    process.env.DEEPSEEK_API_KEY = "fake-api-integration-key";
    process.env.DEEPSEEK_BASE_URL = "https://fake-provider.invalid/compatible-mode/v1";
    let accepted;
    try {
      accepted = await application.app.inject({
        method: "POST",
        url: `/api/conversations/${conversation.conversationId}/messages`,
        payload: { content: "SIDECAR_FAILURE", intent: "WORK" },
      });
    } finally {
      if (previousKey === undefined) delete process.env.DEEPSEEK_API_KEY;
      else process.env.DEEPSEEK_API_KEY = previousKey;
      if (previousBaseUrl === undefined) delete process.env.DEEPSEEK_BASE_URL;
      else process.env.DEEPSEEK_BASE_URL = previousBaseUrl;
    }
    expect(accepted.statusCode, accepted.body).toBe(202);
    await application.runtimeManager.waitForIdle(conversation.conversationId);
    const events = application.repository.listEventsAfter(conversation.conversationId, 0);
    expect(events.filter((event) => event.type === "run.failed")).toHaveLength(1);
    expect(application.repository.getConversation(conversation.conversationId).state).toBe("FAILED");
  });

  it("persists an accepted OpenHands interrupt-then-follow-up steer and a queued follow-up", async () => {
    const { application } = await setup("hybrid");
    const project = application.repository.createProject("OpenHands queue boundary");
    const conversation = application.repository.createConversation({
      projectId: project.projectId,
      title: "unsupported steer",
      mode: "OPENHANDS",
      provider: "deepseek",
      modelId: "deepseek-v4-pro",
    });
    const previousKey = process.env.DEEPSEEK_API_KEY;
    const previousBaseUrl = process.env.DEEPSEEK_BASE_URL;
    process.env.DEEPSEEK_API_KEY = "fake-api-integration-key";
    process.env.DEEPSEEK_BASE_URL = "https://fake-provider.invalid/compatible-mode/v1";
    try {
      const accepted = await application.app.inject({
        method: "POST",
        url: `/api/conversations/${conversation.conversationId}/messages`,
        payload: { content: "LONG_ABORT", intent: "WORK" },
      });
      expect(accepted.statusCode, accepted.body).toBe(202);

      const steer = await application.app.inject({
        method: "POST",
        url: `/api/conversations/${conversation.conversationId}/steer`,
        payload: { content: "rejected steer must not become history" },
      });
      expect(steer.statusCode, steer.body).toBe(202);
      expect(steer.json()).toMatchObject({ accepted: true, mode: "steer" });
      expect(application.repository.getSnapshot(conversation.conversationId).messages.map((message) => message.content))
        .toEqual(["LONG_ABORT", "rejected steer must not become history"]);

      const followUp = await application.app.inject({
        method: "POST",
        url: `/api/conversations/${conversation.conversationId}/follow-up`,
        payload: { content: "accepted follow-up stays queued" },
      });
      expect(followUp.statusCode, followUp.body).toBe(202);
      expect(application.repository.getSnapshot(conversation.conversationId).messages.map((message) => message.content))
        .toEqual(["LONG_ABORT", "rejected steer must not become history", "accepted follow-up stays queued"]);
      await application.runtimeManager.abort(conversation.conversationId);
      await application.runtimeManager.waitForIdle(conversation.conversationId);
    } finally {
      if (previousKey === undefined) delete process.env.DEEPSEEK_API_KEY;
      else process.env.DEEPSEEK_API_KEY = previousKey;
      if (previousBaseUrl === undefined) delete process.env.DEEPSEEK_BASE_URL;
      else process.env.DEEPSEEK_BASE_URL = previousBaseUrl;
    }
  });
});
