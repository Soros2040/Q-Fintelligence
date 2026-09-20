import { createHash } from "node:crypto";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import path from "node:path";

import type { ArtifactSummary, JsonObject } from "@q-fintelligence/contracts";

import { storeArtifact } from "../artifact-store.js";
import type { WorkspaceRepository } from "../db/repository.js";
import type { HardwareExecutionPolicy } from "../hardware-policy.js";
import type { P04Repository } from "./p04-repository.js";
import type { CampaignRepository } from "./repository.js";

export interface P04WorkerContext {
  projectRoot: string;
  campaignId: string;
  conversationId: string;
  runId: string;
  runWorkspace: string;
  campaignWorkspace: string;
  artifactRoot: string;
  workerId: string;
  workspaceRepository: WorkspaceRepository;
  campaignRepository: CampaignRepository;
  p04Repository: P04Repository;
  hardwarePolicy: HardwareExecutionPolicy;
}

export function p04Hash(value: unknown): string {
  return createHash("sha256").update(typeof value === "string" ? value : JSON.stringify(value)).digest("hex");
}

export function p04Json(value: unknown): JsonObject {
  return value as JsonObject;
}

export async function atomicP04Json(pathname: string, value: unknown): Promise<void> {
  await mkdir(path.dirname(pathname), { recursive: true });
  const temporary = `${pathname}.${process.pid}.tmp`;
  await writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, { encoding: "utf8", mode: 0o600 });
  await rename(temporary, pathname);
}

export async function readP04Json<T>(pathname: string): Promise<T> {
  return JSON.parse(await readFile(pathname, "utf8")) as T;
}

export async function registerP04Artifact(input: {
  context: P04WorkerContext;
  data: Uint8Array;
  mediaType: string;
  producer: string;
  logicalName: string;
  relativeOutputPath: string;
  parentHashes?: string[];
}): Promise<ArtifactSummary> {
  const manifest = await storeArtifact({
    root: input.context.artifactRoot,
    data: input.data,
    mediaType: input.mediaType,
    producer: input.producer,
    ...(input.parentHashes ? { parentHashes: input.parentHashes } : {}),
  });
  const artifact = input.context.workspaceRepository.registerArtifact(input.context.conversationId, manifest);
  input.context.p04Repository.claimArtifact(
    input.context.runId,
    artifact.sha256,
    input.logicalName,
    input.relativeOutputPath,
  );
  return artifact;
}

export async function registerP04JsonArtifact(input: {
  context: P04WorkerContext;
  value: unknown;
  producer: string;
  logicalName: string;
  relativeOutputPath: string;
  parentHashes?: string[];
}): Promise<ArtifactSummary> {
  return registerP04Artifact({
    ...input,
    data: new TextEncoder().encode(`${JSON.stringify(input.value, null, 2)}\n`),
    mediaType: "application/json",
  });
}

export async function registerP04TextArtifact(input: {
  context: P04WorkerContext;
  value: string;
  producer: string;
  logicalName: string;
  relativeOutputPath: string;
  parentHashes?: string[];
}): Promise<ArtifactSummary> {
  return registerP04Artifact({
    ...input,
    data: new TextEncoder().encode(input.value),
    mediaType: "text/plain",
  });
}

export async function executeP04JsonStage<T>(input: {
  context: P04WorkerContext;
  stage: string;
  actionType: string;
  expectedEvidence: string;
  inputIdentity: unknown;
  outputPath: string;
  timing: "active" | "external";
  producer: string;
  logicalName: string;
  parentHashes?: string[];
  run: () => Promise<T>;
}): Promise<{ value: T; artifact: ArtifactSummary; reused: boolean }> {
  const identityHash = p04Hash(input.inputIdentity);
  const idempotencyKey = `${input.context.campaignId}:${input.context.runId}:${input.stage}:${identityHash}`;
  const action = input.context.campaignRepository.beginAction({
    campaignId: input.context.campaignId,
    stage: `p04/${input.context.runId}/${input.stage}`,
    actionType: input.actionType,
    idempotencyKey,
    inputHash: identityHash,
    expectedEvidence: input.expectedEvidence,
  });
  if (action.status === "COMPLETED" && action.outputArtifactSha256) {
    input.context.p04Repository.appendEvent(input.context.runId, "STAGE_REUSED", p04Json({
      stage: input.stage,
      actionId: action.actionId,
      artifactSha256: action.outputArtifactSha256,
    }));
    return {
      value: await readP04Json<T>(input.outputPath),
      artifact: input.context.workspaceRepository.getArtifact(action.outputArtifactSha256),
      reused: true,
    };
  }
  input.context.p04Repository.appendEvent(input.context.runId, "STAGE_STARTED", p04Json({
    stage: input.stage,
    actionId: action.actionId,
    expectedEvidence: input.expectedEvidence,
  }));
  const startedAt = performance.now();
  try {
    const value = await input.run();
    await atomicP04Json(input.outputPath, value);
    const artifact = await registerP04JsonArtifact({
      context: input.context,
      value,
      producer: input.producer,
      logicalName: input.logicalName,
      relativeOutputPath: path.relative(input.context.projectRoot, input.outputPath),
      ...(input.parentHashes ? { parentHashes: input.parentHashes } : {}),
    });
    input.context.campaignRepository.completeAction(action.actionId, "COMPLETED", artifact.sha256);
    input.context.p04Repository.appendEvent(input.context.runId, "STAGE_COMPLETED", p04Json({
      stage: input.stage,
      actionId: action.actionId,
      artifactSha256: artifact.sha256,
    }));
    return { value, artifact, reused: false };
  } catch (error) {
    const failure = {
      schemaVersion: "qf.p04.failure-evidence.v1",
      stage: input.stage,
      errorType: error instanceof Error ? error.name : "Error",
      message: (error instanceof Error ? error.message : String(error)).slice(0, 1000),
    };
    const artifact = await registerP04JsonArtifact({
      context: input.context,
      value: failure,
      producer: `${input.producer}.failure`,
      logicalName: `${input.logicalName}-failure-${Date.now()}`,
      relativeOutputPath: `${path.relative(input.context.projectRoot, input.outputPath)}.failure`,
      ...(input.parentHashes ? { parentHashes: input.parentHashes } : {}),
    });
    input.context.campaignRepository.completeAction(action.actionId, "FAILED", artifact.sha256, p04Json(failure));
    input.context.p04Repository.appendEvent(input.context.runId, "STAGE_FAILED", p04Json({
      stage: input.stage,
      actionId: action.actionId,
      artifactSha256: artifact.sha256,
      message: failure.message,
    }));
    throw error;
  } finally {
    input.context.campaignRepository.addTiming(
      input.context.campaignId,
      input.timing,
      (performance.now() - startedAt) / 1000,
    );
  }
}
