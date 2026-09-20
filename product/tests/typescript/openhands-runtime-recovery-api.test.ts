import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readdir, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";

import { afterEach, describe, expect, it, vi } from "vitest";

import { createApplication, type QfApplication } from "../../apps/control-plane/src/app.js";
import type { RuntimeConfig } from "../../apps/control-plane/src/config.js";
import { READ_ONLY_HARDWARE_POLICY } from "../../apps/control-plane/src/hardware-policy.js";

const roots: string[] = [];
const applications: QfApplication[] = [];
const FAKE_SIDECAR_START_LOG = ".qf-fake-sidecar-starts.jsonl";
const FAKE_RECOVERY_DELAY = ".qf-fake-recovery-delay-ms";

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function externalStateSnapshot(databasePath: string): Record<string, unknown[]> {
  const database = new DatabaseSync(databasePath);
  try {
    const names = (database.prepare(
      `SELECT name FROM sqlite_schema
       WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
       ORDER BY name`,
    ).all() as Array<{ name: string }>).map((row) => row.name)
      .filter((name) => [
        "messages",
        "artifacts",
        "conversation_artifacts",
        "approval_requests",
        "approvals",
        "openhands_tool_call_inbox",
      ].includes(name) || /(?:p15|p16|hardware|quantum|query|lease)/u.test(name));
    return Object.fromEntries(names.map((name) => [
      name,
      database.prepare(`SELECT * FROM "${name}" ORDER BY rowid`).all(),
    ]));
  } finally {
    database.close();
  }
}

function durableDatabaseSnapshot(databasePath: string): Record<string, unknown[]> {
  const database = new DatabaseSync(databasePath);
  try {
    const names = (database.prepare(
      "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name",
    ).all() as Array<{ name: string }>).map((row) => row.name);
    return Object.fromEntries(names.map((name) => [
      name,
      database.prepare(`SELECT * FROM "${name}" ORDER BY rowid`).all(),
    ]));
  } finally {
    database.close();
  }
}

async function durableFileSnapshot(root: string): Promise<Record<string, string>> {
  const snapshot: Record<string, string> = {};
  async function visit(directory: string, prefix: string): Promise<void> {
    const entries = await readdir(directory, { withFileTypes: true });
    for (const entry of entries.sort((left, right) => left.name.localeCompare(right.name))) {
      const relative = prefix ? `${prefix}/${entry.name}` : entry.name;
      const absolute = path.join(directory, entry.name);
      if (entry.isDirectory()) {
        snapshot[`${relative}/`] = "directory";
        await visit(absolute, relative);
      } else if (entry.isFile()) {
        const content = await readFile(absolute);
        snapshot[relative] = `${content.byteLength}:${createHash("sha256").update(content).digest("hex")}`;
      } else {
        snapshot[relative] = "unsupported-entry";
      }
    }
  }
  try {
    await visit(root, "");
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return {};
    throw error;
  }
  return snapshot;
}

function runtimeStateRoot(sessionRoot: string, session: { relativeSessionFile: string | null }): string {
  if (session.relativeSessionFile === null) throw new Error("OpenHands session is missing its relative state path");
  return path.join(sessionRoot, session.relativeSessionFile);
}

async function sidecarStartCount(stateRoot: string): Promise<number> {
  try {
    return (await readFile(path.join(stateRoot, FAKE_SIDECAR_START_LOG), "utf8"))
      .split("\n")
      .filter(Boolean).length;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return 0;
    throw error;
  }
}

async function waitUntil(predicate: () => boolean | Promise<boolean>, timeoutMilliseconds = 5_000): Promise<void> {
  const deadline = Date.now() + timeoutMilliseconds;
  while (!(await predicate())) {
    if (Date.now() >= deadline) throw new Error("timed out waiting for recovery test precondition");
    await delay(10);
  }
}

function installFakeDeepSeekConfiguration(): () => void {
  const previousKey = process.env.DEEPSEEK_API_KEY;
  const previousBaseUrl = process.env.DEEPSEEK_BASE_URL;
  process.env.DEEPSEEK_API_KEY = "fake-recovery-api-key";
  process.env.DEEPSEEK_BASE_URL = "https://fake-provider.invalid/compatible-mode/v1";
  return () => {
    if (previousKey === undefined) delete process.env.DEEPSEEK_API_KEY;
    else process.env.DEEPSEEK_API_KEY = previousKey;
    if (previousBaseUrl === undefined) delete process.env.DEEPSEEK_BASE_URL;
    else process.env.DEEPSEEK_BASE_URL = previousBaseUrl;
  };
}

