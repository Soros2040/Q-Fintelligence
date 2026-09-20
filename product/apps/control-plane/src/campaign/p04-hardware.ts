import path from "node:path";

import type { JsonObject, QuantumJobSummary } from "@q-fintelligence/contracts";

import { assertHardwareMutationAuthorized } from "../hardware-policy.js";
import { p04AuthorizationHash } from "./p04-authorization.js";
import { runTianyanJob } from "./python-runner.js";
import { findSixQubitCycle, remapSixQubits } from "./tianyan176-hardware.js";
import {
  executeP04JsonStage,
  p04Hash,
  p04Json,
  readP04Json,
  registerP04JsonArtifact,
  type P04WorkerContext,
} from "./p04-runtime.js";

export interface P04HardwareOutcome {
  schemaVersion: "qf.p04.hardware-outcome.v1";
  terminal: boolean;
  blockedExternally: boolean;
  queryId: string | null;
  quantumJobId: string;
  resultArtifactSha256: string | null;
  localPreflightArtifactSha256: string;
  compatibilityArtifactSha256: string;
  status: string;
}

export interface P04RecoveryQueryOutcome {
  schemaVersion: "qf.p04.recovery-query-outcome.v1";
  queryId: string;
  quantumJobId: string;
  terminal: boolean;
  blockedExternally: boolean;
  resubmitted: false;
  result: JsonObject;
}

function terminalResult(result: JsonObject): boolean {
  return Array.isArray(result.result) && result.result.length > 0;
}

export async function runP04HardwareRecoveryQuery(
  context: P04WorkerContext,
  hardware: P04HardwareOutcome,
): Promise<P04RecoveryQueryOutcome> {
  if (!hardware.queryId) throw new Error("P04 external-query recovery requires a persisted Query ID");
  context.p04Repository.appendEvent(context.runId, "EXTERNAL_QUERY_STARTED", p04Json({
    quantumJobId: hardware.quantumJobId,
    queryId: hardware.queryId,
    phase: "targeted_fault_recovery_probe",
    expectedEvidence: "recovery of the same persisted Query ID without a second hardware submission",
  }));
  const existingFault = context.p04Repository.listFaults(context.campaignId).find((fault) => (
    fault.runId === context.runId && fault.kind === "EXTERNAL_QUERY_PROCESS_TERMINATION"
  ));
  if (!existingFault) {
    // This bounded arming window belongs only to the fault harness. It is not
    // accepted as Campaign runtime or scientific work and disappears on recovery.
    await new Promise((resolve) => setTimeout(resolve, 10_000));
  }
  const query = await runTianyanJob({
    projectRoot: context.projectRoot,
    request: p04Json({
      action: "query",
      machine_name: "tianyan176",
      query_id: hardware.queryId,
      max_wait_seconds: 600,
      poll_interval_seconds: 10,
    }),
    timeoutSeconds: 660,
  });
  context.campaignRepository.addTiming(context.campaignId, "external", query.durationSeconds);
  return {
    schemaVersion: "qf.p04.recovery-query-outcome.v1",
    queryId: hardware.queryId,
    quantumJobId: hardware.quantumJobId,
    terminal: query.exitCode === 0 && terminalResult(query.stdout),
    blockedExternally: query.exitCode !== 0 || !terminalResult(query.stdout),
    resubmitted: false,
    result: query.stdout,
  };
}

function existingJob(context: P04WorkerContext, circuitHash: string): QuantumJobSummary | null {
  return context.campaignRepository.listQuantumJobs(context.campaignId).find((job) => (
    job.backend === "tianyan176"
    && job.targetType === "HARDWARE"
    && job.purpose === "representative_financial_qaoa"
    && job.circuitHash === circuitHash
  )) ?? null;
}

