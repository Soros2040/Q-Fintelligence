import { createHash, randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import { mkdir, readFile } from "node:fs/promises";
import path from "node:path";

import type { JsonObject, P04FaultInjectionSummary, P04RunLane } from "@q-fintelligence/contracts";

import { readArtifact } from "../artifact-store.js";
import { loadRuntimeConfig } from "../config.js";
import { openDatabase } from "../db/migrations.js";
import { WorkspaceRepository } from "../db/repository.js";
import {
  runP04HardwareChain,
  runP04HardwareRecoveryQuery,
  type P04HardwareOutcome,
  type P04RecoveryQueryOutcome,
} from "./p04-hardware.js";
import { P04Repository } from "./p04-repository.js";
import {
  executeP04JsonStage,
  p04Hash,
  p04Json,
  readP04Json,
  registerP04JsonArtifact,
  type P04WorkerContext,
} from "./p04-runtime.js";
import {
  generateValidateRegisterAndInvokeTools,
  runToolActiveValidationBlock,
} from "./p04-tool-factory.js";
import { runGeneratedToolJob, runScienceJob } from "./python-runner.js";
import { CampaignRepository } from "./repository.js";
import { TushareAdapter, type TushareDatasetBundle } from "./tushare-adapter.js";

const PROJECT_ROOT = process.cwd();

function wait(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function remainingSeconds(deadline: number): number {
  return Math.max(0, (deadline - Date.now()) / 1000);
}

async function recoverFault(
  context: P04WorkerContext,
  fault: P04FaultInjectionSummary,
  evidence: JsonObject,
): Promise<void> {
  const artifact = await registerP04JsonArtifact({
    context,
    value: {
      schemaVersion: "qf.p04.fault-recovery-proof.v1",
      faultId: fault.faultId,
      kind: fault.kind,
      sameCampaignId: context.campaignId,
      sameRunId: context.runId,
      evidence,
    },
    producer: `qf-p04.fault-recovery.${fault.kind.toLowerCase()}`,
    logicalName: `fault-recovery-${fault.faultId}`,
    relativeOutputPath: path.relative(context.projectRoot, path.join(context.runWorkspace, "recovery", `${fault.faultId}.json`)),
  });
  context.p04Repository.recoverFault(fault.faultId, artifact.sha256);
  context.p04Repository.appendEvent(context.runId, "FAULT_RECOVERED", p04Json({
    faultId: fault.faultId,
    kind: fault.kind,
    artifactSha256: artifact.sha256,
  }));
}

async function runFinanceLane(context: P04WorkerContext, deadline: number, recovery: boolean): Promise<void> {
  const bundlePath = path.join(context.runWorkspace, "input", "tushare-bundle.json");
  const bundleStage = await executeP04JsonStage<TushareDatasetBundle>({
    context,
    stage: "fetch_tushare_six_stock_bundle",
    actionType: "P04_TUSHARE_READONLY_FETCH",
    expectedEvidence: "real Tushare provenance, point-in-time six-stock selection, cache/request hashes, and sealed 2019-2023 rows",
    inputIdentity: {
      snapshot: "2020-01",
      classificationEffective: "2019-12-31",
      range: ["2019-01-01", "2023-12-31"],
      formalTestSealed: true,
    },
    outputPath: bundlePath,
    timing: "external",
    producer: "qf-p04.tushare-six-stock-bundle",
    logicalName: "tushare-six-stock-bundle",
    run: async () => {
      const token = process.env.TUSHARE_TOKEN;
      if (!token) throw new Error("TUSHARE_TOKEN is not configured");
      return new TushareAdapter(token, path.join(context.projectRoot, ".local", "market-cache", "tushare"))
        .fetchHistoricalBundle();
    },
  });
  const selected = bundleStage.value.selected;
  if (selected.length !== 6 || new Set(selected.map((item) => item.tsCode)).size !== 6) {
    throw new Error("P04 Tushare bundle did not contain six unique stocks");
  }
  const sciencePath = path.join(context.runWorkspace, "results", "science-result.json");
  const scienceStage = await executeP04JsonStage<JsonObject>({
    context,
    stage: "run_validation_only_science_chain",
    actionType: "P04_REGISTERED_SCIENCE_PIPELINE",
    expectedEvidence: "real-data labels, risk graph, validation-only models, QUBO, 20 exact portfolios, QAOA, and local cqlib results",
    inputIdentity: { bundleSha256: bundleStage.artifact.sha256, seed: 20260722, formalTestSealed: true },
    outputPath: sciencePath,
    timing: "active",
    producer: "qf-p04.science-chain",
    logicalName: "science-result",
    parentHashes: [bundleStage.artifact.sha256],
    run: async () => {
      const result = await runScienceJob({
        projectRoot: context.projectRoot,
        request: p04Json({
          action: "run_pipeline",
          workspace_root: context.runWorkspace,
          input_path: bundlePath,
          output_path: sciencePath,
          seed: 20260722,
        }),
        timeoutSeconds: 3_600,
      });
      if (result.exitCode !== 0 || result.stdout.status !== "COMPLETED") {
        throw new Error(`P04 science runner failed: ${String(result.stdout.message ?? "unknown")}`);
      }
      return readP04Json<JsonObject>(sciencePath);
    },
  });
  const science = scienceStage.value;
  const exact = (science.qubo as JsonObject).exact_portfolios;
  const formalTest = science.formal_test as JsonObject;
  if (!Array.isArray(exact) || exact.length !== 20
    || formalTest.sealed !== true || formalTest.test_metrics_emitted !== false) {
    throw new Error("P04 science result violated exact-enumeration or formal-test seal");
  }

  if (recovery) {
    const scienceFault = context.p04Repository.listFaults(context.campaignId).find((fault) => (
      fault.runId === context.runId
      && fault.kind === "SCIENCE_PROCESS_TERMINATION"
      && fault.state === "INJECTED"
    ));
    if (scienceFault) {
      await recoverFault(context, scienceFault, p04Json({
        bundleActionReused: bundleStage.reused,
        scienceActionReused: scienceStage.reused,
        bundleArtifactSha256: bundleStage.artifact.sha256,
        scienceArtifactSha256: scienceStage.artifact.sha256,
        duplicateDownload: false,
        duplicateTraining: false,
      }));
    }
  }

  let block = 0;
  const runReproducibilityBlock = async (minimumComputeSeconds: number) => {
    const outputPath = path.join(context.runWorkspace, "results", `reproducibility-${String(block).padStart(4, "0")}.json`);
    await executeP04JsonStage<JsonObject>({
      context,
      stage: `reproducibility_block_${block}`,
      actionType: "P04_ACTIVE_REPRODUCIBILITY_BLOCK",
      expectedEvidence: "new seed-indexed bootstrap stability digest using the real-data science result",
      inputIdentity: {
        scienceArtifactSha256: scienceStage.artifact.sha256,
        seedStart: 20260722 + block * 1_000_000,
        minimumComputeSeconds,
      },
      outputPath,
      timing: "active",
      producer: "qf-p04.active-reproducibility",
      logicalName: `finance-reproducibility-${block}`,
      parentHashes: [scienceStage.artifact.sha256],
      run: async () => {
        const result = await runScienceJob({
          projectRoot: context.projectRoot,
          request: p04Json({
            action: "active_reproducibility_block",
            workspace_root: context.runWorkspace,
            input_path: sciencePath,
            output_path: outputPath,
            seed_start: 20260722 + block * 1_000_000,
            minimum_compute_seconds: minimumComputeSeconds,
          }),
          timeoutSeconds: minimumComputeSeconds + 120,
        });
        if (result.exitCode !== 0 || result.stdout.status !== "COMPLETED") {
          throw new Error(`P04 reproducibility block failed: ${String(result.stdout.message ?? "unknown")}`);
        }
        return readP04Json<JsonObject>(outputPath);
      },
    });
    block += 1;
  };

  await runReproducibilityBlock(Math.max(1, Math.min(280, Math.floor(remainingSeconds(deadline)))));
  const hardwareOutcomePath = path.join(context.runWorkspace, "hardware", "hardware-outcome.json");
  const hardware = await executeP04JsonStage<P04HardwareOutcome>({
    context,
    stage: "tianyan176_financial_qaoa_chain",
    actionType: "P04_TIANYAN176_HARDWARE_CHAIN",
    expectedEvidence: "same-circuit local cqlib preflight and the frozen hardware-policy result; READ_ONLY creates no Job or Query ID",
    inputIdentity: {
      scienceArtifactSha256: scienceStage.artifact.sha256,
      backend: "tianyan176",
      shots: 100,
      noRemoteSimulator: true,
      hardwarePolicy: context.hardwarePolicy,
    },
    outputPath: hardwareOutcomePath,
    timing: "external",
    producer: "qf-p04.tianyan176-hardware-outcome",
    logicalName: "tianyan176-hardware-outcome",
    parentHashes: [scienceStage.artifact.sha256],
    run: () => runP04HardwareChain(context, sciencePath, scienceStage.artifact.sha256, recovery),
  });
  const queryRecovery = hardware.value.queryId
    ? await executeP04JsonStage<P04RecoveryQueryOutcome>({
      context,
      stage: "targeted_external_query_recovery_v2",
      actionType: "P04_TARGETED_EXTERNAL_QUERY_RECOVERY",
      expectedEvidence: "same-Query-ID external queue recovery after targeted process termination, without resubmission",
      inputIdentity: {
        queryId: hardware.value.queryId,
        quantumJobId: hardware.value.quantumJobId,
        faultKind: "EXTERNAL_QUERY_PROCESS_TERMINATION",
        faultHarnessVersion: 2,
      },
      outputPath: path.join(context.runWorkspace, "hardware", "targeted-query-recovery.json"),
      timing: "external",
      producer: "qf-p04.targeted-query-recovery",
      logicalName: "targeted-query-recovery",
      parentHashes: [hardware.artifact.sha256],
      run: () => runP04HardwareRecoveryQuery(context, hardware.value),
    })
    : null;
  if (recovery) {
    const queryFault = context.p04Repository.listFaults(context.campaignId).find((fault) => (
      fault.runId === context.runId
      && fault.kind === "EXTERNAL_QUERY_PROCESS_TERMINATION"
      && fault.state === "INJECTED"
    ));
    if (queryFault) {
      await recoverFault(context, queryFault, p04Json({
        queryId: queryRecovery?.value.queryId ?? hardware.value.queryId,
        quantumJobId: queryRecovery?.value.quantumJobId ?? hardware.value.quantumJobId,
        sameQueryId: true,
        resubmitted: queryRecovery?.value.resubmitted ?? false,
        terminal: queryRecovery?.value.terminal ?? hardware.value.terminal,
        blockedExternally: queryRecovery?.value.blockedExternally ?? hardware.value.blockedExternally,
        resultArtifactSha256: queryRecovery?.artifact.sha256 ?? hardware.value.resultArtifactSha256,
      }));
    }
  }
  while (remainingSeconds(deadline) >= 1) {
    await runReproducibilityBlock(Math.max(1, Math.min(280, Math.floor(remainingSeconds(deadline)))));
    context.campaignRepository.checkpoint(context.campaignId, `p04_finance_${block}`, p04Json({
      runId: context.runId,
      block,
      wallClockSeconds: context.campaignRepository.getCampaign(context.campaignId).wallClockSeconds,
      scienceArtifactSha256: scienceStage.artifact.sha256,
      hardwareQueryId: hardware.value.queryId,
      formalTestSealed: true,
    }));
  }
}

async function invokeHardwareAnalysisIfAvailable(
  context: P04WorkerContext,
  descriptor: Awaited<ReturnType<typeof generateValidateRegisterAndInvokeTools>>[number],
): Promise<boolean> {
  const job = context.campaignRepository.listQuantumJobs(context.campaignId).find((candidate) => (
    candidate.backend === "tianyan176"
    && candidate.purpose === "representative_financial_qaoa"
    && candidate.queryId
    && candidate.rawResultArtifactSha256
  ));
  if (!job?.rawResultArtifactSha256 || descriptor.tool.name !== "quantum_result_diagnostics") return false;
  const manifest = context.workspaceRepository.getArtifactManifest(job.rawResultArtifactSha256);
  const bytes = await readArtifact({ root: context.artifactRoot, manifest, maxBytes: 1_000_000, allowedMediaTypes: ["application/json"] });
  const hardwareResult = JSON.parse(new TextDecoder().decode(bytes)) as JsonObject;
  const payload = p04Json({ ...descriptor.basePayload, hardware_result: hardwareResult });
  const inputHash = p04Hash(payload);
  const idempotencyKey = `${context.campaignId}:${context.runId}:${descriptor.tool.name}:hardware:${inputHash}`;
  const invocationId = context.p04Repository.beginToolInvocation(descriptor.tool.toolId, context.runId, idempotencyKey, inputHash);
  const result = await runGeneratedToolJob({
    projectRoot: context.projectRoot,
    request: p04Json({
      action: "invoke",
      workspace_root: context.runWorkspace,
      tool_name: descriptor.tool.name,
      tool_path: descriptor.path,
      payload,
    }),
    timeoutSeconds: 60,
  });
  if (result.exitCode !== 0 || result.stdout.status !== "COMPLETED") {
    context.p04Repository.completeToolInvocation({
      invocationId,
      status: "FAILED",
      exitCode: result.exitCode,
      durationSeconds: result.durationSeconds,
    });
    throw new Error("quantum generated tool failed the hardware-result invocation");
  }
  const artifact = await registerP04JsonArtifact({
    context,
    value: result.stdout,
    producer: "qf-p04.generated-tool-invocation.quantum-hardware",
    logicalName: "generated-tool-invocation-quantum-hardware",
    relativeOutputPath: path.relative(context.projectRoot, path.join(context.runWorkspace, "tool-factory", "quantum-hardware-result.json")),
    parentHashes: [descriptor.tool.sourceArtifactSha256!, job.rawResultArtifactSha256],
  });
  context.p04Repository.completeToolInvocation({
    invocationId,
    status: "COMPLETED",
    outputArtifactSha256: artifact.sha256,
    exitCode: result.exitCode,
    durationSeconds: result.durationSeconds,
  });
  context.p04Repository.appendEvent(context.runId, "GENERATED_TOOL_CALLED_ON_HARDWARE_RESULT", p04Json({
    toolId: descriptor.tool.toolId,
    queryId: job.queryId,
    hardwareResultArtifactSha256: job.rawResultArtifactSha256,
    diagnosticArtifactSha256: artifact.sha256,
  }));
  return true;
}

async function runToolFactoryLane(context: P04WorkerContext, deadline: number): Promise<void> {
  const financeRun = context.p04Repository.listRuns(context.campaignId).find((run) => run.lane === "finance");
  if (!financeRun) throw new Error("P04 finance Run was not initialized");
  const sciencePath = path.join(context.projectRoot, financeRun.relativeWorkspace, "results", "science-result.json");
  while (!existsSync(sciencePath)) {
    if (remainingSeconds(deadline) < 1) throw new Error("P04 tool factory timed out waiting for the science result");
    context.p04Repository.appendEvent(context.runId, "WAITING_FOR_EXPLICIT_SCIENCE_INPUT", p04Json({
      financeRunId: financeRun.runId,
      target: path.relative(context.projectRoot, sciencePath),
    }));
    await wait(2_000);
  }
  const descriptors = await generateValidateRegisterAndInvokeTools(context, sciencePath);
  let hardwareAnalyzed = false;
  let block = 0;
  while (remainingSeconds(deadline) >= 1) {
    const descriptor = descriptors[block % descriptors.length]!;
    await runToolActiveValidationBlock({
      context,
      tool: descriptor.tool,
      toolPath: descriptor.path,
      basePayload: descriptor.basePayload,
      block,
      minimumComputeSeconds: Math.max(1, Math.min(120, Math.floor(remainingSeconds(deadline)))),
    });
    if (!hardwareAnalyzed) hardwareAnalyzed = await invokeHardwareAnalysisIfAvailable(context, descriptors.find((item) => item.tool.name === "quantum_result_diagnostics")!);
    block += 1;
    context.campaignRepository.checkpoint(context.campaignId, `p04_tool_factory_${block}`, p04Json({
      runId: context.runId,
      block,
      registeredTools: descriptors.map((item) => item.tool.name),
      hardwareAnalyzed,
    }));
  }
}

async function runEvidenceAuditLane(context: P04WorkerContext, deadline: number): Promise<void> {
  const verified = new Set<string>();
  let block = 0;
  while (remainingSeconds(deadline) >= 1) {
    const startedAt = performance.now();
    const artifactRows = context.p04Repository.artifactRows(context.campaignId);
    const newlyVerified: Array<{ sha256: string; runId: string; bytes: number }> = [];
    for (const artifact of artifactRows) {
      if (verified.has(artifact.sha256)) continue;
      const pathname = path.join(context.artifactRoot, ...artifact.relativePath.split("/"));
      const data = await readFile(pathname);
      const actual = createHash("sha256").update(data).digest("hex");
      if (actual !== artifact.sha256 || data.byteLength !== artifact.bytes) {
        throw new Error(`P04 artifact verification failed for ${artifact.sha256}`);
      }
      verified.add(artifact.sha256);
      newlyVerified.push({ sha256: artifact.sha256, runId: artifact.runId, bytes: artifact.bytes });
    }
    const snapshot = context.p04Repository.concurrencySnapshot(context.campaignId);
    const sequences = snapshot.eventSequences as Array<Record<string, unknown>>;
    const sequenceValid = sequences.every((row) => (
      row.eventCount === row.distinctSequences
      && (row.eventCount === 0 || (row.minimumSequence === 1 && row.maximumSequence === row.eventCount))
    ));
    const valid = snapshot.journalMode === "wal"
      && snapshot.integrityCheck === "ok"
      && snapshot.foreignKeysEnabled === true
      && snapshot.duplicateOutputPaths === 0
      && Number(snapshot.activeHardwareLeases) <= 1
      && sequenceValid;
    if (!valid) throw new Error("P04 concurrent SQLite/artifact/event audit failed");
    const report = {
      schemaVersion: "qf.p04.concurrent-audit-block.v1",
      block,
      capturedAt: new Date().toISOString(),
      valid,
      sequenceValid,
      newlyVerified,
      totalVerifiedArtifacts: verified.size,
      snapshot,
      activeAuditSeconds: (performance.now() - startedAt) / 1000,
    };
    await registerP04JsonArtifact({
      context,
      value: report,
      producer: "qf-p04.concurrent-evidence-audit",
      logicalName: `concurrent-audit-${block}`,
      relativeOutputPath: path.relative(context.projectRoot, path.join(context.runWorkspace, "audit", `audit-${String(block).padStart(5, "0")}.json`)),
    });
    context.campaignRepository.addTiming(context.campaignId, "active", (performance.now() - startedAt) / 1000);
    context.p04Repository.appendEvent(context.runId, "CONCURRENT_AUDIT_COMPLETED", p04Json({
      block,
      newlyVerifiedArtifacts: newlyVerified.length,
      totalVerifiedArtifacts: verified.size,
      sequenceValid,
      activeHardwareLeases: snapshot.activeHardwareLeases,
    }));
    block += 1;
    await wait(Math.min(15_000, Math.max(100, remainingSeconds(deadline) * 1000)));
  }
}

async function main(): Promise<void> {
  if (process.env.QF_PROCESS_NAMESPACE !== "qfintelligence") throw new Error("P04 worker namespace is invalid");
  const projectRoot = process.cwd();
  if (projectRoot !== PROJECT_ROOT) throw new Error("P04 worker must run in the unique WSL project root");
  const campaignId = process.argv[2] ?? process.env.QF_CAMPAIGN_ID;
  const runId = process.argv[3] ?? process.env.QF_P04_RUN_ID;
  const lane = (process.argv[4] ?? process.env.QF_P04_LANE) as P04RunLane | undefined;
  if (!campaignId || !runId || !lane) throw new Error("P04 campaign, Run, and lane are required");
  const recovery = process.env.QF_P04_RECOVERY === "1";
  const config = loadRuntimeConfig();
  const migration = openDatabase(path.resolve(projectRoot, config.sqlitePath), path.join(projectRoot, "infra", "sqlite"));
  const workspaceRepository = new WorkspaceRepository(migration.database);
  const campaignRepository = new CampaignRepository(migration.database);
  const p04Repository = new P04Repository(migration.database);
  const campaign = campaignRepository.getCampaign(campaignId);
  workspaceRepository.assertConversationWritable(campaign.conversationId);
  const run = p04Repository.getRun(runId);
  if (run.campaignId !== campaignId || run.lane !== lane) throw new Error("P04 Run identity does not match the worker arguments");
  if (!campaign.startedAt) throw new Error("P04 Campaign has not been started");
  const deadline = Date.parse(campaign.startedAt) + campaign.minimumRuntimeSeconds * 1000;
  const workerId = `p04_${lane}_${randomUUID()}`;
  const runWorkspace = path.join(projectRoot, run.relativeWorkspace);
  await Promise.all(["input", "results", "hardware", "tool-factory", "active-validation", "audit", "recovery", "logs"]
    .map((directory) => mkdir(path.join(runWorkspace, directory), { recursive: true })));
  const context: P04WorkerContext = {
    projectRoot,
    campaignId,
    conversationId: campaign.conversationId,
    runId,
    runWorkspace,
    campaignWorkspace: path.join(projectRoot, ".local", "campaigns", campaignId, "workspace"),
    artifactRoot: path.resolve(projectRoot, config.artifactRoot),
    workerId,
    workspaceRepository,
    campaignRepository,
    p04Repository,
    hardwarePolicy: config.hardwarePolicy,
  };
  p04Repository.acquireRunLease(runId, workerId, process.pid, recovery);
  const heartbeat = setInterval(() => {
    try {
      p04Repository.heartbeatRun(runId, workerId);
    } catch (error) {
      process.stderr.write(`P04 heartbeat failed: ${error instanceof Error ? error.message : String(error)}\n`);
    }
  }, 30_000);
  heartbeat.unref();
  p04Repository.appendEvent(runId, recovery ? "RUN_WORKER_RECOVERED" : "RUN_WORKER_STARTED", p04Json({
    lane,
    workerId,
    processId: process.pid,
    recovery,
    deadline: new Date(deadline).toISOString(),
  }));
  try {
    if (lane === "finance") await runFinanceLane(context, deadline, recovery);
    else if (lane === "tool_factory") await runToolFactoryLane(context, deadline);
    else await runEvidenceAuditLane(context, deadline);
    p04Repository.appendEvent(runId, "RUN_COMPLETED", p04Json({
      lane,
      wallClockSeconds: campaignRepository.getCampaign(campaignId).wallClockSeconds,
      formalTestSealed: true,
    }));
    p04Repository.setRunState(runId, "COMPLETED");
  } catch (error) {
    const failure = await registerP04JsonArtifact({
      context,
      value: {
        schemaVersion: "qf.p04.run-failure.v1",
        lane,
        errorType: error instanceof Error ? error.name : "Error",
        message: (error instanceof Error ? error.message : String(error)).slice(0, 1000),
      },
      producer: `qf-p04.run-failure.${lane}`,
      logicalName: `run-failure-${Date.now()}`,
      relativeOutputPath: path.relative(projectRoot, path.join(runWorkspace, "logs", `failure-${Date.now()}.json`)),
    });
    campaignRepository.recordFailure({
      campaignId,
      stage: `p04_${lane}`,
      category: lane === "finance" ? "SCIENTIFIC_OR_BACKEND" : lane === "tool_factory" ? "TOOL_FACTORY" : "CONCURRENCY_AUDIT",
      summary: error instanceof Error ? error.message : String(error),
      evidenceArtifactSha256: failure.sha256,
      attempts: recovery ? 2 : 1,
      recoveryCommand: `npm run campaign:p04-resume-lane -- ${campaignId} ${lane}`,
    });
    p04Repository.setRunState(runId, "FAILED");
    throw error;
  } finally {
    clearInterval(heartbeat);
    p04Repository.releaseRunLease(runId, workerId);
    workspaceRepository.close();
  }
}

await main();
