import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { mkdir, readFile, realpath, writeFile } from "node:fs/promises";
import path from "node:path";
import { createInterface } from "node:readline";

import type { AgentUiEventType, JsonObject, JsonValue } from "@q-fintelligence/contracts";

import type { WorkspaceRepository } from "../db/repository.js";
import { redactSecrets, safeErrorMessage } from "../security/redaction.js";
import { FULL_QF_SYSTEM_PROMPT, systemPromptHash } from "./system-prompt.js";
import {
  createQfToolSpecs,
  qfToolSpecsForIpc,
  QF_TOOL_NAMES,
  type QfToolHost,
  type QfToolExecutionContext,
  type QfToolName,
  type QfToolSpec,
  validateQfToolArguments,
} from "./tools.js";
import type {
  AgentRuntime,
  RuntimeEmission,
  RuntimeEventSink,
  RuntimePromptOptions,
  RuntimeResult,
} from "./runtime.js";
import { RuntimeAbortedError, RuntimeControlError, RuntimeFailureError } from "./runtime.js";

export const OPENHANDS_SDK_VERSION = "1.39.0+qf.noobservability.1";
export const OPENHANDS_SIDECAR_VERSION = "qf-openhands-sidecar.v3";
export const OPENHANDS_ADAPTER_VERSION = "qf-openhands-adapter.v2";
export const OPENHANDS_PROTOCOL_VERSION = "qf.agent-runtime.v2";
export const OPENHANDS_WHEEL_SHA256 = "9818384aa3393524ed05574fba041009a91f6e2ce35db77d86cd1a600a6e70e3";

const MAX_FRAME_BYTES = 2 * 1024 * 1024;
const MAX_REPLAY_RESULT_BYTES = 256 * 1024;
const READY_TIMEOUT_MS = 60_000;
const INIT_TIMEOUT_MS = 120_000;
const PROMPT_TIMEOUT_MS = 20 * 60_000;
const QUEUE_TIMEOUT_MS = 10_000;

const PERSISTENT_EVENT_TYPES = new Set<AgentUiEventType>([
  "agent.started",
  "agent.completed",
  "agent.aborted",
  "turn.started",
  "turn.completed",
  "tool.started",
  "tool.progress",
  "tool.completed",
  "run.failed",
  "queue.changed",
]);

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (error: unknown) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

interface PendingRequest {
  resolve: (value: JsonObject) => void;
  reject: (error: unknown) => void;
  timer: ReturnType<typeof setTimeout>;
}

export interface SidecarFrame extends JsonObject {
  protocol: string;
  type: string;
  requestId?: string;
}

interface RuntimeManifest extends JsonObject {
  schemaVersion: "qf.openhands-runtime-manifest.v1";
  adapterVersion: string;
  sidecarVersion: string;
  sdkVersion: string;
  sdkWheelSha256: string;
  protocolVersion: string;
  runtimeSessionId: string;
  provider: string;
  modelId: string;
  baseUrlSha256: string;
  promptVersion: string;
  promptHash: string;
  toolSpecHash: string;
  configHash: string;
  agentContextDateTime: string;
}

export interface OpenHandsRuntimeOptions {
  projectRoot: string;
  sessionRoot: string;
  sidecarPath: string;
  sidecarArguments?: readonly string[];
  promptVersion: string;
  provider: string;
  modelId: string;
  baseUrl: string;
  apiKey: string;
  repository: WorkspaceRepository;
  host?: QfToolHost;
  conversationId: string;
  systemPrompt?: string;
  systemPromptHash?: string;
  toolNames?: readonly string[];
  toolSpecs?: readonly QfToolSpec[];
  toolExecutor?: (name: string, input: JsonObject, context: QfToolExecutionContext) => Promise<JsonValue>;
  maxOutputTokens?: number;
  maxIterations?: number;
  onSidecarStarted?: (identity: OpenHandsSidecarProcessIdentity) => Promise<void> | void;
  onSidecarExited?: (identity: OpenHandsSidecarProcessIdentity) => Promise<void> | void;
}

export interface OpenHandsSidecarProcessIdentity {
  processKey: string;
  pid: number;
  kind: "OPENHANDS_SIDECAR";
  killMode: "PID";
  conversationId: string;
  runtimeSessionId: string;
  runtimeRevision: string;
  configHash: string;
}

export interface OpenHandsRecoveryResult {
  restarted: boolean;
  process: OpenHandsSidecarProcessIdentity;
  runtimeSessionId: string;
  runtimeRevision: string;
  configHash: string;
  recoveryCursor: number;
  executionStatus: string;
  sdkEventCount: number;
  providerPromptRequestsThisProcess: 0;
}

export interface OpenHandsExactRecoveryExpectation {
  runtimeKind: "PI" | "MOCK" | "OPENHANDS";
  sessionId: string;
  runtimeRevision: string;
  configHash: string;
  recoveryCursor: number;
  provider: string;
  modelId: string;
  promptVersion: string;
  promptHash: string;
  relativeSessionFile: string | null;
}

function stableJson(value: unknown): string {
  if (value === null || typeof value === "boolean" || typeof value === "string") return JSON.stringify(value);
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new Error("non-finite values are not valid protocol JSON");
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>)
      .filter(([, entry]) => entry !== undefined)
      .sort(([left], [right]) => left.localeCompare(right));
    return `{${entries.map(([key, entry]) => `${JSON.stringify(key)}:${stableJson(entry)}`).join(",")}}`;
  }
  throw new Error("unsupported protocol JSON value");
}

function sha256(value: unknown): string {
  return createHash("sha256").update(typeof value === "string" ? value : stableJson(value)).digest("hex");
}

function assertSameToolNames(actualNames: readonly string[], expectedNames: readonly string[]): void {
  const actual = [...new Set(actualNames)].sort();
  const expected = [...new Set(expectedNames)].sort();
  if (actual.length !== actualNames.length || expected.length !== expectedNames.length || stableJson(actual) !== stableJson(expected)) {
    throw new Error(`OpenHands tool boundary mismatch: expected ${expected.join(",")}; got ${actual.join(",")}`);
  }
}

