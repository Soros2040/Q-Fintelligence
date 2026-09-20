import { createHash } from "node:crypto";
import { appendFileSync, readFileSync } from "node:fs";
import path from "node:path";
import { createInterface } from "node:readline";

const PROTOCOL = "qf.agent-runtime.v2";
const SDK_VERSION = "1.39.0+qf.noobservability.1";
const SIDECAR_VERSION = "qf-openhands-sidecar.v3";
const SIDECAR_SOURCE_SHA256 = "f".repeat(64);

function stable(value) {
  if (value === null || typeof value === "boolean" || typeof value === "number" || typeof value === "string") {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(stable).join(",")}]`;
  return `{${Object.entries(value).sort(([left], [right]) => left.localeCompare(right))
    .map(([key, entry]) => `${JSON.stringify(key)}:${stable(entry)}`).join(",")}}`;
}

function sha256(value) {
  return createHash("sha256").update(stable(value)).digest("hex");
}

function send(frame) {
  process.stdout.write(`${stable({ protocol: PROTOCOL, ...frame })}\n`);
}

let initialized = null;
let pendingPrompt = null;
let providerPromptRequestsThisProcess = 0;
let sdkEventCount = 0;

const stateRoot = process.argv[2];
if (stateRoot) {
  appendFileSync(
    path.join(stateRoot, ".qf-fake-sidecar-starts.jsonl"),
    `${JSON.stringify({ pid: process.pid })}\n`,
    { encoding: "utf8", mode: 0o600 },
  );
}

function recoveryDelayMilliseconds(message) {
  if (message.recoveryMode !== "RECOVER_EXACT" || !stateRoot) return 0;
  try {
    const value = Number(readFileSync(path.join(stateRoot, ".qf-fake-recovery-delay-ms"), "utf8").trim());
    return Number.isSafeInteger(value) && value >= 0 ? value : 0;
  } catch (error) {
    if (error?.code === "ENOENT") return 0;
    throw error;
  }
}

send({
  type: "ready",
  sdkVersion: SDK_VERSION,
  sidecarVersion: SIDECAR_VERSION,
  sidecarSourceSha256: SIDECAR_SOURCE_SHA256,
  pid: process.pid,
});

const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
lines.on("line", (line) => {
  const message = JSON.parse(line);
  if (message.protocol !== PROTOCOL) process.exit(64);
  if (message.type === "init") {
    initialized = message;
    const respond = () => send({
      type: "response",
      requestId: message.requestId,
      ok: true,
      result: {
        runtimeSessionId: message.runtimeSessionId,
        runtimeRevision: message.runtimeRevision,
        configHash: message.configHash,
        recoveryCursor: message.recoveryCursor,
        executionStatus: "IDLE",
        sdkEventCount,
        providerPromptRequestsThisProcess,
        sdkVersion: SDK_VERSION,
        sidecarVersion: SIDECAR_VERSION,
        sidecarSourceSha256: SIDECAR_SOURCE_SHA256,
        manifestHash: "e".repeat(64),
        toolNames: message.toolNames,
        defaultTools: ["terminal", "file_editor"],
        skills: [],
        mcp: [{ name: "qf-control-plane", version: "qf.mcp-gateway.v1", toolCount: message.toolNames.length }],
        workspace: {
          backend: "hardened-docker-agent-server",
          hostLocalWorkspace: false,
          workspaceId: message.runtimeSessionId,
          snapshot: {
            schemaVersion: "qf.openhands-workspace-snapshot.v1",
            sourceHead: "a".repeat(40),
            snapshotSha256: "b".repeat(64),
            fileCount: 1,
            totalBytes: 1,
          },
          imageReference: "qfintelligence/openhands-agent-server:1.39.0-qf.1",
          imageDigest: `sha256:${"d".repeat(64)}`,
          containerId: "c".repeat(64),
          serverReused: message.recoveryMode === "RECOVER_EXACT",
          secretEnvironmentNames: [],
        },
        steerSemantics: "INTERRUPT_THEN_FOLLOW_UP",
        followUpSemantics: "QUEUED_AFTER_CURRENT_TURN",
        persistenceRelativePath: "agent-server",
      },
    });
    const delayMilliseconds = recoveryDelayMilliseconds(message);
    if (delayMilliseconds > 0) setTimeout(respond, delayMilliseconds);
    else respond();
    return;
  }
  if (message.type === "prompt") {
    providerPromptRequestsThisProcess += 1;
    const content = String(message.content);
    sdkEventCount += 1;
    send({
      type: "event",
      requestId: message.requestId,
      eventId: `agent:${content}`,
      eventType: "agent.started",
      payload: { runtimeSessionId: initialized.runtimeSessionId, toolNames: message.toolNames },
    });
    send({
      type: "event",
      requestId: message.requestId,
      eventId: `agent:${content}`,
      eventType: "agent.started",
      payload: { runtimeSessionId: initialized.runtimeSessionId, toolNames: message.toolNames },
    });
    if (content.includes("LONG_ABORT")) {
      pendingPrompt = { requestId: message.requestId, mode: "abort" };
      return;
    }
    if (content.includes("SIDECAR_FAILURE")) {
      send({
        type: "event",
        requestId: message.requestId,
        eventId: `failure:${message.requestId}`,
        eventType: "run.failed",
        payload: { code: "FAKE_SIDECAR_FAILURE", message: "deterministic fake failure", retryable: true },
      });
      send({
        type: "response",
        requestId: message.requestId,
        ok: false,
        error: { code: "FAKE_SIDECAR_FAILURE", message: "deterministic fake failure" },
      });
      return;
    }
    if (content.includes("TOOL")) {
      const toolName = String(message.toolNames[0]);
      const toolCallId = content.includes("STABLE") ? "stable-tool-call" : `tool:${message.requestId}`;
      pendingPrompt = {
        requestId: message.requestId,
        toolCallId,
        toolName,
        mode: content.includes("CRASH_AFTER_RESULT") ? "crash-after-result" : "normal",
      };
      send({
        type: "event",
        requestId: message.requestId,
        eventId: `action:${toolCallId}`,
        eventType: "tool.started",
        payload: { toolCallId, toolName, arguments: {} },
      });
      send({
        type: "tool.call",
        requestId: toolCallId,
        runtimeSessionId: initialized.runtimeSessionId,
        toolCallId,
        toolName,
        arguments: {},
        requestSha256: sha256({ toolName, arguments: {} }),
      });
      return;
    }
    send({
      type: "event",
      requestId: message.requestId,
      eventId: `token:${content}`,
      eventType: "assistant.delta",
      payload: { text: "FAKE_OPENHANDS_" },
    });
    send({
      type: "event",
      requestId: message.requestId,
      eventId: `token:${content}:2`,
      eventType: "assistant.delta",
      payload: { text: "OK" },
    });
    send({
      type: "response",
      requestId: message.requestId,
      ok: true,
      result: {
        assistantContent: "FAKE_OPENHANDS_OK",
        provider: initialized.provider,
        modelId: initialized.modelId,
        backendMessageId: `message:${message.requestId}`,
        usage: { input: 3, output: 2, cacheRead: 0, cacheWrite: 0, reasoning: null, totalTokens: 5 },
      },
    });
    return;
  }
  if (message.type === "tool.result" && pendingPrompt?.toolCallId === message.requestId) {
    if (pendingPrompt.mode === "crash-after-result") process.exit(73);
    send({
      type: "event",
      requestId: pendingPrompt.requestId,
      eventId: `observation:${pendingPrompt.toolCallId}`,
      eventType: "tool.completed",
      payload: {
        toolCallId: pendingPrompt.toolCallId,
        toolName: pendingPrompt.toolName,
        result: message.ok ? message.result : message.error,
        isError: message.ok !== true,
      },
    });
    send({
      type: "response",
      requestId: pendingPrompt.requestId,
      ok: true,
      result: {
        assistantContent: message.ok ? "FAKE_TOOL_OK" : "FAKE_TOOL_FAILED_CLOSED",
        provider: initialized.provider,
        modelId: initialized.modelId,
        backendMessageId: `message:${pendingPrompt.requestId}`,
        usage: { input: 4, output: 3, cacheRead: 0, cacheWrite: 0, reasoning: null, totalTokens: 7 },
      },
    });
    pendingPrompt = null;
    return;
  }
  if (message.type === "abort") {
    send({ type: "response", requestId: message.requestId, ok: true, result: {} });
    if (pendingPrompt?.mode === "abort") {
      send({
        type: "response",
        requestId: pendingPrompt.requestId,
        ok: false,
        error: { code: "OPENHANDS_ABORTED", message: "run aborted" },
      });
      pendingPrompt = null;
    }
    return;
  }
  if (message.type === "steer") {
    send({
      type: "response",
      requestId: message.requestId,
      ok: true,
      result: { semantics: "INTERRUPT_THEN_FOLLOW_UP", position: 1 },
    });
    return;
  }
  if (message.type === "health") {
    send({
      type: "response",
      requestId: message.requestId,
      ok: true,
      result: {
        runtimeSessionId: initialized.runtimeSessionId,
        runtimeRevision: initialized.runtimeRevision,
        configHash: initialized.configHash,
        recoveryCursor: initialized.recoveryCursor,
        executionStatus: "IDLE",
        sdkEventCount,
        providerPromptRequestsThisProcess,
      },
    });
    return;
  }
  if (message.type === "follow_up") {
    send({ type: "response", requestId: message.requestId, ok: true, result: {} });
    return;
  }
  if (message.type === "shutdown") process.exit(0);
});
