import { createHash } from "node:crypto";
import { access, mkdtemp, readFile, readlink, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { Type } from "typebox";
import { afterEach, describe, expect, it } from "vitest";

import type { JsonObject, JsonValue } from "@q-fintelligence/contracts";

import {
  OpenHandsRuntime,
  type OpenHandsRuntimeOptions,
} from "../../apps/control-plane/src/agent/openhands-runtime.js";
import type { RuntimeEmission } from "../../apps/control-plane/src/agent/runtime.js";
import { RuntimeAbortedError, RuntimeFailureError } from "../../apps/control-plane/src/agent/runtime.js";
import type { QfToolExecutionContext, QfToolSpec } from "../../apps/control-plane/src/agent/tools.js";
import { openDatabase } from "../../apps/control-plane/src/db/migrations.js";
import { WorkspaceRepository } from "../../apps/control-plane/src/db/repository.js";

const FAKE_SIDECAR = path.resolve("tests/fixtures/openhands-sidecar-fake.mjs");
const FORMAL_PROJECT_ROOT = path.resolve(".");
const FORMAL_SESSION_ROOT = path.join(FORMAL_PROJECT_ROOT, ".local", "openhands-sessions");
const FORMAL_SIDECAR = path.join(FORMAL_PROJECT_ROOT, "workers", "openhands_sidecar", "run-isolated.sh");
const temporaryRoots: string[] = [];
const formalConversationRoots: string[] = [];
const harnesses: AdapterHarness[] = [];

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function waitFor(condition: () => boolean, timeoutMs = 3_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (condition()) return;
    await delay(20);
  }
  throw new Error("timed out waiting for the fake sidecar state change");
}