function runtimeConfig(root: string, sessionRoot: string): RuntimeConfig {
  return {
    project: "q-fintelligence",
    processNamespace: "qfintelligence",
    apiHost: "127.0.0.1",
    apiPort: 27_872,
    webPort: 27_871,
    artifactRoot: path.join(root, "artifacts"),
    sqlitePath: path.join(root, "state.sqlite3"),
    stateRoot: root,
    openHandsSessionRoot: sessionRoot,
    openHandsSidecarPath: process.execPath,
    openHandsSidecarArguments: [path.resolve("tests/fixtures/openhands-sidecar-fake.mjs")],
    systemPromptVersion: "qf-agent-p0.v1",
    agentMode: "hybrid",
    hardwarePolicy: READ_ONLY_HARDWARE_POLICY,
  };
}

function create(config: RuntimeConfig): QfApplication {
  const application = createApplication({
    projectRoot: process.cwd(),
    config,
    databasePath: config.sqlitePath,
    logger: false,
  });
  applications.push(application);
  return application;
}

async function seedCompletedOpenHandsConversation(application: QfApplication, title: string) {
  const project = application.repository.createProject(title);
  const conversation = application.repository.createConversation({
    projectId: project.projectId,
    title,
    mode: "OPENHANDS",
    provider: "deepseek",
    modelId: "deepseek-v4-pro",
  });
  const started = await application.app.inject({
    method: "POST",
    url: `/api/conversations/${conversation.conversationId}/messages`,
    payload: { content: "NORMAL_TEXT", intent: "CHAT" },
  });
  expect(started.statusCode, started.body).toBe(202);
  await application.runtimeManager.waitForIdle(conversation.conversationId);
  const session = application.repository.getSession(conversation.conversationId);
  if (!session) throw new Error("OpenHands seed did not create a durable session");
  return { project, conversation, session };
}

async function closeApplication(application: QfApplication): Promise<void> {
  const index = applications.indexOf(application);
  if (index >= 0) applications.splice(index, 1);
  await application.app.close();
  await delay(200);
}

afterEach(async () => {
  await Promise.all(applications.splice(0).map(({ app }) => app.close()));
  await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
});