function jsonValue(value: unknown, depth = 0): JsonValue {
  if (depth > 10) return "[truncated]";
  if (value === null || typeof value === "string" || typeof value === "boolean") return value;
  if (typeof value === "number") return Number.isFinite(value) ? value : String(value);
  if (Array.isArray(value)) return value.slice(0, 500).map((entry) => jsonValue(entry, depth + 1));
  if (typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .slice(0, 500)
        .map(([key, entry]) => [key, jsonValue(entry, depth + 1)]),
    );
  }
  return String(value);
}

function jsonObject(value: unknown, label: string): JsonObject {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error(`${label} must be a JSON object`);
  }
  return jsonValue(value) as JsonObject;
}

function withTimeout<T>(promise: Promise<T>, milliseconds: number, label: string): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`${label} timed out`)), milliseconds);
    timer.unref();
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (error: unknown) => {
        clearTimeout(timer);
        reject(error);
      },
    );
  });
}

function revisionNumber(value: string | undefined): number {
  const matched = value?.match(/\/r(\d+)$/u);
  return matched ? Number(matched[1]) : 0;
}

interface PreparedRuntimeDefinition {
  projectRoot: string;
  sessionRoot: string;
  specs: QfToolSpec[];
  selectedPromptHash: string;
  configHash: string;
  configInput: Pick<
    RuntimeManifest,
    | "adapterVersion"
    | "sidecarVersion"
    | "sdkVersion"
    | "sdkWheelSha256"
    | "protocolVersion"
    | "provider"
    | "modelId"
    | "baseUrlSha256"
    | "promptVersion"
    | "promptHash"
    | "toolSpecHash"
  >;
}

async function prepareRuntimeDefinition(
  options: OpenHandsRuntimeOptions,
  createSessionRoot: boolean,
): Promise<PreparedRuntimeDefinition> {
  if (!options.apiKey || !options.baseUrl.startsWith("https://")) {
    throw new RuntimeFailureError(
      "OpenHands Provider configuration is missing or not HTTPS.",
      "OPENHANDS_PROVIDER_CONFIGURATION_INVALID",
      false,
      "PROVIDER",
      "Configure the explicitly selected Provider without enabling fallback.",
    );
  }
  const projectRoot = await realpath(options.projectRoot);
  if (createSessionRoot) await mkdir(options.sessionRoot, { recursive: true, mode: 0o700 });
  const sessionRoot = await realpath(options.sessionRoot);
  if (!sessionRoot.startsWith(`${projectRoot}${path.sep}`)) {
    throw new Error("OpenHands session root escaped the QF project");
  }
  const selectedNames = options.toolNames ?? options.toolSpecs?.map((spec) => spec.name) ?? QF_TOOL_NAMES;
  const specs = options.toolSpecs
    ? [...options.toolSpecs]
    : options.host
      ? createQfToolSpecs(options.host, selectedNames as readonly QfToolName[])
      : [];
  const actualNames = [...new Set(specs.map((spec) => spec.name))].sort();
  const expectedNames = [...new Set(selectedNames)].sort();
  if (stableJson(actualNames) !== stableJson(expectedNames) || specs.length === 0) {
    throw new Error("OpenHands runtime tool specs do not match the requested allowlist");
  }
  const selectedPrompt = options.systemPrompt ?? FULL_QF_SYSTEM_PROMPT;
  const selectedPromptHash = options.systemPromptHash ?? systemPromptHash(selectedPrompt);
  if (sha256(selectedPrompt) !== selectedPromptHash) throw new Error("OpenHands system prompt hash mismatch");
  const configInput: PreparedRuntimeDefinition["configInput"] = {
    adapterVersion: OPENHANDS_ADAPTER_VERSION,
    sidecarVersion: OPENHANDS_SIDECAR_VERSION,
    sdkVersion: OPENHANDS_SDK_VERSION,
    sdkWheelSha256: OPENHANDS_WHEEL_SHA256,
    protocolVersion: OPENHANDS_PROTOCOL_VERSION,
    provider: options.provider,
    modelId: options.modelId,
    baseUrlSha256: sha256(options.baseUrl.replace(/\/$/u, "")),
    promptVersion: options.promptVersion,
    promptHash: selectedPromptHash,
    toolSpecHash: sha256(qfToolSpecsForIpc(specs)),
  };
  return {
    projectRoot,
    sessionRoot,
    specs,
    selectedPromptHash,
    configHash: sha256(configInput),
    configInput,
  };
}

export function mapOpenHandsEvent(frame: SidecarFrame): RuntimeEmission | null {
  if (frame.type !== "event" || typeof frame.eventType !== "string") return null;
  if (frame.eventType === "assistant.delta") {
    const payload = jsonObject(frame.payload ?? {}, "OpenHands event payload");
    const eventId = typeof frame.eventId === "string" ? frame.eventId : "missing";
    if (payload.streamMode === "persisted-message" && eventId !== "missing") {
      return {
        type: "assistant.delta",
        payload: { ...payload, backendEventId: eventId },
        persistent: true,
      };
    }
    return {
      type: "assistant.delta",
      payload,
      persistent: false,
    };
  }
  if (!PERSISTENT_EVENT_TYPES.has(frame.eventType as AgentUiEventType)) return null;
  const eventId = typeof frame.eventId === "string" ? frame.eventId : "missing";
  return {
    type: frame.eventType as AgentUiEventType,
    payload: {
      ...jsonObject(frame.payload ?? {}, "OpenHands event payload"),
      backendEventId: eventId,
    },
    persistent: true,
  };
}

export class OpenHandsRuntime implements AgentRuntime {
  readonly kind = "OPENHANDS" as const;
  readonly toolNames: readonly string[];
  readonly runtimeSessionId: string;
  readonly configHash: string;
  readonly runtimeRevision: string;

