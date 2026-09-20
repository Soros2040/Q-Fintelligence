import { existsSync } from "node:fs";
import path from "node:path";

import { loadRuntimeConfig } from "../config.js";
import { openDatabase } from "../db/migrations.js";
import { WorkspaceRepository } from "../db/repository.js";
import { OpenHandsAcceptanceRepository } from "./openhands-acceptance-repository.js";
import { OpenHandsAcceptanceService } from "./openhands-acceptance-service.js";

const PROJECT_ROOT = process.cwd();
if (process.cwd() !== PROJECT_ROOT) throw new Error("OpenHands acceptance CLI must run in the unique WSL project root");
if (existsSync(path.join(PROJECT_ROOT, ".env"))) process.loadEnvFile(path.join(PROJECT_ROOT, ".env"));
process.env.QF_PROCESS_NAMESPACE = "qfintelligence";

const config = loadRuntimeConfig();
const migration = openDatabase(path.resolve(PROJECT_ROOT, config.sqlitePath), path.join(PROJECT_ROOT, "infra", "sqlite"));
const workspaceRepository = new WorkspaceRepository(migration.database);
const acceptanceRepository = new OpenHandsAcceptanceRepository(migration.database);
const service = new OpenHandsAcceptanceService(workspaceRepository, acceptanceRepository, config);
const command = process.argv[2] ?? "list";

