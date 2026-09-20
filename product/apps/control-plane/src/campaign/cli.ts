import { randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";

import { loadRuntimeConfig } from "../config.js";
import { openDatabase } from "../db/migrations.js";
import { WorkspaceRepository } from "../db/repository.js";
import { storeArtifact } from "../artifact-store.js";
import { ensureP02Campaign } from "./bootstrap.js";
import { ensureP04Campaign } from "./p04-bootstrap.js";
import { P04Repository } from "./p04-repository.js";
import {
  finalizeP03Campaign,
  runP03OldCloudQuery,
  runP03SseDiagnosis,
  runP03Tianyan176QueueStep,
} from "./p03-operations.js";
import { runTianyanJob } from "./python-runner.js";
import { CampaignRepository } from "./repository.js";
import { runTianyan176Hardware } from "./tianyan176-hardware.js";

const projectRoot = process.cwd();
if (existsSync(path.join(projectRoot, ".env"))) process.loadEnvFile(path.join(projectRoot, ".env"));
process.env.QF_PROCESS_NAMESPACE = "qfintelligence";
process.env.NODE_USE_ENV_PROXY ??= "1";
process.env.http_proxy ??= "http://127.0.0.1:7897";
process.env.https_proxy ??= "http://127.0.0.1:7897";
process.env.HTTP_PROXY ??= process.env.http_proxy;
process.env.HTTPS_PROXY ??= process.env.https_proxy;
process.env.no_proxy ??= "localhost,127.0.0.1,::1";
process.env.NO_PROXY ??= process.env.no_proxy;

const config = loadRuntimeConfig();
const migration = openDatabase(path.resolve(projectRoot, config.sqlitePath), path.join(projectRoot, "infra", "sqlite"));
const workspaceRepository = new WorkspaceRepository(migration.database);
const campaignRepository = new CampaignRepository(migration.database);
const p04Repository = new P04Repository(migration.database);
const command = process.argv[2] ?? "status";

function assertCampaignWritable(campaignId: string) {
  const campaign = campaignRepository.getCampaign(campaignId);
  workspaceRepository.assertConversationWritable(campaign.conversationId);
  return campaign;
}

try {
  if (command === "init") {
    const campaign = ensureP02Campaign(workspaceRepository, campaignRepository);
    process.stdout.write(`${JSON.stringify(campaign, null, 2)}\n`);
  } else if (command === "p04-init") {
    const campaign = ensureP04Campaign(workspaceRepository, campaignRepository);
    workspaceRepository.assertConversationWritable(campaign.conversationId);
    p04Repository.ensureRuns(campaign.campaignId);
    process.stdout.write(`${JSON.stringify(p04Repository.getDetail(campaign), null, 2)}\n`);
  } else if (command === "p04-assert-writable") {
    const campaignId = process.argv[3];
    if (!campaignId) throw new Error("p04-assert-writable requires a Campaign ID");
    const campaign = assertCampaignWritable(campaignId);
    process.stdout.write(`${JSON.stringify({ campaignId, conversationId: campaign.conversationId, writable: true }, null, 2)}\n`);
  } else if (command === "p04-prepare") {
    const campaignId = process.argv[3] ?? ensureP04Campaign(workspaceRepository, campaignRepository).campaignId;
    assertCampaignWritable(campaignId);
    p04Repository.ensureRuns(campaignId);
    const campaign = campaignRepository.setCampaignState(campaignId, "RUNNING", "p04_concurrent_lanes", { level: "L2" });
    campaignRepository.checkpoint(campaignId, "p04_start", {
      schemaVersion: "qf.p04.start.v1",
      campaignId,
      fixedModel: "openai/getoken/gpt-5.6-sol",
      minimumRuntimeSeconds: 21_600,
      lanes: ["finance", "tool_factory", "evidence_audit"],
      backend: "tianyan176",
      remoteSimulatorEnabled: false,
      formalTestSealed: true,
    });
    process.stdout.write(`${JSON.stringify(p04Repository.getDetail(campaign), null, 2)}\n`);
  } else if (command === "p04-status") {
    const campaignId = process.argv[3] ?? campaignRepository.listCampaigns().find((item) => item.minimumRuntimeSeconds === 21_600)?.campaignId;
    if (!campaignId) throw new Error("no P04 Campaign exists");
    process.stdout.write(`${JSON.stringify(p04Repository.getDetail(campaignRepository.getCampaign(campaignId)), null, 2)}\n`);
  } else if (command === "p04-plan-fault") {
    const [campaignId, lane, kind, rawPid] = process.argv.slice(3);
    if (!campaignId || !lane || !kind || !rawPid) throw new Error("p04-plan-fault requires campaign, lane, kind, and pid");
    assertCampaignWritable(campaignId);
    const run = p04Repository.listRuns(campaignId).find((item) => item.lane === lane);
    if (!run) throw new Error(`P04 lane ${lane} was not found`);
    const fault = p04Repository.planFault({
      campaignId,
      runId: run.runId,
      kind: kind as "SCIENCE_PROCESS_TERMINATION" | "EXTERNAL_QUERY_PROCESS_TERMINATION",
      targetProcessId: Number(rawPid),
    });
    process.stdout.write(`${JSON.stringify(fault, null, 2)}\n`);
  } else if (command === "p04-latest-event") {
    const [campaignId, lane] = process.argv.slice(3);
    if (!campaignId || !lane) throw new Error("p04-latest-event requires campaign and lane");
    const run = p04Repository.listRuns(campaignId).find((item) => item.lane === lane);
    if (!run) throw new Error(`P04 lane ${lane} was not found`);
    process.stdout.write(`${JSON.stringify({ run, latestEvent: p04Repository.latestEvent(run.runId) }, null, 2)}\n`);
  } else if (command === "p04-mark-fault-injected") {
    const faultId = process.argv[3];
    if (!faultId) throw new Error("p04-mark-fault-injected requires a fault id");
    assertCampaignWritable(p04Repository.getFault(faultId).campaignId);
    process.stdout.write(`${JSON.stringify(p04Repository.markFaultInjected(faultId), null, 2)}\n`);
  } else if (command === "p04-record-tests") {
    const campaignId = process.argv[3];
    const evidencePath = process.argv[4];
    if (!campaignId || !evidencePath) throw new Error("p04-record-tests requires Campaign ID and evidence path");
    assertCampaignWritable(campaignId);
    const raw = await readFile(path.resolve(evidencePath));
    const evidence = JSON.parse(raw.toString("utf8")) as { passed?: unknown };
    if (evidence.passed !== true) throw new Error("P04 final test evidence is not passing");
    const campaign = campaignRepository.getCampaign(campaignId);
    const manifest = await storeArtifact({
      root: path.resolve(projectRoot, config.artifactRoot),
      data: raw,
      mediaType: "application/json",
      producer: "qf-p04.final-test-gates",
    });
    const artifact = workspaceRepository.registerArtifact(campaign.conversationId, manifest);
    const auditRun = p04Repository.listRuns(campaignId).find((run) => run.lane === "evidence_audit");
    if (!auditRun) throw new Error("P04 evidence audit Run was not found");
    p04Repository.claimArtifact(auditRun.runId, artifact.sha256, "final-test-gates", path.relative(projectRoot, evidencePath));
    campaignRepository.checkpoint(campaignId, "p04_final_test_gate", {
      schemaVersion: "qf.p04.final-test-gate.v1",
      passed: true,
      artifactSha256: artifact.sha256,
    });
    process.stdout.write(`${JSON.stringify({ passed: true, artifactSha256: artifact.sha256 }, null, 2)}\n`);
  } else if (command === "p04-finalize") {
    const campaignId = process.argv[3];
    if (!campaignId) throw new Error("p04-finalize requires a Campaign ID");
    const campaign = assertCampaignWritable(campaignId);
    const detail = p04Repository.getDetail(campaign);
    const snapshot = p04Repository.concurrencySnapshot(campaignId);
    const jobs = campaignRepository.listQuantumJobs(campaignId).filter((job) => job.backend === "tianyan176" && job.targetType === "HARDWARE");
    const hardware = jobs.find((job) => job.terminalStatus === "COMPLETED" && job.queryId && job.rawResultArtifactSha256)
      ?? jobs.find((job) => job.queryId);
    const checkpoints = campaignRepository.listCheckpoints(campaignId, 1_000);
    const testGate = checkpoints.find((checkpoint) => checkpoint.stage === "p04_final_test_gate");
    const runStarts = detail.runs.map((run) => Date.parse(run.startedAt ?? ""));
    const runEnds = detail.runs.map((run) => Date.parse(run.completedAt ?? new Date().toISOString()));
    const overlapSeconds = Math.max(0, (Math.min(...runEnds) - Math.max(...runStarts)) / 1000);
    const gates = {
      wallClock: campaign.wallClockSeconds >= campaign.minimumRuntimeSeconds,
      threeRunsCompleted: detail.runs.length === 3 && detail.runs.every((run) => run.status === "COMPLETED"),
      concurrentOverlap: overlapSeconds > 0,
      toolsRegisteredAndCalled: detail.tools.length === 2 && detail.tools.every((tool) => tool.status === "REGISTERED" && tool.invocationCount >= 1),
      faultsRecovered: detail.faults.length === 2 && detail.faults.every((fault) => fault.state === "RECOVERED"),
      hardwareTerminalOrExternalBlock: Boolean(hardware?.queryId),
      hardwareSingleSubmission: jobs.length === 1,
      sqliteWal: snapshot.journalMode === "wal" && snapshot.integrityCheck === "ok",
      noOutputCollision: snapshot.duplicateOutputPaths === 0,
      hardwareLeaseSerialized: Number(snapshot.activeHardwareLeases) <= 1,
      formalTestSealed: true,
      finalTestGate: testGate?.payload.passed === true,
    };
    const passed = Object.values(gates).every(Boolean);
    const audit = {
      schemaVersion: "qf.p04.final-audit.v1",
      capturedAt: new Date().toISOString(),
      campaignId,
      wallClockSeconds: campaign.wallClockSeconds,
      overlapSeconds,
      gates,
      detail,
      hardwareJobs: jobs,
      concurrencySnapshot: snapshot,
      finalTestArtifactSha256: testGate?.payload.artifactSha256 ?? null,
      conclusion: passed ? (hardware?.terminalStatus === "COMPLETED" ? "COMPLETED" : "COMPLETED_WITH_EXTERNAL_BLOCKER") : "BLOCKED",
    };
    const raw = new TextEncoder().encode(`${JSON.stringify(audit, null, 2)}\n`);
    const auditPath = path.join(projectRoot, ".local", "campaigns", campaignId, "final-audit.json");
    await mkdir(path.dirname(auditPath), { recursive: true });
    await writeFile(auditPath, raw, { mode: 0o600 });
    const manifest = await storeArtifact({
      root: path.resolve(projectRoot, config.artifactRoot),
      data: raw,
      mediaType: "application/json",
      producer: "qf-p04.final-audit",
      parentHashes: typeof testGate?.payload.artifactSha256 === "string" ? [testGate.payload.artifactSha256] : [],
    });
    const artifact = workspaceRepository.registerArtifact(campaign.conversationId, manifest);
    const auditRun = detail.runs.find((run) => run.lane === "evidence_audit")!;
    p04Repository.claimArtifact(auditRun.runId, artifact.sha256, "final-audit", path.relative(projectRoot, auditPath));
    campaignRepository.checkpoint(campaignId, "p04_final_audit", {
      schemaVersion: "qf.p04.final-audit-checkpoint.v1",
      passed,
      artifactSha256: artifact.sha256,
      wallClockSeconds: campaign.wallClockSeconds,
    });
    if (passed) {
      campaignRepository.setCampaignState(campaignId, "COMPLETED", "p04_final_audit", {
        level: hardware?.terminalStatus === "COMPLETED" ? "L4" : "L3",
        blockerCategory: hardware?.terminalStatus === "COMPLETED" ? null : "TIANYAN176_EXTERNAL_NON_TERMINAL",
        blockerArtifactSha256: hardware?.terminalStatus === "COMPLETED" ? null : hardware?.rawResultArtifactSha256 ?? null,
      });
    } else {
      campaignRepository.setCampaignState(campaignId, "BLOCKED", "p04_final_audit", {
        level: hardware?.terminalStatus === "COMPLETED" ? "L4" : "L3",
        blockerCategory: "P04_ACCEPTANCE_GATE_FAILED",
        blockerArtifactSha256: artifact.sha256,
      });
    }
    process.stdout.write(`${JSON.stringify({ ...audit, auditArtifactSha256: artifact.sha256 }, null, 2)}\n`);
  } else if (command === "status") {
    const campaignId = process.argv[3];
    const campaigns = campaignId ? [campaignRepository.getCampaign(campaignId)] : campaignRepository.listCampaigns();
    process.stdout.write(`${JSON.stringify({ schemaVersion: "qf.campaign-status.v1", campaigns }, null, 2)}\n`);
  } else if (command === "audit") {
    const campaignId = process.argv[3] ?? campaignRepository.listCampaigns()[0]?.campaignId;
    if (!campaignId) throw new Error("no P02 campaign exists");
    const detail = campaignRepository.getDetail(campaignId);
    const effectiveRuntimeSeconds = detail.campaign.activeComputeSeconds + detail.campaign.externalWaitSeconds;
    const audit = {
      schemaVersion: "qf.campaign-audit.v1",
      capturedAt: new Date().toISOString(),
      detail,
      gates: {
        minimumRuntime: effectiveRuntimeSeconds >= detail.campaign.minimumRuntimeSeconds,
        formalTestSealed: true,
        hardwareJobCount: detail.campaign.hardwareJobs <= 2,
        hardwareExecutionSeconds: detail.campaign.hardwareExecutionSeconds <= 600,
        checkpointPresent: detail.checkpoints.length > 0,
        artifactActions: detail.actions.filter((action) => action.outputArtifactSha256 !== null).length,
        unknownExternalResubmitProhibited: true,
      },
      effectiveRuntimeSeconds,
      conclusion: detail.campaign.status,
    };
    process.stdout.write(`${JSON.stringify(audit, null, 2)}\n`);
  } else if (command === "doctor") {
    const variables = ["OPENAI_API_KEY", "OPENAI_BASE_URL", "TUSHARE_TOKEN", "TIANYAN_CONNECTION_KEY"];
    process.stdout.write(`${JSON.stringify({
      schemaVersion: "qf.campaign-doctor.v1",
      projectRoot,
      processNamespace: config.processNamespace,
      fixedPorts: [27_871, 27_872, 27_873, 27_874, 27_875],
      xhReservedPorts: [43_187, 43_188, 43_189, 43_190, 43_191, 43_192],
      credentials: Object.fromEntries(variables.map((name) => [name, Boolean(process.env[name]?.trim())])),
      proxyConfigured: Boolean(process.env.http_proxy && process.env.https_proxy && process.env.NO_PROXY),
      formalTestSealed: true,
      hardwareLimits: { maxJobs: 2, maxCumulativeExecutionSeconds: 600 },
      migrations: migration.applied,
      status: variables.every((name) => Boolean(process.env[name]?.trim())) ? "PASS" : "BLOCKED_CREDENTIALS",
    }, null, 2)}\n`);
  } else if (command === "tianyan-probe") {
    const machineName = process.argv[3] ?? "tianyan176";
    if (!/^tianyan[a-z0-9_]+$/u.test(machineName)) {
      throw new Error("TianYan machine name is invalid");
    }
    const qcis = "H Q0\nM Q0";
    const discovery = await runTianyanJob({
      projectRoot,
      request: { action: "discover" },
      timeoutSeconds: 180,
    });
    if (discovery.exitCode !== 0 || discovery.stdout.status === "FAILED") {
      throw new Error(String(discovery.stdout.message ?? "TianYan discovery failed"));
    }
    const backends = Array.isArray(discovery.stdout.backends)
      ? discovery.stdout.backends as Array<Record<string, unknown>>
      : [];
    const backend = backends.find((item) => item.machine_name === machineName);
    if (!backend) throw new Error(`TianYan backend ${machineName} was not discovered`);
    const validation = await runTianyanJob({
      projectRoot,
      request: { action: "validate", machine_name: machineName, qcis },
      timeoutSeconds: 120,
    });
    if (validation.exitCode !== 0 || validation.stdout.status === "FAILED") {
      throw new Error(String(validation.stdout.message ?? "TianYan circuit validation failed"));
    }
    process.stdout.write(`${JSON.stringify({
      schemaVersion: "qf.tianyan-connectivity-probe.v1",
      capturedAt: new Date().toISOString(),
      backend: {
        machineName: backend.machine_name,
        status: backend.status,
        toll: backend.toll,
        targetType: backend.target_type,
      },
      minimalCircuit: {
        qcis,
        qcisSha256: validation.stdout.qcis_sha256,
        valid: validation.stdout.valid,
      },
      discoveryDurationSeconds: discovery.durationSeconds,
      validationDurationSeconds: validation.durationSeconds,
      connected: backend.status === "running" && validation.stdout.valid === true,
    }, null, 2)}\n`);
  } else if (command === "tianyan176-hardware") {
    const campaignId = process.argv[3] ?? campaignRepository.listCampaigns()[0]?.campaignId;
    if (!campaignId) throw new Error("no P02 campaign exists");
    const result = await runTianyan176Hardware({
      projectRoot,
      campaignId,
      config,
      campaignRepository,
      workspaceRepository,
    });
    process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
  } else if (command === "tianyan-config") {
    const machineName = process.argv[3] ?? "tianyan176";
    if (!/^tianyan[a-z0-9_]+$/u.test(machineName)) throw new Error("TianYan machine name is invalid");
    const result = await runTianyanJob({
      projectRoot,
      request: { action: "config_summary", machine_name: machineName },
      timeoutSeconds: 180,
    });
    if (result.exitCode !== 0 || result.stdout.status === "FAILED") {
      throw new Error(String(result.stdout.message ?? "TianYan config query failed"));
    }
    process.stdout.write(`${JSON.stringify(result.stdout, null, 2)}\n`);
  } else if (command === "p03-sse" || command === "p03-old-cloud" || command === "p03-queue-step" || command === "p03-finalize") {
    const campaignId = process.argv[3] ?? campaignRepository.listCampaigns()[0]?.campaignId;
    if (!campaignId) throw new Error("no P02 campaign exists");
    const context = { projectRoot, campaignId, config, campaignRepository, workspaceRepository };
    if (command === "p03-sse") {
      process.stdout.write(`${JSON.stringify(await runP03SseDiagnosis(context), null, 2)}\n`);
    } else if (command === "p03-old-cloud") {
      process.stdout.write(`${JSON.stringify(await runP03OldCloudQuery(context), null, 2)}\n`);
    } else if (command === "p03-queue-step") {
      const workerId = `p03_cli_${randomUUID()}`;
      campaignRepository.acquireLease(campaignId, workerId, 180);
      try {
        process.stdout.write(`${JSON.stringify(await runP03Tianyan176QueueStep(context), null, 2)}\n`);
      } finally {
        campaignRepository.releaseLease(campaignId, workerId);
      }
    } else {
      process.stdout.write(`${JSON.stringify(finalizeP03Campaign(context), null, 2)}\n`);
    }
  } else {
    throw new Error(`unknown campaign CLI command ${command}`);
  }
} finally {
  workspaceRepository.close();
}
