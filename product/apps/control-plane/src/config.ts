import path from "node:path";

import { loadHardwareExecutionPolicy, type HardwareExecutionPolicy } from "./hardware-policy.js";

const RESERVED_PORTS = new Set([43_187, 43_188, 43_189, 43_190, 43_191, 43_192]);

export function resolveProjectRoot(moduleDirectory: string = import.meta.dirname): string {
  return path.resolve(moduleDirectory, "../../..");
}

function parsePort(name: string, fallback: number): number {
  const value = Number(process.env[name] ?? fallback);
  if (!Number.isInteger(value) || value < 1024 || value > 65_535) {
    throw new Error(`${name} must be an integer between 1024 and 65535`);
  }
  if (RESERVED_PORTS.has(value)) {
    throw new Error(`${name}=${value} is reserved by local runtime`);
  }
  return value;
}

function parseLoopbackHost(name: string, fallback: string): string {
  const value = process.env[name] ?? fallback;
  if (value !== "127.0.0.1" && value !== "::1" && value !== "localhost") {
    throw new Error(`${name} must remain loopback-only`);
  }
  return value;
}

export interface RuntimeConfig {
  project: "q-fintelligence";
  processNamespace: "qfintelligence";
  apiHost: string;
  apiPort: number;
  webPort: number;
  artifactRoot: string;
  sqlitePath: string;
  stateRoot: string;
  openHandsSessionRoot: string;
  openHandsSidecarPath: string;
  openHandsSidecarArguments?: readonly string[];
  systemPromptVersion: string;
  agentMode: "mock" | "openhands" | "hybrid";
  hardwarePolicy: HardwareExecutionPolicy;
}

export function loadRuntimeConfig(): RuntimeConfig {
  const processNamespace = process.env.QF_PROCESS_NAMESPACE ?? "qfintelligence";
  if (processNamespace !== "qfintelligence") {
    throw new Error("QF_PROCESS_NAMESPACE must be qfintelligence");
  }
  const stateRoot = process.env.QF_STATE_ROOT ?? ".local/state";
  const requestedAgentMode = process.env.QF_AGENT_MODE ?? "mock";
  // Existing private deployments may still carry the pre-migration name.
  // This is a one-way configuration alias only: it never creates or falls
  // back to a legacy runtime.
  const agentMode = requestedAgentMode === "pi" || requestedAgentMode === "harness"
    ? "openhands"
    : requestedAgentMode;
  if (!new Set(["mock", "openhands", "hybrid"]).has(agentMode)) {
    throw new Error("QF_AGENT_MODE must be mock, openhands, or hybrid");
  }
  return {
    project: "q-fintelligence",
    processNamespace,
    apiHost: parseLoopbackHost("QF_API_HOST", "127.0.0.1"),
    apiPort: parsePort("QF_API_PORT", 27_872),
    webPort: parsePort("QF_WEB_PORT", 27_871),
    artifactRoot: process.env.QF_ARTIFACT_ROOT ?? ".local/artifacts",
    sqlitePath: process.env.QF_SQLITE_PATH ?? `${stateRoot}/qfintelligence.sqlite3`,
    stateRoot,
    openHandsSessionRoot: process.env.QF_OPENHANDS_SESSION_ROOT ?? ".local/openhands-sessions",
    openHandsSidecarPath: process.env.QF_OPENHANDS_SIDECAR_PATH ?? "workers/openhands_sidecar/run-isolated.sh",
    systemPromptVersion: process.env.QF_SYSTEM_PROMPT_VERSION ?? "qf-agent-p0.v1",
    agentMode: agentMode as RuntimeConfig["agentMode"],
    hardwarePolicy: loadHardwareExecutionPolicy(),
  };
}