  private readonly specs: QfToolSpec[];
  private readonly specByName: Map<string, QfToolSpec>;
  private readonly stateRoot: string;
  private readonly manifest: RuntimeManifest;
  private readonly seenBackendEventIds = new Set<string>();
  private readonly pending = new Map<string, PendingRequest>();
  private readonly toolControllers = new Map<string, AbortController>();
  private readonly eventBacklog: SidecarFrame[] = [];
  private child: ChildProcessWithoutNullStreams | null = null;
  private sidecarExitCleanup: Promise<void> | null = null;
  private ready: Deferred<JsonObject> | null = null;
  private eventQueue: Promise<void> = Promise.resolve();
  private sinkFailure: unknown = null;
  private activeSink: RuntimeEventSink | null = null;
  private activeToolNames = new Set<string>();
  private recoveryCursor: number;
  private stderrTail = "";
  private disposed = false;
  private starting: Promise<void> | null = null;
  private readonly exactRecovery: boolean;
  private recoveryHandshakeRequested = false;

  private constructor(private readonly options: OpenHandsRuntimeOptions, input: {
    runtimeSessionId: string;
    stateRoot: string;
    configHash: string;
    runtimeRevision: string;
    manifest: RuntimeManifest;
    recoveryCursor: number;
    specs: QfToolSpec[];
    exactRecovery?: boolean;
  }) {
    this.runtimeSessionId = input.runtimeSessionId;
    this.stateRoot = input.stateRoot;
    this.configHash = input.configHash;
    this.runtimeRevision = input.runtimeRevision;
    this.manifest = input.manifest;
    this.recoveryCursor = input.recoveryCursor;
    this.specs = input.specs;
    this.exactRecovery = input.exactRecovery === true;
    this.specByName = new Map(input.specs.map((spec) => [spec.name, spec]));
    this.toolNames = input.specs.map((spec) => spec.name);
    for (const event of options.repository.listEventsAfter(options.conversationId, 0)) {
      const eventId = event.payload.backendEventId;
      if (typeof eventId === "string") this.seenBackendEventIds.add(eventId);
    }
  }