function processExists(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

async function fileExists(target: string): Promise<boolean> {
  try {
    await access(target);
    return true;
  } catch {
    return false;
  }
}

function stableJson(value: unknown): string {
  if (value === null || typeof value === "boolean" || typeof value === "number" || typeof value === "string") {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  return `{${Object.entries(value as Record<string, unknown>)
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([key, entry]) => `${JSON.stringify(key)}:${stableJson(entry)}`).join(",")}}`;
}

function toolRequestHash(toolName: string, input: JsonObject): string {
  return createHash("sha256").update(stableJson({ toolName, arguments: input })).digest("hex");
}

class AdapterHarness {
  readonly sessionRoot: string;
  readonly repository: WorkspaceRepository;
  readonly conversationId: string;
  readonly runtimes: OpenHandsRuntime[] = [];
  toolExecutions = 0;
  private closed = false;

  private constructor(readonly root: string, repository: WorkspaceRepository, conversationId: string) {
    this.sessionRoot = path.join(root, "openhands-sessions");
    this.repository = repository;
    this.conversationId = conversationId;
  }

  static async create(): Promise<AdapterHarness> {
    const root = await mkdtemp(path.join(os.tmpdir(), "qf-openhands-adapter-test-"));
    temporaryRoots.push(root);
    const opened = openDatabase(path.join(root, "state.sqlite3"), path.resolve("infra/sqlite"));
    const repository = new WorkspaceRepository(opened.database);
    const project = repository.createProject("OpenHands Adapter test");
    const conversation = repository.createConversation({
      projectId: project.projectId,
      title: "isolated fake sidecar",
      mode: "OPENHANDS",
      provider: "deepseek",
      modelId: "deepseek-v4-pro",
    });
    const harness = new AdapterHarness(root, repository, conversation.conversationId);
    harnesses.push(harness);
    return harness;
  }

  private toolSpecs(): QfToolSpec[] {
    return [{
      name: "probe_tool",
      label: "Probe tool",
      description: "Return one deterministic result from the fake QF host.",
      parameters: Type.Object({}, { additionalProperties: false }),
      execute: async () => ({ status: "UNUSED_DIRECT_EXECUTOR" }),
    }];
  }

  private readonly toolExecutor = async (
    name: string,
    input: JsonObject,
    context: QfToolExecutionContext,
  ): Promise<JsonValue> => {
    context.signal.throwIfAborted();
    if (name !== "probe_tool" || Object.keys(input).length !== 0) throw new Error("fake tool boundary mismatch");
    this.toolExecutions += 1;
    return { status: "FAKE_TOOL_COMPLETED", execution: this.toolExecutions, toolCallId: context.toolCallId };
  };

  runtimeOptions(overrides: Partial<OpenHandsRuntimeOptions> = {}): OpenHandsRuntimeOptions {
    return {
      projectRoot: this.root,
      sessionRoot: this.sessionRoot,
      sidecarPath: process.execPath,
      sidecarArguments: [FAKE_SIDECAR],
      promptVersion: "qf-openhands-adapter-test.v1",
      provider: "deepseek",
      modelId: "deepseek-v4-pro",
      baseUrl: "https://fake-provider.invalid/compatible-mode/v1",
      apiKey: "fake-adapter-key",
      repository: this.repository,
      conversationId: this.conversationId,
      systemPrompt: "QF deterministic fake OpenHands Adapter prompt.",
      toolNames: ["probe_tool"],
      toolSpecs: this.toolSpecs(),
      toolExecutor: this.toolExecutor,
      ...overrides,
    };
  }

  async createRuntime(overrides: Partial<OpenHandsRuntimeOptions> = {}): Promise<OpenHandsRuntime> {
    const options = this.runtimeOptions(overrides);
    const runtime = await OpenHandsRuntime.create(options);
    this.runtimes.push(runtime);
    return runtime;
  }

  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    for (const runtime of this.runtimes) runtime.dispose();
    await delay(150);
    this.repository.close();
  }
}

afterEach(async () => {
  await Promise.all(harnesses.splice(0).map((harness) => harness.close()));
  await Promise.all(temporaryRoots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
  await Promise.all(formalConversationRoots.splice(0).map(async (root) => {
    if (!root.startsWith(`${FORMAL_SESSION_ROOT}${path.sep}`)) {
      throw new Error("refused to clean an OpenHands race-test path outside the formal session root");
    }
    await rm(root, { recursive: true, force: true });
  }));
});

describe("OpenHands Runtime Adapter with an isolated fake sidecar", () => {
  it("returns normal text, streams deltas, and deduplicates repeated persistent event IDs", async () => {
    const harness = await AdapterHarness.create();
    const runtime = await harness.createRuntime();
    expect(harness.repository.getOpenHandsWorkspace(harness.conversationId)).toMatchObject({
      runtimeSessionId: runtime.runtimeSessionId,
      manifestHash: "e".repeat(64),
      backend: "hardened-docker-agent-server",
      hostLocalWorkspace: false,
      workspaceId: runtime.runtimeSessionId,
      imageDigest: `sha256:${"d".repeat(64)}`,
      secretEnvironmentNames: [],
      snapshot: {
        schemaVersion: "qf.openhands-workspace-snapshot.v1",
        snapshotSha256: "b".repeat(64),
      },
    });
    const events: RuntimeEmission[] = [];
    const result = await runtime.prompt("NORMAL_TEXT", async (event) => { events.push(event); });

    expect(result).toMatchObject({
      assistantContent: "FAKE_OPENHANDS_OK",
      provider: "deepseek",
      modelId: "deepseek-v4-pro",
      usage: { input: 3, output: 2, totalTokens: 5 },
    });
    expect(events.filter((event) => event.type === "agent.started")).toHaveLength(1);
    expect(events.filter((event) => event.type === "assistant.delta")
      .map((event) => String(event.payload.text)).join(""))
      .toBe("FAKE_OPENHANDS_OK");
  });

  it("keeps WORK on the exact tool and CHAT on a zero-tool route", async () => {
    const harness = await AdapterHarness.create();
    const runtime = await harness.createRuntime();
    const workEvents: RuntimeEmission[] = [];
    const work = await runtime.prompt(
      "WORK_TOOL",
      async (event) => { workEvents.push(event); },
      { toolNames: ["probe_tool"] },
    );
    expect(work.assistantContent).toBe("FAKE_TOOL_OK");
    expect(harness.toolExecutions).toBe(1);
    expect(workEvents.map((event) => event.type)).toEqual(expect.arrayContaining(["tool.started", "tool.completed"]));

    const chatEvents: RuntimeEmission[] = [];
    const chat = await runtime.prompt(
      "CHAT_ZERO",
      async (event) => { chatEvents.push(event); },
      { toolNames: [] },
    );
    expect(chat.assistantContent).toBe("FAKE_OPENHANDS_OK");
    expect(harness.toolExecutions).toBe(1);
    expect(chatEvents.some((event) => event.type.startsWith("tool."))).toBe(false);
    expect(chatEvents.find((event) => event.type === "agent.started")?.payload.toolNames).toEqual([]);
  });

  it("deduplicates the same persisted backend event ID after runtime recovery", async () => {
    const harness = await AdapterHarness.create();
    const first = await harness.createRuntime();
    const firstEvents: RuntimeEmission[] = [];
    await first.prompt("DURABLE_EVENT", async (event) => {
      firstEvents.push(event);
      if (event.persistent) harness.repository.appendEvent({
        conversationId: harness.conversationId,
        type: event.type,
        payload: event.payload,
      });
    });
    expect(firstEvents.filter((event) => event.type === "agent.started")).toHaveLength(1);
    first.dispose();
    await delay(50);

    const recovered = await harness.createRuntime();
    expect(recovered.runtimeSessionId).toBe(first.runtimeSessionId);
    const recoveredEvents: RuntimeEmission[] = [];
    await recovered.prompt("DURABLE_EVENT", async (event) => { recoveredEvents.push(event); });
    expect(recoveredEvents.filter((event) => event.type === "agent.started")).toHaveLength(0);
    expect(recoveredEvents.filter((event) => event.type === "assistant.delta")).toHaveLength(2);
  });

  it("aborts only the active prompt and surfaces RuntimeAbortedError", async () => {
    const harness = await AdapterHarness.create();
    const runtime = await harness.createRuntime();
    const prompt = runtime.prompt("LONG_ABORT", async () => undefined);
    const rejected = expect(prompt).rejects.toBeInstanceOf(RuntimeAbortedError);
    await delay(25);
    await runtime.abort();
    await rejected;
    expect(harness.toolExecutions).toBe(0);
  });

  it("restarts only the killed sidecar and proves an exact zero-prompt recovery handshake", async () => {
    const harness = await AdapterHarness.create();
    const started: Array<{ pid: number; processKey: string }> = [];
    const exited: Array<{ pid: number; processKey: string }> = [];
    const runtime = await harness.createRuntime({
      onSidecarStarted: (identity) => { started.push(identity); },
      onSidecarExited: (identity) => { exited.push(identity); },
    });
    const beforeSession = harness.repository.getSession(harness.conversationId)!;
    const beforeProcess = runtime.sidecarProcess!;

    process.kill(beforeProcess.pid, "SIGTERM");
    await waitFor(() => runtime.sidecarProcess === null && !processExists(beforeProcess.pid));
    await waitFor(() => exited.some((identity) => identity.pid === beforeProcess.pid));

    const recovered = await runtime.recoverSidecar();
    expect(recovered).toMatchObject({
      restarted: true,
      runtimeSessionId: beforeSession.sessionId,
      runtimeRevision: beforeSession.runtimeRevision,
      configHash: beforeSession.configHash,
      recoveryCursor: beforeSession.recoveryCursor,
      providerPromptRequestsThisProcess: 0,
      executionStatus: "IDLE",
    });
    expect(recovered.process.pid).not.toBe(beforeProcess.pid);
    expect(recovered.process.processKey).toBe(beforeProcess.processKey);
    expect(started.map((identity) => identity.pid)).toEqual([beforeProcess.pid, recovered.process.pid]);
    expect(harness.repository.getSession(harness.conversationId)).toEqual(beforeSession);
    expect(harness.toolExecutions).toBe(0);
  });

  it("contains a sidecar-registration failure without an unhandled rejection or leaked child", async () => {
    const harness = await AdapterHarness.create();
    const started: number[] = [];
    const unhandled: unknown[] = [];
    const onUnhandled = (reason: unknown) => { unhandled.push(reason); };
    process.on("unhandledRejection", onUnhandled);
    try {
      await expect(OpenHandsRuntime.create(harness.runtimeOptions({
        onSidecarStarted: (identity) => {
          started.push(identity.pid);
          throw new Error("TEST_REGISTRY_REJECTED_SIDECAR");
        },
      }))).rejects.toThrow("TEST_REGISTRY_REJECTED_SIDECAR");
      expect(started).toHaveLength(1);
      await waitFor(() => !processExists(started[0]!));
      await delay(50);
      expect(unhandled).toEqual([]);
    } finally {
      process.off("unhandledRejection", onUnhandled);
    }
  });

  it("waits for real run-isolated Bash to exec bwrap before registration and withholds init", async () => {
    const harness = await AdapterHarness.create();
    const conversationRoot = path.join(FORMAL_SESSION_ROOT, harness.conversationId);
    formalConversationRoots.push(conversationRoot);
    let observedPid = 0;
    let observedStateRoot = "";
    let callbackFinished = false;

    await expect(OpenHandsRuntime.create(harness.runtimeOptions({
      projectRoot: FORMAL_PROJECT_ROOT,
      sessionRoot: FORMAL_SESSION_ROOT,
      sidecarPath: FORMAL_SIDECAR,
      sidecarArguments: [],
      apiKey: "fake-race-key-that-must-not-cross-init",
      onSidecarStarted: async (identity) => {
        observedPid = identity.pid;
        observedStateRoot = path.join(conversationRoot, identity.runtimeSessionId);
        const [executable, commandLineBytes] = await Promise.all([
          readlink(`/proc/${identity.pid}/exe`),
          readFile(`/proc/${identity.pid}/cmdline`),
        ]);
        const commandLine = commandLineBytes.toString("utf8").split("\0").filter(Boolean);
        expect(executable).toBe("/usr/bin/bwrap");
        expect(path.basename(commandLine[0] ?? "")).toBe("bwrap");
        expect(commandLine).toEqual(expect.arrayContaining([
          "--die-with-parent",
          "--unshare-pid",
          "--hostname",
          "qf-openhands-sidecar",
          "/opt/.venv/bin/python",
          "-I",
          "/opt/main.py",
        ]));

        // Keep the callback pending long enough that a concurrent init would
        // persist the sidecar-owned manifest. Its absence proves init remains
        // ordered strictly after successful registration.
        await delay(300);
        expect(await fileExists(path.join(observedStateRoot, "qf-runtime-manifest.json"))).toBe(true);
        expect(await fileExists(path.join(observedStateRoot, "runtime-manifest.json"))).toBe(false);
        callbackFinished = true;
        throw new Error("TEST_REAL_BWRAP_REGISTRATION_REJECTED");
      },
    }))).rejects.toThrow("TEST_REAL_BWRAP_REGISTRATION_REJECTED");

    expect(callbackFinished).toBe(true);
    expect(observedPid).toBeGreaterThan(1);
    await waitFor(() => !processExists(observedPid));
    expect(await fileExists(path.join(observedStateRoot, "runtime-manifest.json"))).toBe(false);
  }, 45_000);

  it("preflights RECOVER_EXACT before writes or process start and reconstructs the same durable identity", async () => {
    const harness = await AdapterHarness.create();
    const started: number[] = [];
    const onSidecarStarted: NonNullable<OpenHandsRuntimeOptions["onSidecarStarted"]> = (identity) => {
      started.push(identity.pid);
    };
    const options = harness.runtimeOptions({
      onSidecarStarted,
    });
    const first = await harness.createRuntime({
      onSidecarStarted,
    });
    const expected = harness.repository.getSession(harness.conversationId)!;
    const manifestPath = path.join(harness.sessionRoot, expected.relativeSessionFile!, "qf-runtime-manifest.json");
    const manifestBefore = await readFile(manifestPath, "utf8");
    first.dispose();
    await waitFor(() => first.sidecarProcess === null);
    const startsBeforeDrift = started.length;

    await expect(OpenHandsRuntime.prepareExactRecovery(harness.runtimeOptions({
      promptVersion: "qf-openhands-adapter-test.drift",
      systemPrompt: "QF deterministic drift that must not create a replacement revision.",
      onSidecarStarted,
    }), expected)).rejects.toMatchObject({
      code: "OPENHANDS_RECOVER_EXACT_CONFIGURATION_MISMATCH",
      category: "SECURITY",
    });
    expect(started).toHaveLength(startsBeforeDrift);
    expect(harness.repository.getSession(harness.conversationId)).toEqual(expected);
    expect(await readFile(manifestPath, "utf8")).toBe(manifestBefore);

    const reconstructed = await OpenHandsRuntime.prepareExactRecovery(options, expected);
    harness.runtimes.push(reconstructed);
    const recovered = await reconstructed.recoverSidecar();
    expect(recovered).toMatchObject({
      restarted: true,
      runtimeSessionId: expected.sessionId,
      runtimeRevision: expected.runtimeRevision,
      configHash: expected.configHash,
      recoveryCursor: expected.recoveryCursor,
      providerPromptRequestsThisProcess: 0,
    });
    expect(harness.repository.getSession(harness.conversationId)).toEqual(expected);
    expect(harness.toolExecutions).toBe(0);
  });

  it("creates a new revision for config drift and fails closed on manifest drift", async () => {
    const harness = await AdapterHarness.create();
    const first = await harness.createRuntime();
    const firstSessionId = first.runtimeSessionId;
    const firstConfigHash = first.configHash;
    first.dispose();
    await delay(50);

    const driftOptions = {
      promptVersion: "qf-openhands-adapter-test.v2",
      systemPrompt: "QF deterministic fake OpenHands Adapter prompt revision two.",
    } satisfies Partial<OpenHandsRuntimeOptions>;
    const revised = await harness.createRuntime(driftOptions);
    expect(revised.runtimeSessionId).not.toBe(firstSessionId);
    expect(revised.configHash).not.toBe(firstConfigHash);
    expect(revised.runtimeRevision).toMatch(/\/r2$/u);

    const session = harness.repository.getSession(harness.conversationId)!;
    const manifestPath = path.join(harness.sessionRoot, session.relativeSessionFile!, "qf-runtime-manifest.json");
    const manifest = JSON.parse(await readFile(manifestPath, "utf8")) as JsonObject;
    manifest.adapterVersion = "tampered-adapter";
    await writeFile(manifestPath, `${JSON.stringify(manifest)}\n`, "utf8");
    revised.dispose();
    await delay(50);

    await expect(harness.createRuntime(driftOptions)).rejects.toMatchObject({
      code: "OPENHANDS_RECOVERY_MANIFEST_MISMATCH",
      category: "SECURITY",
    });
  });

  it("replays a completed stable tool call after a sidecar crash without executing the Host twice", async () => {
    const harness = await AdapterHarness.create();
    const first = await harness.createRuntime();
    await expect(first.prompt("WORK_TOOL STABLE CRASH_AFTER_RESULT", async () => undefined))
      .rejects.toBeInstanceOf(RuntimeFailureError);
    expect(harness.toolExecutions).toBe(1);
    expect(harness.repository.replayOpenHandsToolCall(
      harness.conversationId,
      first.runtimeSessionId,
      "stable-tool-call",
    )).toMatchObject({ status: "COMPLETED" });

    const recovered = await harness.createRuntime();
    expect(recovered.runtimeSessionId).toBe(first.runtimeSessionId);
    const result = await recovered.prompt("WORK_TOOL STABLE", async () => undefined);
    expect(result.assistantContent).toBe("FAKE_TOOL_OK");
    expect(harness.toolExecutions).toBe(1);
  });

  it("fails an UNKNOWN stable tool call closed and never re-executes the Host", async () => {
    const harness = await AdapterHarness.create();
    const runtime = await harness.createRuntime();
    expect(harness.repository.claimOpenHandsToolCall({
      conversationId: harness.conversationId,
      runtimeSessionId: runtime.runtimeSessionId,
      toolCallId: "stable-tool-call",
      toolName: "probe_tool",
      requestHash: toolRequestHash("probe_tool", {}),
    }).disposition).toBe("CLAIMED");
    expect(harness.repository.markOpenHandsToolCallsUnknown(harness.conversationId, runtime.runtimeSessionId)).toBe(1);

    const events: RuntimeEmission[] = [];
    const result = await runtime.prompt("WORK_TOOL STABLE", async (event) => { events.push(event); });
    expect(result.assistantContent).toBe("FAKE_TOOL_FAILED_CLOSED");
    expect(harness.toolExecutions).toBe(0);
    expect(events.find((event) => event.type === "tool.completed")?.payload.isError).toBe(true);
    expect(harness.repository.listOpenHandsToolCallsForRecovery(
      harness.conversationId,
      runtime.runtimeSessionId,
    )).toEqual([expect.objectContaining({ toolCallId: "stable-tool-call", status: "UNKNOWN" })]);
  });

  it("creates a fresh runtime session and increments the revision when the selected model changes", async () => {
    const harness = await AdapterHarness.create();
    const first = await harness.createRuntime();
    first.dispose();
    await delay(50);
    harness.repository.updateConversationModel(harness.conversationId, "openai", "gpt-5.6");

    const switched = await harness.createRuntime({ provider: "openai", modelId: "gpt-5.6" });
    expect(switched.runtimeSessionId).not.toBe(first.runtimeSessionId);
    expect(switched.configHash).not.toBe(first.configHash);
    expect(switched.runtimeRevision).toMatch(/\/r2$/u);
    const result = await switched.prompt("MODEL_SWITCH", async () => undefined);
    expect(result).toMatchObject({ provider: "openai", modelId: "gpt-5.6" });
  });
});
