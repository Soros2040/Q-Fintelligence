import { createHash } from "node:crypto";

import type { P04LaunchAuthorization } from "@q-fintelligence/contracts";

export const P04_AUTHORIZATION: P04LaunchAuthorization = {
  schemaVersion: "qf.p04.authorization.v1",
  stageId: "P04",
  providerAlias: "openai",
  baseUrlAlias: "getoken",
  modelId: "gpt-5.6-sol",
  allowModelFallback: false,
  minimumRuntimeMinutes: 360,
  maximumRuntimeMinutes: 480,
  formalTestSealed: true,
  runLanes: ["finance", "tool_factory", "evidence_audit"],
  backend: "tianyan176",
  remoteSimulatorEnabled: false,
  maxActiveHardwareLeases: 1,
  requireLocalCqlibPreflight: true,
  authorizeFaultInjection: true,
  authorizeGitCommit: false,
  authorizeGitPush: false,
  authorizationSource: "User P04 authorization recorded 2026-07-22",
};

export function p04AuthorizationHash(): string {
  return createHash("sha256").update(JSON.stringify(P04_AUTHORIZATION)).digest("hex");
}

export function assertP04Authorization(): void {
  if (P04_AUTHORIZATION.modelId !== "gpt-5.6-sol" || P04_AUTHORIZATION.allowModelFallback) {
    throw new Error("P04 fixed-model authorization is invalid");
  }
  if (P04_AUTHORIZATION.minimumRuntimeMinutes !== 360 || P04_AUTHORIZATION.formalTestSealed !== true) {
    throw new Error("P04 duration or formal-test seal is invalid");
  }
  if (P04_AUTHORIZATION.backend !== "tianyan176" || P04_AUTHORIZATION.remoteSimulatorEnabled) {
    throw new Error("P04 TianYan boundary is invalid");
  }
  if (P04_AUTHORIZATION.authorizeGitCommit || P04_AUTHORIZATION.authorizeGitPush) {
    throw new Error("P04 Git authorization must remain disabled");
  }
}