export async function runP04HardwareChain(
  context: P04WorkerContext,
  sciencePath: string,
  scienceArtifactSha256: string,
  recovery: boolean,
): Promise<P04HardwareOutcome> {
  const science = await readP04Json<JsonObject>(sciencePath);
  const local = science.cqlib_local as JsonObject;
  const qaoa = local.qaoa as JsonObject;
  const sourceQcis = String(qaoa.qcis ?? "");
  const sourceCircuitHash = p04Hash(sourceQcis);
  if (!sourceQcis || qaoa.qcis_sha256 !== sourceCircuitHash) throw new Error("science result QAOA QCIS hash is inconsistent");

  const localPath = path.join(context.runWorkspace, "hardware", "local-cqlib-preflight.json");
  const localPreflight = await executeP04JsonStage<JsonObject>({
    context,
    stage: "local_cqlib_preflight",
    actionType: "P04_CQLIB_LOCAL_PREFLIGHT",
    expectedEvidence: "same-QCIS local cqlib probabilities and hash before any hardware submission",
    inputIdentity: { circuitHash: sourceCircuitHash, shots: 4096, seed: 20260722 },
    outputPath: localPath,
    timing: "active",
    producer: "qf-p04.cqlib-local-preflight",
    logicalName: "local-cqlib-preflight",
    parentHashes: [scienceArtifactSha256],
    run: async () => {
      const result = await runTianyanJob({
        projectRoot: context.projectRoot,
        request: p04Json({ action: "local_simulate", qcis: sourceQcis, shots: 4096, seed: 20260722 }),
        timeoutSeconds: 180,
      });
      if (result.exitCode !== 0 || result.stdout.qcis_sha256 !== sourceCircuitHash) {
        throw new Error(`local cqlib preflight failed: ${String(result.stdout.message ?? "hash mismatch")}`);
      }
      return result.stdout;
    },
  });

  const discoveryPath = path.join(context.runWorkspace, "hardware", "tianyan176-discovery.json");
  const discovery = await executeP04JsonStage<JsonObject>({
    context,
    stage: "discover_tianyan176",
    actionType: "P04_TIANYAN176_READONLY_DISCOVERY",
    expectedEvidence: "current tianyan176 running/free/hardware classification without querying remote simulators",
    inputIdentity: { backend: "tianyan176", capturedFor: sourceCircuitHash },
    outputPath: discoveryPath,
    timing: "external",
    producer: "qf-p04.tianyan176-discovery",
    logicalName: "tianyan176-discovery",
    parentHashes: [localPreflight.artifact.sha256],
    run: async () => {
      const result = await runTianyanJob({ projectRoot: context.projectRoot, request: { action: "discover" }, timeoutSeconds: 180 });
      if (result.exitCode !== 0 || result.stdout.status === "FAILED") {
        throw new Error(String(result.stdout.message ?? "TianYan discovery failed"));
      }
      const backends = Array.isArray(result.stdout.backends) ? result.stdout.backends as Array<Record<string, unknown>> : [];
      const backend = backends.find((item) => item.machine_name === "tianyan176");
      if (!backend || backend.status !== "running" || backend.toll !== "free" || backend.target_type !== "HARDWARE") {
        throw new Error("tianyan176 is not currently running/free/hardware");
      }
      return p04Json({
        schemaVersion: "qf.p04.tianyan176-discovery.v1",
        backend,
        remoteSimulatorInspected: false,
      });
    },
  });

  const configPath = path.join(context.runWorkspace, "hardware", "tianyan176-config-summary.json");
  const config = await executeP04JsonStage<JsonObject>({
    context,
    stage: "read_tianyan176_topology",
    actionType: "P04_TIANYAN176_READONLY_CONFIG",
    expectedEvidence: "current non-secret tianyan176 coupler map used for deterministic six-qubit placement",
    inputIdentity: { backend: "tianyan176", sourceCircuitHash },
    outputPath: configPath,
    timing: "external",
    producer: "qf-p04.tianyan176-config-summary",
    logicalName: "tianyan176-config-summary",
    parentHashes: [localPreflight.artifact.sha256, discovery.artifact.sha256],
    run: async () => {
      const result = await runTianyanJob({
        projectRoot: context.projectRoot,
        request: { action: "config_summary", machine_name: "tianyan176" },
        timeoutSeconds: 180,
      });
      if (result.exitCode !== 0 || result.stdout.status === "FAILED") {
        throw new Error(String(result.stdout.message ?? "tianyan176 configuration query failed"));
      }
      return result.stdout;
    },
  });
  const overview = config.value.overview as JsonObject;
  const physicalQubits = findSixQubitCycle(overview.coupler_map as Record<string, unknown>);
  const qcis = remapSixQubits(sourceQcis, physicalQubits);
  const circuitHash = p04Hash(qcis);

  const mappedLocalPath = path.join(context.runWorkspace, "hardware", "mapped-local-cqlib-preflight.json");
  const mappedLocalPreflight = await executeP04JsonStage<JsonObject>({
    context,
    stage: "local_cqlib_mapped_preflight",
    actionType: "P04_CQLIB_MAPPED_LOCAL_PREFLIGHT",
    expectedEvidence: "local cqlib probabilities for the exact topology-mapped QCIS submitted to tianyan176",
    inputIdentity: { sourceCircuitHash, circuitHash, physicalQubits, shots: 4096, seed: 20260722 },
    outputPath: mappedLocalPath,
    timing: "active",
    producer: "qf-p04.cqlib-mapped-local-preflight",
    logicalName: "mapped-local-cqlib-preflight",
    parentHashes: [localPreflight.artifact.sha256, config.artifact.sha256],
    run: async () => {
      const result = await runTianyanJob({
        projectRoot: context.projectRoot,
        request: p04Json({ action: "local_simulate", qcis, shots: 4096, seed: 20260722 }),
        timeoutSeconds: 180,
      });
      if (result.exitCode !== 0 || result.stdout.qcis_sha256 !== circuitHash) {
        throw new Error(`mapped local cqlib preflight failed: ${String(result.stdout.message ?? "hash mismatch")}`);
      }
      return p04Json({ ...result.stdout, source_qcis_sha256: sourceCircuitHash, physical_qubits: physicalQubits });
    },
  });

  const compatibilityPath = path.join(context.runWorkspace, "hardware", "tianyan176-compatibility.json");
  const compatibility = await executeP04JsonStage<JsonObject>({
    context,
    stage: "validate_tianyan176_qcis",
    actionType: "P04_TIANYAN176_CIRCUIT_VALIDATION",
    expectedEvidence: "tianyan176 accepts the exact locally preflighted QAOA QCIS hash",
    inputIdentity: { backend: "tianyan176", circuitHash },
    outputPath: compatibilityPath,
    timing: "external",
    producer: "qf-p04.tianyan176-compatibility",
    logicalName: "tianyan176-compatibility",
    parentHashes: [mappedLocalPreflight.artifact.sha256, discovery.artifact.sha256, config.artifact.sha256],
    run: async () => {
      const result = await runTianyanJob({
        projectRoot: context.projectRoot,
        request: p04Json({ action: "validate", machine_name: "tianyan176", qcis }),
        timeoutSeconds: 180,
      });
      if (result.exitCode !== 0 || result.stdout.valid !== true || result.stdout.qcis_sha256 !== circuitHash) {
        throw new Error(String(result.stdout.message ?? "tianyan176 rejected the locally preflighted circuit"));
      }
      return result.stdout;
    },
  });

  if (context.hardwarePolicy.mode === "READ_ONLY") {
    context.p04Repository.appendEvent(context.runId, "HARDWARE_NOT_AUTHORIZED", p04Json({
      hardwareMode: context.hardwarePolicy.mode,
      backend: context.hardwarePolicy.target,
      maxNewHardwareJobs: context.hardwarePolicy.maxNewHardwareJobs,
      shotsPerJob: context.hardwarePolicy.shotsPerJob,
      preparationCreated: false,
      approvalCreated: false,
      queryIdCreated: false,
      localPreflightArtifactSha256: mappedLocalPreflight.artifact.sha256,
      compatibilityArtifactSha256: compatibility.artifact.sha256,
      formalTestSealed: true,
    }));
    return {
      schemaVersion: "qf.p04.hardware-outcome.v1",
      terminal: false,
      blockedExternally: false,
      queryId: null,
      quantumJobId: "NOT_AUTHORIZED",
      resultArtifactSha256: null,
      localPreflightArtifactSha256: mappedLocalPreflight.artifact.sha256,
      compatibilityArtifactSha256: compatibility.artifact.sha256,
      status: "NOT_AUTHORIZED",
    };
  }

  assertHardwareMutationAuthorized(context.hardwarePolicy, {
    operation: "legacy P04 hardware chain",
    authorizationBasis: null,
    jobCount: 1,
    shotsPerJob: 100,
    target: "tianyan176",
  });

  context.p04Repository.acquireHardwareLease({
    campaignId: context.campaignId,
    runId: context.runId,
    workerId: context.workerId,
    recovery,
  });
  const requestSnapshot = {
    schemaVersion: "qf.p04.hardware-request.v1",
    backend: "tianyan176",
    purpose: "representative_financial_qaoa",
    circuitHash,
    shots: 100,
    estimatedExecutionSeconds: 240,
    localPreflightArtifactSha256: mappedLocalPreflight.artifact.sha256,
    compatibilityArtifactSha256: compatibility.artifact.sha256,
    authorizationHash: p04AuthorizationHash(),
  };
  const requestHash = p04Hash(requestSnapshot);
  const idempotencyKey = `${context.campaignId}:p04:tianyan176:${requestHash}`;
  context.campaignRepository.recordBudget({
    campaignId: context.campaignId,
    resourceKind: "TIANYAN176_HARDWARE",
    actionKey: idempotencyKey,
    calls: 1,
    shots: 100,
    executionSeconds: 240,
    expectedEvidence: "one query id and terminal raw tianyan176 result, or an honest recoverable external blocker",
  });
  const externalRequestId = context.campaignRepository.prepareExternalRequest({
    campaignId: context.campaignId,
    provider: "tianyan",
    requestKind: "P04_HARDWARE_SUBMIT_OR_QUERY",
    target: "tianyan176",
    idempotencyKey,
    approvalHash: p04AuthorizationHash(),
    requestHash,
  });
  let job = existingJob(context, circuitHash) ?? context.campaignRepository.createQuantumJob({
    campaignId: context.campaignId,
    externalRequestId,
    purpose: "representative_financial_qaoa",
    backend: "tianyan176",
    targetType: "HARDWARE",
    circuitHash,
    shots: 100,
    estimatedExecutionSeconds: 240,
  });
  if (job.terminalStatus === "COMPLETED" && job.queryId && job.rawResultArtifactSha256) {
    context.p04Repository.releaseHardwareLease(context.runId, context.workerId, "COMPLETED_REUSED");
    return {
      schemaVersion: "qf.p04.hardware-outcome.v1",
      terminal: true,
      blockedExternally: false,
      queryId: job.queryId,
      quantumJobId: job.quantumJobId,
      resultArtifactSha256: job.rawResultArtifactSha256,
      localPreflightArtifactSha256: mappedLocalPreflight.artifact.sha256,
      compatibilityArtifactSha256: compatibility.artifact.sha256,
      status: "COMPLETED_REUSED",
    };
  }

  let queryId = job.queryId;
  if (!queryId) {
    context.p04Repository.appendEvent(context.runId, "HARDWARE_SUBMISSION_STARTED", p04Json({
      quantumJobId: job.quantumJobId,
      idempotencyKey,
      expectedEvidence: "new tianyan176 Query ID for the locally preflighted financial QAOA circuit",
    }));
    context.p04Repository.updateHardwareLease(context.runId, context.workerId, "SUBMITTING");
    context.campaignRepository.markExternalRequest(externalRequestId, "COMMITTING");
    const submission = await runTianyanJob({
      projectRoot: context.projectRoot,
      request: p04Json({
        action: "submit",
        commit_authorized: true,
        approval_hash: p04AuthorizationHash(),
        authorization_phase: "P04",
        target_type: "HARDWARE",
        purpose: "representative_financial_qaoa",
        shots: 100,
        estimated_execution_seconds: 240,
        previous_hardware_execution_seconds: 0,
        qcis,
        machine_name: "tianyan176",
      }),
      timeoutSeconds: 240,
    });
    context.campaignRepository.addTiming(context.campaignId, "external", submission.durationSeconds);
    if (submission.exitCode !== 0 || submission.stdout.status === "FAILED") {
      context.campaignRepository.markExternalRequest(externalRequestId, "UNKNOWN");
      context.p04Repository.updateHardwareLease(context.runId, context.workerId, "UNKNOWN_NO_QUERY_ID");
      throw new Error(`tianyan176 submission entered unknown state without resubmission: ${String(submission.stdout.message ?? "adapter failure")}`);
    }
    queryId = String(submission.stdout.query_id ?? "");
    if (!queryId) {
      context.campaignRepository.markExternalRequest(externalRequestId, "UNKNOWN");
      context.p04Repository.updateHardwareLease(context.runId, context.workerId, "UNKNOWN_NO_QUERY_ID");
      throw new Error("tianyan176 submission returned no Query ID; resubmission is prohibited");
    }
    job = context.campaignRepository.markQuantumJobSubmitted(job.quantumJobId, queryId);
    const submissionArtifact = await registerP04JsonArtifact({
      context,
      value: submission.stdout,
      producer: "qf-p04.tianyan176-submission",
      logicalName: "tianyan176-submission",
      relativeOutputPath: path.relative(context.projectRoot, path.join(context.runWorkspace, "hardware", "tianyan176-submission.json")),
      parentHashes: [compatibility.artifact.sha256],
    });
    context.campaignRepository.markExternalRequest(externalRequestId, "SUBMITTED", queryId, submissionArtifact.sha256);
    context.p04Repository.updateHardwareLease(context.runId, context.workerId, "QUEUED", queryId);
    context.p04Repository.appendEvent(context.runId, "HARDWARE_QUERY_ID_PERSISTED", p04Json({
      quantumJobId: job.quantumJobId,
      queryId,
      artifactSha256: submissionArtifact.sha256,
    }));
  } else {
    context.p04Repository.updateHardwareLease(context.runId, context.workerId, "QUERY_RECOVERY", queryId);
    context.p04Repository.appendEvent(context.runId, "HARDWARE_QUERY_RECOVERED", p04Json({
      quantumJobId: job.quantumJobId,
      queryId,
      resubmitted: false,
    }));
  }

  context.p04Repository.appendEvent(context.runId, "EXTERNAL_QUERY_STARTED", p04Json({
    quantumJobId: job.quantumJobId,
    queryId,
    expectedEvidence: "terminal tianyan176 raw result for the persisted Query ID",
  }));
  const query = await runTianyanJob({
    projectRoot: context.projectRoot,
    request: p04Json({
      action: "query",
      machine_name: "tianyan176",
      query_id: queryId,
      max_wait_seconds: 600,
      poll_interval_seconds: 10,
    }),
    timeoutSeconds: 660,
  });
  context.campaignRepository.addTiming(context.campaignId, "external", query.durationSeconds);
  if (query.exitCode !== 0 || !terminalResult(query.stdout)) {
    const blocker = await registerP04JsonArtifact({
      context,
      value: {
        schemaVersion: "qf.p04.tianyan176-external-blocker.v1",
        queryId,
        backend: "tianyan176",
        resubmissionProhibited: true,
        adapterStatus: query.stdout.status ?? "NON_TERMINAL",
        message: query.stdout.message ?? "terminal result not returned within bounded query",
      },
      producer: "qf-p04.tianyan176-external-blocker",
      logicalName: `tianyan176-external-blocker-${Date.now()}`,
      relativeOutputPath: path.relative(context.projectRoot, path.join(context.runWorkspace, "hardware", `external-blocker-${Date.now()}.json`)),
      parentHashes: [compatibility.artifact.sha256],
    });
    context.campaignRepository.markExternalRequest(externalRequestId, "SUBMITTED", queryId, blocker.sha256);
    context.p04Repository.updateHardwareLease(context.runId, context.workerId, "EXTERNAL_BLOCKED", queryId, 21_600);
    context.p04Repository.appendEvent(context.runId, "EXTERNAL_QUERY_BLOCKED", p04Json({ queryId, artifactSha256: blocker.sha256 }));
    return {
      schemaVersion: "qf.p04.hardware-outcome.v1",
      terminal: false,
      blockedExternally: true,
      queryId,
      quantumJobId: job.quantumJobId,
      resultArtifactSha256: blocker.sha256,
      localPreflightArtifactSha256: mappedLocalPreflight.artifact.sha256,
      compatibilityArtifactSha256: compatibility.artifact.sha256,
      status: "EXTERNAL_BLOCKED",
    };
  }
  const resultArtifact = await registerP04JsonArtifact({
    context,
    value: query.stdout,
    producer: "qf-p04.tianyan176-result",
    logicalName: "tianyan176-terminal-result",
    relativeOutputPath: path.relative(context.projectRoot, path.join(context.runWorkspace, "hardware", "tianyan176-terminal-result.json")),
    parentHashes: [compatibility.artifact.sha256],
  });
  context.campaignRepository.completeQuantumJob(job.quantumJobId, queryId, "COMPLETED", resultArtifact.sha256);
  context.campaignRepository.markExternalRequest(externalRequestId, "COMPLETED", queryId, resultArtifact.sha256);
  context.p04Repository.releaseHardwareLease(context.runId, context.workerId, "COMPLETED");
  context.p04Repository.appendEvent(context.runId, "HARDWARE_TERMINAL_RESULT", p04Json({
    quantumJobId: job.quantumJobId,
    queryId,
    artifactSha256: resultArtifact.sha256,
  }));
  return {
    schemaVersion: "qf.p04.hardware-outcome.v1",
    terminal: true,
    blockedExternally: false,
    queryId,
    quantumJobId: job.quantumJobId,
    resultArtifactSha256: resultArtifact.sha256,
    localPreflightArtifactSha256: mappedLocalPreflight.artifact.sha256,
    compatibilityArtifactSha256: compatibility.artifact.sha256,
    status: "COMPLETED",
  };
}
