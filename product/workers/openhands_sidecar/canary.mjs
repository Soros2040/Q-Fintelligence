import { createHash, randomUUID } from "node:crypto";
import { mkdir, readFile, readdir, stat, writeFile } from "node:fs/promises";
import path from "node:path";
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";

const PROTOCOL = "qf.agent-runtime.v2";
const projectRoot = path.resolve(import.meta.dirname, "../..");
const wrapper = path.join(import.meta.dirname, "run-isolated.sh");
const reportRoot = path.join(projectRoot, ".local", "openhands-canary");
const stateParent = path.join(projectRoot, ".local", "openhands-sessions");
const apiKey = process.env.DEEPSEEK_API_KEY;
const baseUrl = (process.env.DEEPSEEK_BASE_URL ?? "").replace(/\/$/u, "");

if (!apiKey || !baseUrl.startsWith("https://")) {
  throw new Error("DeepSeek OpenAI-compatible credentials are not configured");
}

function withTimeout(promise, milliseconds, label) {
  let timeout;
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      timeout = setTimeout(() => reject(new Error(`${label} timed out`)), milliseconds);
    }),
  ]).finally(() => clearTimeout(timeout));
}

class SidecarClient {
  constructor(stateRoot) {
    this.responses = new Map();
    this.events = [];
    this.toolCalls = [];
    this.stderr = "";
    this.readyResolve = null;
    this.ready = new Promise((resolve) => { this.readyResolve = resolve; });
    this.blockedToolResolve = null;
    this.blockedToolCall = new Promise((resolve) => { this.blockedToolResolve = resolve; });
    this.child = spawn("bash", [wrapper, stateRoot], {
      cwd: projectRoot,
      env: {
        LANG: "C.UTF-8",
        LOGNAME: "sandbox",
        PATH: process.env.PATH ?? "/usr/bin:/bin",
        USER: "sandbox",
      },
      stdio: ["pipe", "pipe", "pipe"],
    });
    createInterface({ input: this.child.stdout }).on("line", (line) => this.onLine(line));
    this.child.stderr.on("data", (chunk) => {
      this.stderr = `${this.stderr}${chunk.toString("utf8")}`.slice(-32_000);
    });
  }

  write(message) {
    this.child.stdin.write(`${JSON.stringify({ protocol: PROTOCOL, ...message })}\n`);
  }

  onLine(line) {
    const message = JSON.parse(line);
    if (message.protocol !== PROTOCOL) throw new Error("sidecar emitted an invalid protocol frame");
    if (message.type === "ready") {
      this.readyResolve(message);
      return;
    }
    if (message.type === "event") {
      this.events.push(message);
      return;
    }
    if (message.type === "tool.call") {
      this.toolCalls.push(message);
      if (message.toolName === "qf_canary_block") {
        this.blockedToolResolve(message);
        return;
      }
      this.write({
        type: "tool.result",
        requestId: message.requestId,
        ok: true,
        result: {
          summary: "QF canary context returned from the Node host.",
          taskId: "qf-openhands-stage-b-canary",
          formalTestSealed: true,
        },
      });
      return;
    }
    if (message.type === "response") {
      const waiter = this.responses.get(message.requestId);
      if (waiter) {
        this.responses.delete(message.requestId);
        if (message.ok) waiter.resolve(message.result ?? {});
        else waiter.reject(Object.assign(new Error(message.error?.message ?? "sidecar failed"), { code: message.error?.code }));
      }
    }
  }

  request(type, input = {}) {
    const requestId = `${type}-${randomUUID()}`;
    const promise = new Promise((resolve, reject) => this.responses.set(requestId, { resolve, reject }));
    this.write({ type, requestId, ...input });
    return promise;
  }

  resolveBlockedTool(message) {
    this.write({
      type: "tool.result",
      requestId: message.requestId,
      ok: true,
      result: {
        summary: "The controlled canary block was released after cancellation.",
        formalTestSealed: true,
      },
    });
  }