  static async create(options: OpenHandsRuntimeOptions): Promise<OpenHandsRuntime> {
    const {
      sessionRoot,
      specs,
      selectedPromptHash,
      configHash,
      configInput,
    } = await prepareRuntimeDefinition(options, true);
    const existing = options.repository.getSession(options.conversationId);
    const reuse = existing?.runtimeKind === "OPENHANDS"
      && existing.configHash === configHash
      && existing.provider === options.provider
      && existing.modelId === options.modelId
      && existing.promptHash === selectedPromptHash
      && /^[0-9a-f-]{36}$/iu.test(existing.sessionId);
    const runtimeSessionId = reuse ? existing.sessionId : randomUUID();
    const runtimeRevision = reuse
      ? existing.runtimeRevision
      : `${OPENHANDS_ADAPTER_VERSION}/${OPENHANDS_SDK_VERSION}/r${revisionNumber(existing?.runtimeRevision) + 1}`;
    const stateRoot = path.join(sessionRoot, options.conversationId, runtimeSessionId);
    await mkdir(stateRoot, { recursive: true, mode: 0o700 });
    const resolvedStateRoot = await realpath(stateRoot);
    if (!resolvedStateRoot.startsWith(`${sessionRoot}${path.sep}`)) throw new Error("OpenHands state path escaped its root");
    const manifestPath = path.join(resolvedStateRoot, "qf-runtime-manifest.json");
    let storedManifest: unknown = null;
    if (reuse) {
      try {
        storedManifest = JSON.parse(await readFile(manifestPath, "utf8")) as unknown;
      } catch (error) {
        throw new RuntimeFailureError(
          `OpenHands recovery manifest is unavailable: ${safeErrorMessage(error)}`,
          "OPENHANDS_RECOVERY_MANIFEST_MISSING",
          false,
          "SECURITY",
          "Keep the conversation read-only and inspect its isolated runtime state.",
        );
      }
    }
    const storedAgentContextDateTime = reuse
      && typeof storedManifest === "object"
      && storedManifest !== null
      && !Array.isArray(storedManifest)
      && typeof (storedManifest as Record<string, unknown>).agentContextDateTime === "string"
      ? (storedManifest as Record<string, string>).agentContextDateTime
      : null;
    const manifest: RuntimeManifest = {
      schemaVersion: "qf.openhands-runtime-manifest.v1",
      ...configInput,
      runtimeSessionId,
      configHash,
      agentContextDateTime: storedAgentContextDateTime ?? new Date().toISOString(),
    };
    if (reuse) {
      if (storedAgentContextDateTime === null || stableJson(storedManifest) !== stableJson(manifest)) {
        throw new RuntimeFailureError(
          "OpenHands recovery manifest differs from the durable QF session metadata.",
          "OPENHANDS_RECOVERY_MANIFEST_MISMATCH",
          false,
          "SECURITY",
          "Do not resume this runtime until the configuration drift is independently reviewed.",
        );
      }
    } else {
      await writeFile(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`, { encoding: "utf8", mode: 0o600, flag: "wx" });
    }
    options.repository.upsertOpenHandsSession({
      conversationId: options.conversationId,
      sessionId: runtimeSessionId,
      relativeSessionFile: path.relative(sessionRoot, resolvedStateRoot),
      provider: options.provider,
      modelId: options.modelId,
      promptVersion: options.promptVersion,
      promptHash: selectedPromptHash,
      lifecycleState: "STARTING",
      runtimeRevision,
      configHash,
      recoveryCursor: reuse ? existing.recoveryCursor : 0,
    });
    options.repository.markOpenHandsToolCallsUnknown(options.conversationId, runtimeSessionId);
    const runtime = new OpenHandsRuntime(options, {
      runtimeSessionId,
      stateRoot: resolvedStateRoot,
      configHash,
      runtimeRevision,
      manifest,
      recoveryCursor: reuse ? existing.recoveryCursor : 0,
      specs,
    });
    await runtime.ensureStarted();
    return runtime;
  }

  static async prepareExactRecovery(
    options: OpenHandsRuntimeOptions,
    expected: OpenHandsExactRecoveryExpectation,
  ): Promise<OpenHandsRuntime> {
    const {
      sessionRoot,
      specs,
      selectedPromptHash,
      configHash,
      configInput,
    } = await prepareRuntimeDefinition(options, false);
    if (
      expected.runtimeKind !== "OPENHANDS"
      || expected.provider !== options.provider
      || expected.modelId !== options.modelId
      || expected.promptVersion !== options.promptVersion
      || expected.promptHash !== selectedPromptHash
      || expected.configHash !== configHash
      || !expected.runtimeRevision.trim()
      || !Number.isSafeInteger(expected.recoveryCursor)
      || expected.recoveryCursor < 0
      || !/^[0-9a-f-]{36}$/iu.test(expected.sessionId)
      || expected.relativeSessionFile === null
      || path.isAbsolute(expected.relativeSessionFile)
    ) {
      throw new RuntimeFailureError(
        "OpenHands exact recovery configuration differs from the durable runtime identity.",
        "OPENHANDS_RECOVER_EXACT_CONFIGURATION_MISMATCH",
        false,
        "SECURITY",
        "Do not create a replacement revision through the recovery endpoint; use the explicit model-change workflow after review.",
      );
    }
    const requestedStateRoot = path.resolve(sessionRoot, expected.relativeSessionFile);
    const resolvedStateRoot = await realpath(requestedStateRoot);
    const normalizedRelative = path.relative(sessionRoot, resolvedStateRoot);
    if (
      !resolvedStateRoot.startsWith(`${sessionRoot}${path.sep}`)
      || normalizedRelative !== path.normalize(expected.relativeSessionFile)
    ) {
      throw new RuntimeFailureError(
        "OpenHands exact recovery state path differs from the durable relative path.",
        "OPENHANDS_RECOVER_EXACT_PATH_MISMATCH",
        false,
        "SECURITY",
        "Keep the conversation read-only and inspect the isolated runtime state path.",
      );
    }
    let storedManifest: unknown;
    try {
      storedManifest = JSON.parse(await readFile(
        path.join(resolvedStateRoot, "qf-runtime-manifest.json"),
        "utf8",
      )) as unknown;
    } catch (error) {
      throw new RuntimeFailureError(
        `OpenHands exact recovery manifest is unavailable: ${safeErrorMessage(error)}`,
        "OPENHANDS_RECOVERY_MANIFEST_MISSING",
        false,
        "SECURITY",
        "Keep the conversation read-only and inspect its isolated runtime state.",
      );
    }
    const agentContextDateTime = typeof storedManifest === "object"
      && storedManifest !== null
      && !Array.isArray(storedManifest)
      && typeof (storedManifest as Record<string, unknown>).agentContextDateTime === "string"
      ? (storedManifest as Record<string, string>).agentContextDateTime
      : null;
    const manifest: RuntimeManifest = {
      schemaVersion: "qf.openhands-runtime-manifest.v1",
      ...configInput,
      runtimeSessionId: expected.sessionId,
      configHash,
      agentContextDateTime: agentContextDateTime ?? "",
    };
    if (agentContextDateTime === null || stableJson(storedManifest) !== stableJson(manifest)) {
      throw new RuntimeFailureError(
        "OpenHands exact recovery manifest differs from the durable QF session metadata.",
        "OPENHANDS_RECOVERY_MANIFEST_MISMATCH",
        false,
        "SECURITY",
        "Do not start a sidecar until the configuration drift is independently reviewed.",
      );
    }
    const durable = options.repository.getSession(options.conversationId);
    if (!durable || stableJson(durable) !== stableJson(expected)) {
      throw new RuntimeFailureError(
        "OpenHands durable runtime identity changed while exact recovery was being prepared.",
        "OPENHANDS_RECOVER_EXACT_CAS_MISMATCH",
        true,
        "SQLITE",
        "Retry only after confirming no model switch or other recovery is active.",
      );
    }
    return new OpenHandsRuntime(options, {
      runtimeSessionId: expected.sessionId,
      stateRoot: resolvedStateRoot,
      configHash,
      runtimeRevision: expected.runtimeRevision,
      manifest,
      recoveryCursor: expected.recoveryCursor,
      specs,
      exactRecovery: true,
    });
  }

  private scrub(value: string): string {
    return redactSecrets(value.split(this.options.apiKey).join("[REDACTED]"));
  }

  private childFailure(message: string): RuntimeFailureError {
    const stderr = this.stderrTail.trim();
    return new RuntimeFailureError(
      this.scrub(`${message}${stderr ? `; stderr=${stderr}` : ""}`),
      "OPENHANDS_SIDECAR_EXITED",
      true,
      "INTERNAL",
      "Restart the same QF runtime revision; UNKNOWN tool calls remain fail-closed and are never re-executed.",
    );
  }

  private processIdentity(child = this.child): OpenHandsSidecarProcessIdentity | null {
    if (!child || child.exitCode !== null || !Number.isInteger(child.pid) || Number(child.pid) <= 1) return null;
    return {
      processKey: `openhands-sidecar:${this.options.conversationId}:${this.runtimeSessionId}`,
      pid: Number(child.pid),
      kind: "OPENHANDS_SIDECAR",
      killMode: "PID",
      conversationId: this.options.conversationId,
      runtimeSessionId: this.runtimeSessionId,
      runtimeRevision: this.runtimeRevision,
      configHash: this.configHash,
    };
  }

  get sidecarProcess(): OpenHandsSidecarProcessIdentity | null {
    return this.processIdentity();
  }

  private async waitForChildExit(child: ChildProcessWithoutNullStreams, timeoutMs: number): Promise<boolean> {
    if (child.exitCode !== null || child.signalCode !== null) return true;
    return new Promise<boolean>((resolve) => {
      let settled = false;
      const finish = () => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        child.off("exit", finish);
        resolve(child.exitCode !== null || child.signalCode !== null);
      };
      const timer = setTimeout(finish, timeoutMs);
      timer.unref();
      child.once("exit", finish);
      if (child.exitCode !== null || child.signalCode !== null) finish();
    });
  }

  private async ensureStarted(): Promise<void> {
    if (this.disposed) throw new Error("OpenHands runtime is disposed");
    if (this.child && this.child.exitCode === null) return;
    if (this.starting) return this.starting;
    this.starting = this.startChild().finally(() => {
      this.starting = null;
    });
    return this.starting;
  }

  private async startChild(): Promise<void> {
    const recoveryHandshake = this.exactRecovery || this.recoveryHandshakeRequested;
    this.stderrTail = "";
    const ready = deferred<JsonObject>();
    // The child can fail before startChild begins awaiting readiness. Attach a
    // handler immediately so its exit cannot become an unhandled rejection
    // that terminates the Control Plane process.
    void ready.promise.catch(() => undefined);
    this.ready = ready;
    // The isolated launcher mounts the versioned sidecar source read-only. A
    // process restart is therefore the only supported way to load a source
    // revision; never patch a live runtime in place.
    const child = spawn(this.options.sidecarPath, [...(this.options.sidecarArguments ?? []), this.stateRoot], {
      cwd: this.options.projectRoot,
      env: {
        PATH: process.env.PATH ?? "/usr/bin:/bin",
        LANG: "C.UTF-8",
        LC_ALL: "C.UTF-8",
        QF_PROCESS_NAMESPACE: "qfintelligence",
      },
      shell: false,
      stdio: ["pipe", "pipe", "pipe"],
    });
    this.child = child;
    this.sidecarExitCleanup = null;
    const identity = this.processIdentity(child);
    if (!identity) {
      child.kill("SIGTERM");
      throw new Error("OpenHands sidecar did not expose a valid process identity");
    }
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk: string) => {
      this.stderrTail = this.scrub(`${this.stderrTail}${chunk}`).slice(-16_000);
    });
    const lines = createInterface({ input: child.stdout, crlfDelay: Infinity });
    lines.on("line", (line) => this.receiveLine(line));
    child.once("error", (error) => {
      ready.reject(error);
      this.failPending(error);
    });
    child.once("exit", (code, signal) => {
      if (this.child === child) this.child = null;
      this.sidecarExitCleanup = Promise.resolve(this.options.onSidecarExited?.(identity)).catch((error: unknown) => {
        this.sinkFailure ??= error;
      });
      const failure = this.childFailure(`OpenHands sidecar exited with code=${String(code)} signal=${String(signal)}`);
      ready.reject(failure);
      this.failPending(failure);
      if (!this.disposed) {
        try {
          this.options.repository.markOpenHandsToolCallsUnknown(this.options.conversationId, this.runtimeSessionId);
        } catch (error) {
          this.sinkFailure ??= error;
        }
      }
      for (const controller of this.toolControllers.values()) controller.abort(failure);
      this.toolControllers.clear();
    });
    const readyFrame = await withTimeout(ready.promise, READY_TIMEOUT_MS, "OpenHands sidecar readiness");
    const sidecarSourceSha256 = typeof readyFrame.sidecarSourceSha256 === "string" ? readyFrame.sidecarSourceSha256 : "";
    if (
      readyFrame.sdkVersion !== OPENHANDS_SDK_VERSION
      || readyFrame.sidecarVersion !== OPENHANDS_SIDECAR_VERSION
      || !/^[a-f0-9]{64}$/u.test(sidecarSourceSha256)
    ) {
      child.kill("SIGTERM");
      throw new Error("OpenHands sidecar version assertion failed");
    }
    // run-isolated.sh starts as Bash and then execs bubblewrap. Registration
    // must observe the final bwrap identity, while still completing before the
    // init frame exposes Provider configuration or its API key to the sidecar.
    try {
      await this.options.onSidecarStarted?.(identity);
    } catch (error) {
      child.kill("SIGTERM");
      throw error;
    }
    const initialized = await this.request("init", {
      runtimeSessionId: this.runtimeSessionId,
      provider: this.options.provider,
      modelId: this.options.modelId,
      baseUrl: this.options.baseUrl.replace(/\/$/u, ""),
      apiKey: this.options.apiKey,
      systemPrompt: this.options.systemPrompt ?? FULL_QF_SYSTEM_PROMPT,
      systemPromptHash: this.manifest.promptHash,
      configHash: this.configHash,
      runtimeRevision: this.runtimeRevision,
      recoveryCursor: this.recoveryCursor,
      recoveryMode: recoveryHandshake ? "RECOVER_EXACT" : "CREATE_OR_REUSE",
      toolSpecs: qfToolSpecsForIpc(this.specs),
      toolNames: [...this.toolNames],
      currentDateTime: this.manifest.agentContextDateTime,
      maxOutputTokens: this.options.maxOutputTokens ?? 8_192,
      maxIterations: this.options.maxIterations ?? 100,
    }, INIT_TIMEOUT_MS);
    assertSameToolNames(
      Array.isArray(initialized.toolNames) ? initialized.toolNames.map(String) : [],
      this.toolNames,
    );
    if (
      initialized.runtimeSessionId !== this.runtimeSessionId
      || initialized.runtimeRevision !== this.runtimeRevision
      || initialized.configHash !== this.configHash
      || initialized.recoveryCursor !== this.recoveryCursor
      || initialized.providerPromptRequestsThisProcess !== 0
      || initialized.sidecarSourceSha256 !== sidecarSourceSha256
      || typeof initialized.manifestHash !== "string"
      || !/^[a-f0-9]{64}$/u.test(initialized.manifestHash)
      || !Array.isArray(initialized.defaultTools)
      || stableJson(initialized.defaultTools) !== stableJson(["terminal", "file_editor"])
      || !Array.isArray(initialized.skills)
      || initialized.skills.length !== 0
      || !Array.isArray(initialized.mcp)
      || initialized.mcp.length !== 1
      || typeof initialized.workspace !== "object"
      || initialized.workspace === null
      || Array.isArray(initialized.workspace)
      || initialized.workspace.hostLocalWorkspace !== false
      || initialized.workspace.backend !== "hardened-docker-agent-server"
    ) {
      child.kill("SIGTERM");
      throw new Error("OpenHands default capability assertion failed");
    }
    if (!recoveryHandshake) {
      this.options.repository.upsertOpenHandsSession({
        conversationId: this.options.conversationId,
        sessionId: this.runtimeSessionId,
        relativeSessionFile: path.relative(this.options.sessionRoot, this.stateRoot),
        provider: this.options.provider,
        modelId: this.options.modelId,
        promptVersion: this.options.promptVersion,
        promptHash: this.manifest.promptHash,
        lifecycleState: "READY",
        runtimeRevision: this.runtimeRevision,
        configHash: this.configHash,
        recoveryCursor: this.recoveryCursor,
      });
    }
    const workspace = jsonObject(initialized.workspace, "OpenHands workspace proof");
    const workspaceSnapshot = jsonObject(workspace.snapshot, "OpenHands workspace snapshot");
    const secretEnvironmentNames = Array.isArray(workspace.secretEnvironmentNames)
      ? workspace.secretEnvironmentNames.map(String)
      : [];
    this.options.repository.recordOpenHandsWorkspace({
      conversationId: this.options.conversationId,
      runtimeSessionId: this.runtimeSessionId,
      manifestHash: String(initialized.manifestHash),
      backend: "hardened-docker-agent-server",
      hostLocalWorkspace: false,
      workspaceId: String(workspace.workspaceId),
      snapshot: workspaceSnapshot,
      imageReference: String(workspace.imageReference),
      imageDigest: String(workspace.imageDigest),
      containerId: String(workspace.containerId),
      serverReused: workspace.serverReused === true,
      secretEnvironmentNames,
    });
  }

  private receiveLine(line: string): void {
    if (Buffer.byteLength(line, "utf8") > MAX_FRAME_BYTES) {
      this.child?.kill("SIGTERM");
      this.failPending(new Error("OpenHands sidecar output frame exceeded the protocol limit"));
      return;
    }
    let frame: SidecarFrame;
    try {
      const parsed = JSON.parse(line) as unknown;
      frame = jsonObject(parsed, "OpenHands sidecar frame") as SidecarFrame;
      if (frame.protocol !== OPENHANDS_PROTOCOL_VERSION) throw new Error("protocol version mismatch");
    } catch (error) {
      this.child?.kill("SIGTERM");
      this.failPending(new Error(`OpenHands sidecar emitted invalid JSON: ${safeErrorMessage(error)}`));
      return;
    }
    if (frame.type === "ready") {
      this.ready?.resolve(frame);
      return;
    }
    if (frame.type === "response") {
      const requestId = typeof frame.requestId === "string" ? frame.requestId : "";
      const pending = this.pending.get(requestId);
      if (!pending) return;
      clearTimeout(pending.timer);
      this.pending.delete(requestId);
      if (frame.ok === true) pending.resolve(jsonObject(frame.result ?? {}, "OpenHands response result"));
      else pending.reject(jsonObject(frame.error ?? {}, "OpenHands response error"));
      return;
    }
    if (frame.type === "event") {
      this.enqueueEvent(frame);
      return;
    }
    if (frame.type === "tool.call") {
      void this.handleToolCall(frame);
      return;
    }
    if (frame.type === "fatal") {
      const failure = this.childFailure(`OpenHands sidecar fatal protocol error ${String(frame.code ?? "UNKNOWN")}`);
      this.failPending(failure);
      this.child?.kill("SIGTERM");
    }
  }

  private enqueueEvent(frame: SidecarFrame): void {
    if (this.activeSink === null) {
      this.eventBacklog.push(frame);
      return;
    }
    this.eventQueue = this.eventQueue.then(async () => {
      const mapped = mapOpenHandsEvent(frame);
      if (!mapped || this.activeSink === null) return;
      const eventId = typeof frame.eventId === "string" ? frame.eventId : "";
      if (mapped.persistent && (!eventId || this.seenBackendEventIds.has(eventId))) return;
      await this.activeSink(mapped);
      if (mapped.persistent) {
        this.seenBackendEventIds.add(eventId);
        this.recoveryCursor += 1;
        this.options.repository.updateOpenHandsRecoveryCursor(
          this.options.conversationId,
          this.runtimeSessionId,
          this.recoveryCursor,
        );
      }
    }).catch((error: unknown) => {
      this.sinkFailure = error;
    });
  }

  private async flushBacklog(): Promise<void> {
    const frames = this.eventBacklog.splice(0);
    for (const frame of frames) this.enqueueEvent(frame);
    await this.eventQueue;
    if (this.sinkFailure !== null) {
      const failure = this.sinkFailure;
      this.sinkFailure = null;
      throw failure;
    }
  }

  private async writeFrame(frame: JsonObject): Promise<void> {
    const child = this.child;
    if (!child || child.exitCode !== null) throw this.childFailure("OpenHands sidecar is not running");
    const encoded = `${stableJson({ protocol: OPENHANDS_PROTOCOL_VERSION, ...frame })}\n`;
    if (Buffer.byteLength(encoded, "utf8") > MAX_FRAME_BYTES) throw new Error("OpenHands request frame exceeds the protocol limit");
    await new Promise<void>((resolve, reject) => {
      child.stdin.write(encoded, "utf8", (error) => error ? reject(error) : resolve());
    });
  }

  private async request(type: string, payload: JsonObject, timeoutMs: number, requestId = randomUUID()): Promise<JsonObject> {
    await this.ensureStartedUnlessBootstrapping(type);
    const response = new Promise<JsonObject>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(requestId);
        reject(new Error(`OpenHands ${type} request timed out`));
      }, timeoutMs);
      timer.unref();
      this.pending.set(requestId, { resolve, reject, timer });
    });
    try {
      await this.writeFrame({ type, requestId, ...payload });
    } catch (error) {
      const pending = this.pending.get(requestId);
      if (pending) clearTimeout(pending.timer);
      this.pending.delete(requestId);
      throw error;
    }
    return response;
  }

  private async ensureStartedUnlessBootstrapping(type: string): Promise<void> {
    if (type !== "init") await this.ensureStarted();
  }

  private failPending(error: unknown): void {
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(error);
    }
    this.pending.clear();
  }

  private async sendToolResult(frame: SidecarFrame, ok: boolean, value: JsonValue): Promise<void> {
    const requestId = typeof frame.requestId === "string" ? frame.requestId : "";
    await this.writeFrame(ok
      ? { type: "tool.result", requestId, ok: true, result: value }
      : { type: "tool.result", requestId, ok: false, error: value });
  }

  private async handleToolCall(frame: SidecarFrame): Promise<void> {
    const toolCallId = typeof frame.toolCallId === "string" ? frame.toolCallId : "";
    const toolName = typeof frame.toolName === "string" ? frame.toolName : "";
    const requestHash = typeof frame.requestSha256 === "string" ? frame.requestSha256 : "";
    const runtimeSessionId = typeof frame.runtimeSessionId === "string" ? frame.runtimeSessionId : "";
    const argumentsValue = frame.arguments ?? {};
    const expectedHash = sha256({ toolName, arguments: argumentsValue });
    const boundaryChecks = {
      runtimeSessionMatches: runtimeSessionId === this.runtimeSessionId,
      toolCallIdPresent: Boolean(toolCallId),
      toolAllowlisted: this.activeToolNames.has(toolName),
      toolSpecPresent: this.specByName.has(toolName),
      requestHashMatches: requestHash === expectedHash,
    };
    if (!Object.values(boundaryChecks).every(Boolean)) {
      this.options.repository.appendEvent({
        conversationId: this.options.conversationId,
        type: "run.blocked",
        payload: {
          summary: "OpenHands 工具帧在 QF 边界被拒绝",
          code: "OPENHANDS_TOOL_BOUNDARY_REJECTED",
          boundaryChecks,
          formalTestSealed: true,
        },
      });
      await this.sendToolResult(frame, false, {
        code: "OPENHANDS_TOOL_BOUNDARY_REJECTED",
        message: "QF rejected a tool call outside the active runtime identity or allowlist.",
      });
      return;
    }
    const claim = this.options.repository.claimOpenHandsToolCall({
      conversationId: this.options.conversationId,
      runtimeSessionId,
      toolCallId,
      toolName,
      requestHash,
    });
    if (claim.disposition === "REPLAY") {
      if (claim.entry.status === "COMPLETED") await this.sendToolResult(frame, true, claim.entry.result);
      else await this.sendToolResult(frame, false, claim.entry.error ?? { code: "OPENHANDS_TOOL_REPLAY_FAILED" });
      return;
    }
    if (claim.disposition === "IN_FLIGHT") {
      await this.sendToolResult(frame, false, {
        code: "OPENHANDS_TOOL_OUTCOME_UNKNOWN",
        message: "The durable QF tool claim has no replayable terminal result; execution is not repeated.",
      });
      return;
    }
    const controller = new AbortController();
    this.toolControllers.set(toolCallId, controller);
    try {
      const spec = this.specByName.get(toolName)!;
      const validated = validateQfToolArguments(spec, argumentsValue);
      const context = { runtimeSessionId, toolCallId, signal: controller.signal };
      const result = jsonValue(this.options.toolExecutor
        ? await this.options.toolExecutor(toolName, validated, context)
        : this.options.host
          ? await this.options.host.execute(toolName as QfToolName, validated, context)
          : (() => { throw new Error("OpenHands runtime has no QF tool executor"); })());
      controller.signal.throwIfAborted();
      if (Buffer.byteLength(stableJson(result), "utf8") > MAX_REPLAY_RESULT_BYTES) {
        throw new Error("QF tool result exceeds the durable replay limit");
      }
      const completed = this.options.repository.completeOpenHandsToolCall({
        conversationId: this.options.conversationId,
        runtimeSessionId,
        toolCallId,
        result,
      });
      await this.sendToolResult(frame, true, completed.result);
    } catch (error) {
      const detail: JsonObject = {
        code: controller.signal.aborted ? "OPENHANDS_TOOL_ABORTED" : "OPENHANDS_TOOL_FAILED",
        message: this.scrub(safeErrorMessage(error)),
        retryable: false,
      };
      const failed = this.options.repository.failOpenHandsToolCall({
        conversationId: this.options.conversationId,
        runtimeSessionId,
        toolCallId,
        error: detail,
      });
      try {
        await this.sendToolResult(frame, false, failed.error ?? detail);
      } catch {
        // The durable terminal error is replayed if the isolated sidecar restarts.
      }
    } finally {
      this.toolControllers.delete(toolCallId);
    }
  }

  async prompt(content: string, sink: RuntimeEventSink, options?: RuntimePromptOptions): Promise<RuntimeResult> {
    await this.ensureStarted();
    if (this.activeSink !== null) throw new Error("OpenHands runtime already has an active prompt");
    const selected = options?.toolNames ?? this.toolNames;
    if (new Set(selected).size !== selected.length) throw new Error("OpenHands prompt tool list contains duplicates");
    const unknown = selected.filter((name) => !this.specByName.has(name));
    if (unknown.length > 0) throw new Error(`OpenHands prompt requested unknown QF tools: ${unknown.join(",")}`);
    this.activeSink = sink;
    this.activeToolNames = new Set(selected);
    this.sinkFailure = null;
    const requestId = randomUUID();
    try {
      await this.flushBacklog();
      let response: JsonObject;
      try {
        response = await this.request("prompt", { content, toolNames: [...selected] }, PROMPT_TIMEOUT_MS, requestId);
      } catch (error) {
        await this.eventQueue;
        if (this.sinkFailure !== null) {
          const failure = this.sinkFailure;
          this.sinkFailure = null;
          throw failure;
        }
        const detail = typeof error === "object" && error !== null && !Array.isArray(error)
          ? jsonObject(error, "OpenHands prompt error")
          : {};
        if (detail.code === "OPENHANDS_ABORTED") throw new RuntimeAbortedError();
        if (error instanceof RuntimeAbortedError) throw error;
        if (error instanceof RuntimeFailureError) throw error;
        const actionScopeRequired = detail.code === "OPENHANDS_ACTION_SCOPE_REQUIRED";
        throw new RuntimeFailureError(
          this.scrub(typeof detail.message === "string" ? detail.message : safeErrorMessage(error)),
          typeof detail.code === "string" ? detail.code : "OPENHANDS_PROMPT_FAILED",
          !actionScopeRequired,
          actionScopeRequired ? "SECURITY" : "PROVIDER",
          actionScopeRequired
            ? "Revise the execution plan to use approved workspace commands or an explicitly governed QF Tool Gateway action."
            : "Resume only the same explicitly selected Provider/model and runtime revision after inspecting durable events.",
        );
      }
      await this.eventQueue;
      if (this.sinkFailure !== null) throw this.sinkFailure;
      const assistantContent = typeof response.assistantContent === "string" ? response.assistantContent : "";
      if (!assistantContent.trim()) {
        throw new RuntimeFailureError(
          "OpenHands completed without assistant text.",
          "OPENHANDS_EMPTY_RESPONSE",
          true,
          "MODEL",
          "Inspect the persisted OpenHands events before retrying the same model.",
        );
      }
      const usage = jsonObject(response.usage ?? {}, "OpenHands usage");
      return {
        assistantContent,
        provider: typeof response.provider === "string" ? response.provider : this.options.provider,
        modelId: typeof response.modelId === "string" ? response.modelId : this.options.modelId,
        runtimeMessageId: typeof response.backendMessageId === "string" ? response.backendMessageId : null,
        usage: {
          input: typeof usage.input === "number" ? usage.input : 0,
          output: typeof usage.output === "number" ? usage.output : 0,
          cacheRead: typeof usage.cacheRead === "number" ? usage.cacheRead : 0,
          cacheWrite: typeof usage.cacheWrite === "number" ? usage.cacheWrite : 0,
          reasoning: typeof usage.reasoning === "number" ? usage.reasoning : null,
          totalTokens: typeof usage.totalTokens === "number" ? usage.totalTokens : 0,
        },
      };
    } finally {
      this.activeSink = null;
      this.activeToolNames.clear();
    }
  }

  async steer(content: string): Promise<void> {
    try {
      await this.request("steer", { content }, QUEUE_TIMEOUT_MS);
    } catch (error) {
      const detail = typeof error === "object" && error !== null && !Array.isArray(error)
        ? jsonObject(error, "OpenHands steer error")
        : {};
      throw new RuntimeControlError(
        this.scrub(typeof detail.message === "string" ? detail.message : safeErrorMessage(error)),
        typeof detail.code === "string" ? detail.code : "OPENHANDS_STEER_FAILED",
        detail.retryable === true,
        "Queue the input as a follow-up, or wait for the active turn to finish before starting another prompt.",
      );
    }
  }

  async followUp(content: string): Promise<void> {
    try {
      await this.request("follow_up", { content }, QUEUE_TIMEOUT_MS);
    } catch (error) {
      const detail = typeof error === "object" && error !== null && !Array.isArray(error)
        ? jsonObject(error, "OpenHands follow-up error")
        : {};
      throw new RuntimeControlError(
        this.scrub(typeof detail.message === "string" ? detail.message : safeErrorMessage(error)),
        typeof detail.code === "string" ? detail.code : "OPENHANDS_FOLLOW_UP_FAILED",
        detail.retryable === true,
        "Retry only after confirming the same active runtime revision remains available.",
      );
    }
  }

  async abort(): Promise<void> {
    for (const controller of this.toolControllers.values()) controller.abort(new RuntimeAbortedError());
    if (!this.child || this.child.exitCode !== null) return;
    try {
      await this.request("abort", {}, QUEUE_TIMEOUT_MS);
    } catch (error) {
      if (this.child?.exitCode === null) throw error;
    }
  }

  async recoverSidecar(): Promise<OpenHandsRecoveryResult> {
    if (this.disposed) throw new Error("OpenHands runtime is disposed");
    if (this.activeSink !== null || this.pending.size > 0 || this.toolControllers.size > 0) {
      throw new Error("OpenHands sidecar recovery requires an idle runtime");
    }
    const restarted = this.sidecarProcess === null;
    this.recoveryHandshakeRequested = true;
    await this.ensureStarted();
    const process = this.sidecarProcess;
    if (!process) throw new Error("OpenHands sidecar recovery did not produce a live process");
    const health = await this.request("health", {}, INIT_TIMEOUT_MS);
    const sdkEventCount = typeof health.sdkEventCount === "number" ? health.sdkEventCount : -1;
    const executionStatus = typeof health.executionStatus === "string" ? health.executionStatus : "";
    if (
      health.runtimeSessionId !== this.runtimeSessionId
      || health.runtimeRevision !== this.runtimeRevision
      || health.configHash !== this.configHash
      || health.recoveryCursor !== this.recoveryCursor
      || health.providerPromptRequestsThisProcess !== 0
      || !Number.isSafeInteger(sdkEventCount)
      || sdkEventCount < 0
      || !executionStatus
    ) {
      throw new RuntimeFailureError(
        "OpenHands sidecar exact-recovery health proof did not match the durable runtime identity.",
        "OPENHANDS_RECOVER_EXACT_HEALTH_MISMATCH",
        false,
        "SECURITY",
        "Stop the registered sidecar and inspect its recovery handshake before retrying.",
      );
    }
    return {
      restarted,
      process,
      runtimeSessionId: this.runtimeSessionId,
      runtimeRevision: this.runtimeRevision,
      configHash: this.configHash,
      recoveryCursor: this.recoveryCursor,
      executionStatus,
      sdkEventCount,
      providerPromptRequestsThisProcess: 0,
    };
  }

  async shutdown(): Promise<void> {
    this.disposed = true;
    for (const controller of this.toolControllers.values()) controller.abort(new RuntimeAbortedError());
    this.toolControllers.clear();
    const child = this.child;
    if (!child || child.exitCode !== null) {
      await this.sidecarExitCleanup;
      return;
    }
    try {
      await this.writeFrame({ type: "shutdown", requestId: randomUUID() });
    } catch {
      // A sidecar that has already lost its IPC channel is terminated below.
    }
    if (!(await this.waitForChildExit(child, 2_000)) && child.exitCode === null) {
      child.kill("SIGTERM");
      await this.waitForChildExit(child, 2_000);
    }
    await this.sidecarExitCleanup;
  }

  dispose(): void {
    void this.shutdown();
  }

  terminateForRecoveryTest(): void {
    const child = this.child;
    if (child?.exitCode === null) child.kill("SIGTERM");
  }
}
