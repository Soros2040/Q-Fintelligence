import { createHash } from "node:crypto";

import type { P02LaunchAuthorization } from "@q-fintelligence/contracts";

export const P02_AUTHORIZATION: P02LaunchAuthorization = Object.freeze({
  schemaVersion: "qf.p02.authorization.v1",
  taskTitle: "六股票风险图到天衍云模拟与真机的长程真实链路",
  providerAlias: "openai",
  baseUrlAlias: "getoken",
  modelId: "gpt-5.6-sol",
  minimumRuntimeMinutes: 150,
  maximumRuntimeMinutes: 360,
  formalTestSealed: true,
  tushareReadonly: true,
  localMarketDataCache: true,
  tianyanCloudSimulator: true,
  tianyanHardware: true,
  maxSimulatorJobs: 4,
  simulatorShotsPerJob: 5_000,
  maxHardwareJobs: 2,
  hardwareMaxTotalShots: 10_000,
  hardwareMaxCumulativeExecutionSeconds: 600,
  allowModelFallback: false,
  authorizeGitCommit: false,
  authorizeGitPush: false,
  authorizationSource:
    "project_archive/03_argumentation_outputs/量融智枢_P02已授权启动输入_20260721_140114.md",
});

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map((item) => canonicalJson(item)).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => `${JSON.stringify(key)}:${canonicalJson(item)}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

export function authorizationHash(authorization: P02LaunchAuthorization = P02_AUTHORIZATION): string {
  return createHash("sha256").update(canonicalJson(authorization)).digest("hex");
}

export function assertStandingAuthorization(authorization: P02LaunchAuthorization): void {
  if (!authorization.formalTestSealed || authorization.authorizeGitCommit || authorization.authorizeGitPush) {
    throw new Error("P02 authorization cannot unseal the formal test set or authorize Git delivery");
  }
  if (authorization.maxHardwareJobs > 2 || authorization.hardwareMaxCumulativeExecutionSeconds > 600) {
    throw new Error("P02 hardware limits exceed the user authorization");
  }
  if (authorizationHash(authorization) !== authorizationHash(P02_AUTHORIZATION)) {
    throw new Error("P02 standing authorization only applies to the exact canonical fields");
  }
}