  async close() {
    if (this.child.exitCode === null) {
      await withTimeout(this.request("shutdown"), 60_000, "sidecar shutdown");
    }
    this.child.stdin.end();
    if (this.child.exitCode === null) {
      await withTimeout(new Promise((resolve) => this.child.once("exit", resolve)), 60_000, "sidecar exit");
    }
  }
}

async function listFiles(root) {
  const files = [];
  async function visit(directory) {
    for (const name of await readdir(directory)) {
      const target = path.join(directory, name);
      const details = await stat(target);
      if (details.isDirectory()) await visit(target);
      else if (details.isFile()) files.push(target);
    }
  }
  await visit(root);
  return files.sort();
}

async function secretHits(root, secret, surfaces) {
  const hits = [];
  for (const file of await listFiles(root)) {
    const data = await readFile(file);
    if (data.includes(Buffer.from(secret))) hits.push(path.relative(projectRoot, file));
  }
  for (const [name, value] of Object.entries(surfaces)) {
    if (String(value).includes(secret)) hits.push(`surface:${name}`);
  }
  return hits;
}

const runtimeSessionId = randomUUID();
const stateRoot = path.join(stateParent, `stage-b-${runtimeSessionId}`);
await mkdir(stateRoot, { recursive: true, mode: 0o700 });
await mkdir(reportRoot, { recursive: true, mode: 0o700 });
const systemPrompt = [
  "You are the isolated q-fintelligence OpenHands Stage-B canary.",
  "Use only QF Tool Gateway tools supplied for the current route. Never call terminal, file, browser, network, package, or secret tools.",
  "Call exactly the QF tool explicitly requested by the user, once, before answering.",
  "Keep every response short and include the exact marker requested by the user.",
].join("\n");
const systemPromptHash = createHash("sha256").update(systemPrompt).digest("hex");
const runtimeRevision = "qf-openhands-adapter.v2/1.39.0+qf.noobservability.1/r1";
const configHash = createHash("sha256").update(JSON.stringify({
  provider: "deepseek",
  modelId: "deepseek-v4-pro",
  baseUrl,
  systemPromptHash,
  runtimeRevision,
})).digest("hex");
const toolSpecs = [
  {
    name: "qf_canary_context",
    label: "QF canary context",
    description: "Read one deterministic side-effect-free QF canary context from the Node host.",
    parameters: { type: "object", properties: {}, required: [], additionalProperties: false },
  },
  {
    name: "qf_canary_block",
    label: "QF controlled cancellation block",
    description: "Wait at a deterministic QF cancellation boundary until the host releases this canary call.",
    parameters: { type: "object", properties: {}, required: [], additionalProperties: false },
  },
];
const init = {
  runtimeSessionId,
  provider: "deepseek",
  modelId: "deepseek-v4-pro",
  baseUrl,
  apiKey,
  systemPrompt,
  systemPromptHash,
  configHash,
  runtimeRevision,
  recoveryCursor: 0,
  recoveryMode: "CREATE_OR_REUSE",
  toolSpecs,
  toolNames: toolSpecs.map((tool) => tool.name),
  currentDateTime: new Date().toISOString(),
  maxOutputTokens: 512,
  maxIterations: 8,
};

const first = new SidecarClient(stateRoot);
const firstReady = await withTimeout(first.ready, 30_000, "first sidecar ready");
const firstInit = await withTimeout(first.request("init", init), 120_000, "first sidecar init");
const firstHealth = await withTimeout(first.request("health"), 10_000, "first sidecar health");
const firstResult = await withTimeout(first.request("prompt", {
  content: "Call qf_canary_context exactly once, then reply with QF_OPENHANDS_TOOL_CANARY_OK.",
  toolNames: ["qf_canary_context"],
}), 180_000, "tool canary prompt");
await first.close();