describe("OpenHands exact recovery API after a Control Plane restart", () => {
  it("adds only retry evidence while session, messages, usage, tools, artifacts, approvals, Query IDs and hardware stay exact", async () => {
    const stateRoot = await mkdtemp(path.join(os.tmpdir(), "qf-openhands-recovery-api-"));
    roots.push(stateRoot);
    const localRoot = path.resolve(".local");
    await mkdir(localRoot, { recursive: true });
    const sessionRoot = await mkdtemp(path.join(localRoot, "qf-openhands-recovery-api-"));
    roots.push(sessionRoot);
    const config = runtimeConfig(stateRoot, sessionRoot);
    const previousKey = process.env.DEEPSEEK_API_KEY;
    const previousBaseUrl = process.env.DEEPSEEK_BASE_URL;
    process.env.DEEPSEEK_API_KEY = "fake-recovery-api-key";
    process.env.DEEPSEEK_BASE_URL = "https://fake-provider.invalid/compatible-mode/v1";
    try {
      const first = create(config);
      await first.app.ready();
      const project = first.repository.createProject("OpenHands recovery API");
      const conversation = first.repository.createConversation({
        projectId: project.projectId,
        title: "exact recovery",
        mode: "OPENHANDS",
        provider: "deepseek",
        modelId: "deepseek-v4-pro",
      });
      const started = await first.app.inject({
        method: "POST",
        url: `/api/conversations/${conversation.conversationId}/messages`,
        payload: { content: "NORMAL_TEXT", intent: "CHAT" },
      });
      expect(started.statusCode, started.body).toBe(202);
      await first.runtimeManager.waitForIdle(conversation.conversationId);
      const originalSession = first.repository.getSession(conversation.conversationId)!;
      await closeApplication(first);

      const second = create(config);
      await second.app.ready();
      const before = second.repository.getSnapshot(conversation.conversationId);
      const beforeExternal = externalStateSnapshot(config.sqlitePath);
      const beforeProviderEvidence = second.repository.listEventsAfter(conversation.conversationId, 0)
        .filter((event) => event.type === "model.request" || event.type === "model.usage");

      const recovered = await second.app.inject({
        method: "POST",
        url: `/api/conversations/${conversation.conversationId}/runtime/recover`,
      });
      expect(recovered.statusCode, recovered.body).toBe(200);
      expect(recovered.json()).toMatchObject({
        recovered: true,
        restarted: true,
        runtimeSessionId: originalSession.sessionId,
        runtimeRevision: originalSession.runtimeRevision,
        configHash: originalSession.configHash,
        recoveryCursor: originalSession.recoveryCursor,
        providerCallsAdded: 0,
        externalActionsReplayed: false,
      });

      const after = second.repository.getSnapshot(conversation.conversationId);
      for (const key of ["messages", "steps", "artifacts", "approvals", "session", "workspace"] as const) {
        expect(after[key]).toEqual(before[key]);
      }
      const afterProviderEvidence = second.repository.listEventsAfter(conversation.conversationId, 0)
        .filter((event) => event.type === "model.request" || event.type === "model.usage");
      expect(afterProviderEvidence).toEqual(beforeProviderEvidence);
      expect(externalStateSnapshot(config.sqlitePath)).toEqual(beforeExternal);
      const recoveryEvents = second.repository.listEventsAfter(conversation.conversationId, before.latestSequence)
        .filter((event) => event.type.startsWith("retry."));
      expect(recoveryEvents.map((event) => [event.type, event.payload.success ?? null])).toEqual([
        ["retry.started", null],
        ["retry.completed", true],
      ]);
    } finally {
      if (previousKey === undefined) delete process.env.DEEPSEEK_API_KEY;
      else process.env.DEEPSEEK_API_KEY = previousKey;
      if (previousBaseUrl === undefined) delete process.env.DEEPSEEK_BASE_URL;
      else process.env.DEEPSEEK_BASE_URL = previousBaseUrl;
    }
  });

  it("rejects recovery of a still-live cached sidecar with zero database, file, process, or Provider delta", async () => {
    const stateRoot = await mkdtemp(path.join(os.tmpdir(), "qf-openhands-live-recovery-"));
    roots.push(stateRoot);
    const localRoot = path.resolve(".local");
    await mkdir(localRoot, { recursive: true });
    const sessionRoot = await mkdtemp(path.join(localRoot, "qf-openhands-live-recovery-"));
    roots.push(sessionRoot);
    const config = runtimeConfig(stateRoot, sessionRoot);
    const restoreProvider = installFakeDeepSeekConfiguration();
    try {
      const application = create(config);
      await application.app.ready();
      const { conversation, session } = await seedCompletedOpenHandsConversation(application, "live sidecar gate");
      const runtimeRoot = runtimeStateRoot(sessionRoot, session);
      await waitUntil(async () => await sidecarStartCount(runtimeRoot) === 1);
      const beforeDatabase = durableDatabaseSnapshot(config.sqlitePath);
      const beforeFiles = {
        sessions: await durableFileSnapshot(sessionRoot),
        artifacts: await durableFileSnapshot(config.artifactRoot),
      };
      const fetchSpy = vi.spyOn(globalThis, "fetch");
      try {
        const response = await application.app.inject({
          method: "POST",
          url: `/api/conversations/${conversation.conversationId}/runtime/recover`,
        });
        expect(response.statusCode, response.body).toBe(409);
        expect(response.json()).toMatchObject({
          error: {
            category: "CONFLICT",
            code: "STATE_CONFLICT",
            message: expect.stringContaining("sidecar is still running"),
          },
        });
        expect(durableDatabaseSnapshot(config.sqlitePath)).toEqual(beforeDatabase);
        expect({
          sessions: await durableFileSnapshot(sessionRoot),
          artifacts: await durableFileSnapshot(config.artifactRoot),
        }).toEqual(beforeFiles);
        expect(await sidecarStartCount(runtimeRoot)).toBe(1);
        expect(fetchSpy).not.toHaveBeenCalled();
      } finally {
        fetchSpy.mockRestore();
      }
    } finally {
      restoreProvider();
    }
  });

  for (const legacyMode of ["PI", "MOCK"] as const) {
    it(`rejects ${legacyMode} recovery before durable, file, process, or Provider side effects`, async () => {
      const stateRoot = await mkdtemp(path.join(os.tmpdir(), `qf-openhands-${legacyMode.toLowerCase()}-recovery-`));
      roots.push(stateRoot);
      const localRoot = path.resolve(".local");
      await mkdir(localRoot, { recursive: true });
      const sessionRoot = await mkdtemp(path.join(localRoot, `qf-openhands-${legacyMode.toLowerCase()}-recovery-`));
      roots.push(sessionRoot);
      const config = runtimeConfig(stateRoot, sessionRoot);
      const application = create(config);
      await application.app.ready();
      const project = application.repository.createProject(`${legacyMode} recovery gate`);
      const conversation = application.repository.createConversation({
        projectId: project.projectId,
        title: `${legacyMode} recovery gate`,
        mode: "MOCK",
      });
      if (legacyMode === "PI") {
        const database = new DatabaseSync(config.sqlitePath);
        database.prepare("UPDATE conversations SET mode = 'PI' WHERE id = ?").run(conversation.conversationId);
        database.close();
      }
      const beforeDatabase = durableDatabaseSnapshot(config.sqlitePath);
      const beforeFiles = {
        sessions: await durableFileSnapshot(sessionRoot),
        artifacts: await durableFileSnapshot(config.artifactRoot),
      };
      const fetchSpy = vi.spyOn(globalThis, "fetch");
      try {
        const response = await application.app.inject({
          method: "POST",
          url: `/api/conversations/${conversation.conversationId}/runtime/recover`,
        });
        expect(response.statusCode, response.body).toBe(409);
        expect(response.json()).toMatchObject({
          error: {
            category: "CONFLICT",
            code: "STATE_CONFLICT",
            message: legacyMode === "PI"
              ? expect.stringContaining("LEGACY_READ_ONLY")
              : expect.stringContaining("only OpenHands conversations"),
          },
        });
        expect(durableDatabaseSnapshot(config.sqlitePath)).toEqual(beforeDatabase);
        expect({
          sessions: await durableFileSnapshot(sessionRoot),
          artifacts: await durableFileSnapshot(config.artifactRoot),
        }).toEqual(beforeFiles);
        expect(fetchSpy).not.toHaveBeenCalled();
      } finally {
        fetchSpy.mockRestore();
      }
    });
  }

  it("serializes recover, model switch, and abort so rejected concurrent controls add no side effects", async () => {
    const stateRoot = await mkdtemp(path.join(os.tmpdir(), "qf-openhands-concurrent-recovery-"));
    roots.push(stateRoot);
    const localRoot = path.resolve(".local");
    await mkdir(localRoot, { recursive: true });
    const sessionRoot = await mkdtemp(path.join(localRoot, "qf-openhands-concurrent-recovery-"));
    roots.push(sessionRoot);
    const config = runtimeConfig(stateRoot, sessionRoot);
    const restoreProvider = installFakeDeepSeekConfiguration();
    try {
      const first = create(config);
      await first.app.ready();
      const { conversation, session } = await seedCompletedOpenHandsConversation(first, "concurrent recovery gate");
      const runtimeRoot = runtimeStateRoot(sessionRoot, session);
      await waitUntil(async () => await sidecarStartCount(runtimeRoot) === 1);
      await closeApplication(first);
      await writeFile(path.join(runtimeRoot, FAKE_RECOVERY_DELAY), "1500\n", { encoding: "utf8", mode: 0o600 });

      const second = create(config);
      await second.app.ready();
      const fetchSpy = vi.spyOn(globalThis, "fetch");
      try {
        const recoveryPromise = (async () => await second.app.inject({
          method: "POST",
          url: `/api/conversations/${conversation.conversationId}/runtime/recover`,
        }))();
        await waitUntil(async () => (
          await sidecarStartCount(runtimeRoot) === 2
          && second.repository.listEventsAfter(conversation.conversationId, 0)
            .some((event) => event.type === "retry.started")
        ));
        const beforeRejectedControls = durableDatabaseSnapshot(config.sqlitePath);
        const beforeFiles = {
          sessions: await durableFileSnapshot(sessionRoot),
          artifacts: await durableFileSnapshot(config.artifactRoot),
        };
        const rejected = await Promise.all([
          second.app.inject({
            method: "POST",
            url: `/api/conversations/${conversation.conversationId}/runtime/recover`,
          }),
          second.app.inject({
            method: "PATCH",
            url: `/api/conversations/${conversation.conversationId}/model`,
            payload: { provider: "openai", modelId: "gpt-5.6", actor: "HUMAN" },
          }),
          second.app.inject({
            method: "POST",
            url: `/api/conversations/${conversation.conversationId}/abort`,
          }),
        ]);
        expect(rejected.map((response) => response.statusCode)).toEqual([409, 409, 409]);
        expect(rejected.map((response) => response.json().error.code)).toEqual([
          "STATE_CONFLICT",
          "STATE_CONFLICT",
          "STATE_CONFLICT",
        ]);
        expect(durableDatabaseSnapshot(config.sqlitePath)).toEqual(beforeRejectedControls);
        expect({
          sessions: await durableFileSnapshot(sessionRoot),
          artifacts: await durableFileSnapshot(config.artifactRoot),
        }).toEqual(beforeFiles);
        expect(await sidecarStartCount(runtimeRoot)).toBe(2);
        expect(fetchSpy).not.toHaveBeenCalled();

        const recovered = await recoveryPromise;
        expect(recovered.statusCode, recovered.body).toBe(200);
        expect(recovered.json()).toMatchObject({
          recovered: true,
          runtimeSessionId: session.sessionId,
          runtimeRevision: session.runtimeRevision,
          configHash: session.configHash,
          recoveryCursor: session.recoveryCursor,
          providerCallsAdded: 0,
          externalActionsReplayed: false,
        });
        expect(second.repository.listEventsAfter(conversation.conversationId, 0)
          .filter((event) => event.type.startsWith("retry."))
          .map((event) => [event.type, event.payload.success ?? null])).toEqual([
          ["retry.started", null],
          ["retry.completed", true],
        ]);
      } finally {
        fetchSpy.mockRestore();
      }
    } finally {
      restoreProvider();
    }
  });

  it("rejects configuration drift after restart before events, files, processes, or Provider calls", async () => {
    const stateRoot = await mkdtemp(path.join(os.tmpdir(), "qf-openhands-drift-recovery-"));
    roots.push(stateRoot);
    const localRoot = path.resolve(".local");
    await mkdir(localRoot, { recursive: true });
    const sessionRoot = await mkdtemp(path.join(localRoot, "qf-openhands-drift-recovery-"));
    roots.push(sessionRoot);
    const config = runtimeConfig(stateRoot, sessionRoot);
    const restoreProvider = installFakeDeepSeekConfiguration();
    try {
      const first = create(config);
      await first.app.ready();
      const { conversation, session } = await seedCompletedOpenHandsConversation(first, "configuration drift gate");
      const runtimeRoot = runtimeStateRoot(sessionRoot, session);
      await waitUntil(async () => await sidecarStartCount(runtimeRoot) === 1);
      await closeApplication(first);

      const driftedConfig: RuntimeConfig = {
        ...config,
        systemPromptVersion: "qf-agent-p0.v1-drifted",
      };
      const second = create(driftedConfig);
      await second.app.ready();
      const beforeDatabase = durableDatabaseSnapshot(config.sqlitePath);
      const beforeFiles = {
        sessions: await durableFileSnapshot(sessionRoot),
        artifacts: await durableFileSnapshot(config.artifactRoot),
      };
      const fetchSpy = vi.spyOn(globalThis, "fetch");
      try {
        const response = await second.app.inject({
          method: "POST",
          url: `/api/conversations/${conversation.conversationId}/runtime/recover`,
        });
        expect(response.statusCode, response.body).toBe(409);
        expect(response.json()).toEqual({
          error: {
            category: "SECURITY",
            code: "OPENHANDS_RECOVER_EXACT_CONFIGURATION_MISMATCH",
            message: "OpenHands exact recovery configuration differs from the durable runtime identity.",
            retryable: false,
            recovery: "Do not create a replacement revision through the recovery endpoint; use the explicit model-change workflow after review.",
          },
        });
        expect(durableDatabaseSnapshot(config.sqlitePath)).toEqual(beforeDatabase);
        expect({
          sessions: await durableFileSnapshot(sessionRoot),
          artifacts: await durableFileSnapshot(config.artifactRoot),
        }).toEqual(beforeFiles);
        expect(await sidecarStartCount(runtimeRoot)).toBe(1);
        expect(fetchSpy).not.toHaveBeenCalled();
        expect(second.runtimeManager.isActive(conversation.conversationId)).toBe(false);
      } finally {
        fetchSpy.mockRestore();
      }
    } finally {
      restoreProvider();
    }
  });
});
