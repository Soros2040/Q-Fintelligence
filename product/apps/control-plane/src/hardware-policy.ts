import { createHash } from "node:crypto";

export type HardwareMode = "READ_ONLY" | "ONE_JOB";

export interface HardwareExecutionPolicy {
  mode: HardwareMode;
  target: "tianyan176";
  maxNewHardwareJobs: number;
  shotsPerJob: number;
  authorizationBasis: string | null;
}

export const READ_ONLY_HARDWARE_POLICY: HardwareExecutionPolicy = {
  mode: "READ_ONLY",
  target: "tianyan176",
  maxNewHardwareJobs: 0,
  shotsPerJob: 0,
  authorizationBasis: null,
};

export const HISTORICAL_HARDWARE_AUTHORIZATION_BASES = new Set([
  "P04_USER_AUTHORIZATION_20260722",
  "P05_USER_REQUEST_20260722",
  "P07_USER_REQUEST_20260722",
  "P15_USER_REQUEST_20260725",
  "P16_USER_REQUEST_20260725",
]);

const HARDWARE_MUTATION_ACTIONS = new Set(["submit", "submit_batch"]);

export class HardwarePolicyError extends Error {
  readonly code: "HARDWARE_NOT_AUTHORIZED" | "HARDWARE_POLICY_INVALID";

  constructor(
    message: string,
    code: "HARDWARE_NOT_AUTHORIZED" | "HARDWARE_POLICY_INVALID" = "HARDWARE_NOT_AUTHORIZED",
  ) {
    super(message);
    this.name = "HardwarePolicyError";
    this.code = code;
  }
}

function nonNegativeInteger(
  env: NodeJS.ProcessEnv,
  name: "MAX_NEW_HARDWARE_JOBS" | "SHOTS_PER_JOB",
  fallback: number,
): number {
  const value = Number(env[name] ?? fallback);
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new HardwarePolicyError(`${name} must be a non-negative integer`, "HARDWARE_POLICY_INVALID");
  }
  return value;
}

export function loadHardwareExecutionPolicy(env: NodeJS.ProcessEnv = process.env): HardwareExecutionPolicy {
  const mode = env.HARDWARE_MODE ?? "READ_ONLY";
  if (mode !== "READ_ONLY" && mode !== "ONE_JOB") {
    throw new HardwarePolicyError("HARDWARE_MODE must be READ_ONLY or ONE_JOB", "HARDWARE_POLICY_INVALID");
  }
  const target = env.HARDWARE_TARGET ?? "tianyan176";
  if (target !== "tianyan176") {
    throw new HardwarePolicyError("HARDWARE_TARGET must remain tianyan176", "HARDWARE_POLICY_INVALID");
  }
  const maxNewHardwareJobs = nonNegativeInteger(env, "MAX_NEW_HARDWARE_JOBS", mode === "READ_ONLY" ? 0 : 1);
  const shotsPerJob = nonNegativeInteger(env, "SHOTS_PER_JOB", mode === "READ_ONLY" ? 0 : 100);
  const authorizationBasis = env.HARDWARE_AUTHORIZATION_BASIS?.trim() || null;

  if (mode === "READ_ONLY") {
    if (maxNewHardwareJobs !== 0 || shotsPerJob !== 0) {
      throw new HardwarePolicyError(
        "READ_ONLY requires MAX_NEW_HARDWARE_JOBS=0 and SHOTS_PER_JOB=0",
        "HARDWARE_POLICY_INVALID",
      );
    }
  } else {
    if (maxNewHardwareJobs !== 1 || shotsPerJob < 1) {
      throw new HardwarePolicyError(
        "ONE_JOB requires MAX_NEW_HARDWARE_JOBS=1 and positive SHOTS_PER_JOB",
        "HARDWARE_POLICY_INVALID",
      );
    }
    if (!authorizationBasis) {
      throw new HardwarePolicyError(
        "ONE_JOB requires a current HARDWARE_AUTHORIZATION_BASIS",
        "HARDWARE_POLICY_INVALID",
      );
    }
    if (HISTORICAL_HARDWARE_AUTHORIZATION_BASES.has(authorizationBasis)) {
      throw new HardwarePolicyError(
        "historical P04/P05/P07/P15/P16 authorization cannot authorize a new execution",
        "HARDWARE_POLICY_INVALID",
      );
    }
  }

  return { mode, target, maxNewHardwareJobs, shotsPerJob, authorizationBasis };
}

export function assertHardwareMutationAuthorized(
  policy: HardwareExecutionPolicy,
  input: {
    operation: string;
    authorizationBasis?: unknown;
    jobCount?: number;
    shotsPerJob?: number;
    target?: string;
  },
): void {
  if (policy.mode !== "ONE_JOB") {
    throw new HardwarePolicyError(
      `${input.operation} is NOT_AUTHORIZED while HARDWARE_MODE=READ_ONLY`,
    );
  }
  if (
    typeof input.authorizationBasis !== "string"
    || input.authorizationBasis !== policy.authorizationBasis
    || HISTORICAL_HARDWARE_AUTHORIZATION_BASES.has(input.authorizationBasis)
  ) {
    throw new HardwarePolicyError(`${input.operation} is not bound to the current execution authorization`);
  }
  const jobCount = input.jobCount ?? 1;
  const shotsPerJob = input.shotsPerJob ?? policy.shotsPerJob;
  const target = input.target ?? policy.target;
  if (
    jobCount < 1
    || jobCount > policy.maxNewHardwareJobs
    || shotsPerJob !== policy.shotsPerJob
    || target !== policy.target
  ) {
    throw new HardwarePolicyError(`${input.operation} exceeds the frozen hardware target, Job, or shots limits`);
  }
}

export function isHardwareMutationAction(action: unknown): boolean {
  return typeof action === "string" && HARDWARE_MUTATION_ACTIONS.has(action);
}

export function hardwarePolicyPrompt(policy: HardwareExecutionPolicy): string {
  const basisHash = policy.authorizationBasis
    ? createHash("sha256").update(policy.authorizationBasis).digest("hex")
    : "NONE";
  return [
    "Current execution hardware policy (authoritative, fail-closed):",
    `HARDWARE_MODE=${policy.mode}`,
    `HARDWARE_TARGET=${policy.target}`,
    `MAX_NEW_HARDWARE_JOBS=${policy.maxNewHardwareJobs}`,
    `SHOTS_PER_JOB=${policy.shotsPerJob}`,
    `HARDWARE_AUTHORIZATION_BASIS_SHA256=${basisHash}`,
    "Historical P04/P05/P07/P15/P16 authorization literals never authorize this conversation.",
    policy.mode === "READ_ONLY"
      ? "Discovery, configuration, local simulation, validation, and stored-Query-ID query may remain read-only; prepare, hardware approval, submit, and new Query ID creation are NOT_AUTHORIZED."
      : "Only the exact current authorization basis and frozen limits may authorize one new Job; there is no fallback or scope expansion.",
  ].join("\n");
}