const second = new SidecarClient(stateRoot);
const secondReady = await withTimeout(second.ready, 30_000, "resumed sidecar ready");
const secondInit = await withTimeout(second.request("init", {
  ...init,
  recoveryMode: "RECOVER_EXACT",
}), 120_000, "resumed sidecar init");
const resumedResult = await withTimeout(second.request("prompt", {
  content: "Do not call tools. Reply with QF_OPENHANDS_RECOVERY_OK.",
  toolNames: [],
}), 180_000, "recovery canary prompt");
const recoveryToolCalls = second.toolCalls.length;
const abortPromise = second.request("prompt", {
  content: "Call qf_canary_block exactly once, wait for its result, then reply with QF_ABORT_SHOULD_NOT_COMPLETE.",
  toolNames: ["qf_canary_block"],
});
const blockedToolCall = await withTimeout(
  second.blockedToolCall,
  180_000,
  "controlled cancellation tool call",
);
await withTimeout(second.request("abort"), 10_000, "abort command");
second.resolveBlockedTool(blockedToolCall);
let abortCode = "PROMPT_COMPLETED_BEFORE_ABORT";
try {
  await withTimeout(abortPromise, 60_000, "aborted prompt response");
} catch (error) {
  abortCode = error.code ?? "UNKNOWN_ABORT_ERROR";
}
let steerCode = "STEER_REJECTED";
try {
  await withTimeout(second.request("steer", { content: "Reply with the recovery marker again." }), 10_000, "steer acceptance");
  steerCode = "INTERRUPT_THEN_FOLLOW_UP";
} catch (error) {
  steerCode = error.code ?? "UNKNOWN_STEER_ERROR";
}
await second.close();

const eventTypes = [...first.events, ...second.events].map((event) => event.eventType);
const completedToolEvents = first.events
  .filter((event) => event.eventType === "tool.completed");