try {
  if (command === "list") {
    process.stdout.write(`${JSON.stringify({ campaigns: service.list() }, null, 2)}\n`);
  } else if (command === "status") {
    const campaignId = process.argv[3];
    if (!campaignId) throw new Error("status requires a Campaign ID");
    process.stdout.write(`${JSON.stringify({ campaign: service.get(campaignId), audit: service.audit(campaignId) }, null, 2)}\n`);
  } else if (command === "start") {
    const campaignId = process.argv[3];
    if (!campaignId) throw new Error("start requires a Campaign ID");
    process.stdout.write(`${JSON.stringify(service.start(campaignId), null, 2)}\n`);
  } else if (command === "resume-start") {
    const campaignId = process.argv[3];
    if (!campaignId) throw new Error("resume-start requires a Campaign ID");
    process.stdout.write(`${JSON.stringify(service.resumeStart(campaignId), null, 2)}\n`);
  } else if (command === "compensate-start") {
    const campaignId = process.argv[3];
    if (!campaignId) throw new Error("compensate-start requires a Campaign ID");
    process.stdout.write(`${JSON.stringify(service.compensateStart(campaignId), null, 2)}\n`);
  } else if (command === "startup-complete") {
    const campaignId = process.argv[3];
    if (!campaignId) throw new Error("startup-complete requires a Campaign ID");
    process.stdout.write(`${JSON.stringify({ complete: service.startupComplete(campaignId) }, null, 2)}\n`);
  } else if (command === "complete-start") {
    const campaignId = process.argv[3];
    if (!campaignId) throw new Error("complete-start requires a Campaign ID");
    process.stdout.write(`${JSON.stringify(service.completeStart(campaignId), null, 2)}\n`);
  } else if (command === "audit") {
    const campaignId = process.argv[3];
    if (!campaignId) throw new Error("audit requires a Campaign ID");
    process.stdout.write(`${JSON.stringify(service.audit(campaignId), null, 2)}\n`);
  } else if (command === "finalize") {
    const campaignId = process.argv[3];
    if (!campaignId) throw new Error("finalize requires a Campaign ID");
    process.stdout.write(`${JSON.stringify(service.finalize(campaignId), null, 2)}\n`);
  } else if (command === "plan-fault") {
    const [campaignId, runId, kind, rawPid, targetProcessKey, beforeCheckpointHash] = process.argv.slice(3);
    if (!campaignId || !runId || !kind || !rawPid || !targetProcessKey || !beforeCheckpointHash) {
      throw new Error("plan-fault requires Campaign ID, Run ID, kind, PID, process key, and checkpoint hash");
    }
    if (kind !== "OPENHANDS_PROCESS_TERMINATION" && kind !== "SCIENCE_WORKER_TERMINATION") {
      throw new Error("unsupported OpenHands acceptance fault kind");
    }
    const faultId = acceptanceRepository.planFault({
      campaignId,
      runId,
      kind,
      targetProcessId: Number(rawPid),
      targetProcessKey,
      beforeCheckpointHash,
      beforeSnapshot: acceptanceRepository.captureStateSnapshot(campaignId, beforeCheckpointHash),
    });
    process.stdout.write(`${JSON.stringify({ faultId }, null, 2)}\n`);
  } else if (command === "latest-checkpoint") {
    const runId = process.argv[3];
    if (!runId) throw new Error("latest-checkpoint requires a Run ID");
    const row = migration.database.prepare(
      "SELECT payload_hash AS payloadHash, stage, created_at AS createdAt FROM openhands_acceptance_checkpoints WHERE run_id = ? ORDER BY created_at DESC LIMIT 1",
    ).get(runId);
    if (!row) throw new Error("Run has no durable checkpoint");
    process.stdout.write(`${JSON.stringify(row, null, 2)}\n`);
  } else if (command === "mark-fault-injected") {
    const faultId = process.argv[3];
    if (!faultId) throw new Error("mark-fault-injected requires a fault ID");
    const fault = acceptanceRepository.markFaultInjected(faultId);
    process.stdout.write(`${JSON.stringify({ faultId, state: fault.state }, null, 2)}\n`);
  } else if (command === "fault") {
    const [campaignId, runId, kind] = process.argv.slice(3);
    if (!campaignId || !runId || !kind) throw new Error("fault requires Campaign ID, Run ID, and kind");
    if (kind !== "OPENHANDS_PROCESS_TERMINATION" && kind !== "SCIENCE_WORKER_TERMINATION") {
      throw new Error("unsupported OpenHands acceptance fault kind");
    }
    process.stdout.write(`${JSON.stringify({ fault: acceptanceRepository.getFault(campaignId, runId, kind) }, null, 2)}\n`);
  } else if (command === "prepare-run-recovery-after-death") {
    const [runId, workerId, rawProcessId, rawLeaseGeneration] = process.argv.slice(3);
    if (!runId || !workerId || !rawProcessId || !rawLeaseGeneration) {
      throw new Error("prepare-run-recovery-after-death requires Run, worker, PID, and lease generation");
    }
    process.stdout.write(`${JSON.stringify(acceptanceRepository.prepareRunRecoveryAfterProcessDeath(
      runId,
      workerId,
      Number(rawProcessId),
      Number(rawLeaseGeneration),
    ), null, 2)}\n`);
  } else if (command === "injected-fault") {
    const [campaignId, runId, kind] = process.argv.slice(3);
    if (!campaignId || !runId || !kind) throw new Error("injected-fault requires Campaign ID, Run ID, and kind");
    if (kind !== "OPENHANDS_PROCESS_TERMINATION" && kind !== "SCIENCE_WORKER_TERMINATION") {
      throw new Error("unsupported OpenHands acceptance fault kind");
    }
    const fault = acceptanceRepository.getFault(campaignId, runId, kind);
    if (!fault || fault.state !== "INJECTED") throw new Error("matching injected OpenHands acceptance fault was not found");
    process.stdout.write(`${JSON.stringify(fault, null, 2)}\n`);
  } else if (command === "verify-fault-recovery") {
    const [faultId, afterCheckpointHash] = process.argv.slice(3);
    if (!faultId || !afterCheckpointHash) {
      throw new Error("verify-fault-recovery requires fault ID and after-checkpoint hash");
    }
    const verification = acceptanceRepository.verifyAndRecoverFault(faultId, afterCheckpointHash);
    process.stdout.write(`${JSON.stringify({ faultId, state: "RECOVERED", verification }, null, 2)}\n`);
  } else {
    throw new Error(`unknown OpenHands acceptance CLI command ${command}`);
  }
} finally {
  workspaceRepository.close();
}
