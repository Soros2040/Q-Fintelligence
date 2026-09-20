import { createHash } from "node:crypto";

import type { P03LaunchAuthorization } from "@q-fintelligence/contracts";

export const P03_AUTHORIZATION: P03LaunchAuthorization = Object.freeze({
  schemaVersion: "qf.p03.authorization.v1",
  stageId: "P03",
  providerAlias: "openai",
  baseUrlAlias: "getoken",
  modelId: "gpt-5.6-sol",
  allowModelFallback: false,
  resumeCampaignId: "redacted-run-id",
  formalTestSealed: true,
  backend: "tianyan176",
  maxNewHardwareJobs: 2,
  shotsPerJob: 100,
  maxNewHardwareShots: 200,
  conservativeSecondsPerJob: 240,
  maxNewHardwareExecutionSeconds: 600,
  queueCheckpointMinutes: 30,
  queueForegroundMinutes: 180,
  authorizeCloudResubmit: false,
  authorizeGitCommit: false,
  authorizeGitPush: false,
  authorizationSource:
    "project_archive/03_argumentation_outputs/量融智枢_P03已授权启动输入_20260721_194424.md",
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

export function p03AuthorizationHash(authorization: P03LaunchAuthorization = P03_AUTHORIZATION): string {
  return createHash("sha256").update(canonicalJson(authorization)).digest("hex");
}

export function assertP03Authorization(authorization: P03LaunchAuthorization): void {
  if (
    authorization.backend !== "tianyan176"
    || authorization.maxNewHardwareJobs > 2
    || authorization.shotsPerJob !== 100
    || authorization.maxNewHardwareShots > 200
    || authorization.maxNewHardwareExecutionSeconds > 600
    || !authorization.formalTestSealed
    || authorization.allowModelFallback
    || authorization.authorizeCloudResubmit
    || authorization.authorizeGitCommit
    || authorization.authorizeGitPush
  ) {
    throw new Error("P03 authorization exceeds the fixed provider, hardware, formal-test, cloud, or Git boundary");
  }
  if (p03AuthorizationHash(authorization) !== p03AuthorizationHash(P03_AUTHORIZATION)) {
    throw new Error("P03 authorization only applies to the exact canonical fields");
  }
}