const tokenChunks = eventTypes.filter((type) => type === "assistant.delta").length;
const hits = await secretHits(stateRoot, apiKey, {
  firstReady: JSON.stringify(firstReady),
  firstInit: JSON.stringify(firstInit),
  firstHealth: JSON.stringify(firstHealth),
  firstEvents: JSON.stringify(first.events),
  firstStderr: first.stderr,
  secondReady: JSON.stringify(secondReady),
  secondInit: JSON.stringify(secondInit),
  secondEvents: JSON.stringify(second.events),
  secondStderr: second.stderr,
});
const checks = {
  sdkVersionExact: firstReady.sdkVersion === "1.39.0+qf.noobservability.1",
  sidecarSourceStable: /^[a-f0-9]{64}$/u.test(firstReady.sidecarSourceSha256)
    && firstReady.sidecarSourceSha256 === secondReady.sidecarSourceSha256
    && firstReady.sidecarSourceSha256 === firstInit.sidecarSourceSha256,
  toolsExact: JSON.stringify(firstInit.toolNames)
    === JSON.stringify(["qf_canary_block", "qf_canary_context"]),
  openHandsCapabilities: JSON.stringify(firstInit.defaultTools) === JSON.stringify(["terminal", "file_editor"])
    && firstInit.skills.length === 0
    && firstInit.mcp.length === 1
    && firstInit.workspace?.backend === "hardened-docker-agent-server"
    && firstInit.workspace?.hostLocalWorkspace === false
    && firstInit.workspace?.secretEnvironmentNames?.length === 0,
  manifestStable: /^[a-f0-9]{64}$/u.test(firstInit.manifestHash)
    && firstInit.manifestHash === secondInit.manifestHash
    && firstInit.manifestHash === firstHealth.manifestHash,
  toolCallExactlyOnce: first.toolCalls.length === 1 && first.toolCalls[0].toolName === "qf_canary_context",
  toolEventsComplete: eventTypes.includes("tool.started")
    && completedToolEvents.length === 1
    && completedToolEvents[0].payload?.isError === false,
  streamObserved: tokenChunks > 0,
  firstMarker: firstResult.assistantContent.includes("QF_OPENHANDS_TOOL_CANARY_OK"),
  providerUsage: firstResult.usage.totalTokens > 0 && resumedResult.usage.totalTokens > 0,
  runtimeSessionStable: firstResult.runtimeSessionId === runtimeSessionId && resumedResult.runtimeSessionId === runtimeSessionId,
  recoveryIdentityExact: secondInit.runtimeSessionId === runtimeSessionId
    && secondInit.runtimeRevision === runtimeRevision
    && secondInit.configHash === configHash
    && secondInit.recoveryCursor === 0,
  recoveryProviderCallsZero: secondInit.providerPromptRequestsThisProcess === 0,
  recoveryMarker: resumedResult.assistantContent.includes("QF_OPENHANDS_RECOVERY_OK"),
  chatToolsZero: recoveryToolCalls === 0,
  abortToolCallExactlyOnce: second.toolCalls.length === 1
    && second.toolCalls[0].toolName === "qf_canary_block",
  abortObserved: abortCode === "OPENHANDS_ABORTED",
  steerExplicitlySupported: firstInit.steerSemantics === "INTERRUPT_THEN_FOLLOW_UP"
    && firstInit.followUpSemantics === "QUEUED_AFTER_CURRENT_TURN"
    && steerCode === "INTERRUPT_THEN_FOLLOW_UP",
  resourceLimitsExact: firstHealth.isolation?.rlimits?.core?.soft === 0
    && firstHealth.isolation?.rlimits?.core?.hard === 0
    && firstHealth.isolation?.rlimits?.nofile?.soft === 256
    && firstHealth.isolation?.rlimits?.nofile?.hard === 256
    && firstHealth.isolation?.rlimits?.nproc?.soft === 1024
    && firstHealth.isolation?.rlimits?.nproc?.hard === 1024
    && firstHealth.isolation?.rlimits?.addressSpace?.soft === 2_147_483_648
    && firstHealth.isolation?.rlimits?.addressSpace?.hard === 2_147_483_648,
  isolationBoundaries: firstHealth.isolation?.hostname === "qf-openhands-sidecar"
    && firstHealth.isolation?.windowsProjectHidden === true
    && firstHealth.isolation?.workspaceAvailable === true
    && firstHealth.isolation?.workspaceBackend === "hardened-docker-agent-server"
    && firstHealth.isolation?.hostLocalWorkspace === false
    && /^sha256:[a-f0-9]{64}$/u.test(firstHealth.isolation?.agentServerImageDigest ?? "")
    && firstHealth.isolation?.providerSecretEnvironmentNames?.length === 0,
  secretHitsZero: hits.length === 0,
};
const report = {
  schemaVersion: "qf.openhands-stage-b-canary.v1",
  status: Object.values(checks).every(Boolean) ? "PASS" : "FAILED",
  runtimeSessionId,
  model: "deepseek/deepseek-v4-pro",
  sdkVersion: firstReady.sdkVersion,
  sidecarVersion: firstReady.sidecarVersion,
  checks,
  evidence: {
    firstUsage: firstResult.usage,
    resumedUsage: resumedResult.usage,
    toolCalls: first.toolCalls.length,
    tokenChunks,
    eventTypes: [...new Set(eventTypes)].sort(),
    abortCode,
    steerCode,
    manifestHash: firstInit.manifestHash,
    sidecarSourceSha256: firstReady.sidecarSourceSha256,
    isolation: firstHealth.isolation,
    secretHitCount: hits.length,
    secretHitPaths: hits,
    stateFiles: (await listFiles(stateRoot)).map((file) => path.relative(stateRoot, file)),
  },
  createdAt: new Date().toISOString(),
};
const reportPath = path.join(reportRoot, `stage-b-${runtimeSessionId}.json`);
await writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`, { encoding: "utf8", mode: 0o600 });
console.log(JSON.stringify({
  status: report.status,
  reportPath: path.relative(projectRoot, reportPath),
  checks,
  usageTotal: firstResult.usage.totalTokens + resumedResult.usage.totalTokens,
}));
if (report.status !== "PASS") process.exitCode = 1;
