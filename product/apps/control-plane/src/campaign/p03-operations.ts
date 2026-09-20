import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import path from "node:path";

import type { JsonObject, P03QueueRunSummary } from "@q-fintelligence/contracts";

import { storeArtifact } from "../artifact-store.js";
import type { RuntimeConfig } from "../config.js";
import type { WorkspaceRepository } from "../db/repository.js";
import { assertP03Authorization, P03_AUTHORIZATION, p03AuthorizationHash } from "./p03-authorization.js";
import { runTianyanJob } from "./python-runner.js";
import { probeFixedCampaignStreaming } from "./provider-probe.js";
import type { CampaignRepository } from "./repository.js";
import { findSixQubitCycle, remapSixQubits, representativeQgnnSubcircuit } from "./tianyan176-hardware.js";

const MACHINE_NAME = "tianyan176";
const SHOTS = 100;
const RESERVED_SECONDS = 240;
const OLD_CLOUD_QUERY_IDS = ["2079463720507179009", "2079469100217958401"] as const;

interface P03Context {
  projectRoot: string;
  campaignId: string;
  config: RuntimeConfig;
  campaignRepository: CampaignRepository;
  workspaceRepository: WorkspaceRepository;
}

function asJson(value: unknown): JsonObject {
  return value as JsonObject;
}

function hash(value: unknown): string {
  return createHash("sha256").update(typeof value === "string" ? value : JSON.stringify(value)).digest("hex");
}

function terminalResult(value: JsonObject): boolean {
  return Array.isArray(value.result) && value.result.length > 0;
}

function boundedFailure(value: JsonObject): JsonObject {
  return asJson({
    schemaVersion: value.schema_version ?? "qf.tianyan-error.v1",
    status: value.status ?? "UNKNOWN",
    errorType: typeof value.error_type === "string" ? value.error_type.slice(0, 120) : null,
    message: typeof value.message === "string"
      ? value.message.replace(/Bearer\s+\S+/giu, "Bearer [REDACTED]").slice(0, 500)
      : "no terminal result returned within bounded query",
  });
}

async function registerJsonArtifact(
  context: P03Context,
  value: unknown,
  producer: string,
  parentHashes: string[] = [],
): Promise<string> {
  const manifest = await storeArtifact({
    root: path.resolve(context.projectRoot, context.config.artifactRoot),
    data: new TextEncoder().encode(`${JSON.stringify(value, null, 2)}\n`),
    mediaType: "application/json",
    producer,
    parentHashes,
  });
  return context.workspaceRepository.registerArtifact(
    context.campaignRepository.getCampaign(context.campaignId).conversationId,
    manifest,
  ).sha256;
}

export function computeP03BackoffSeconds(pollAttempts: number, jitterUnit: number): number {
  if (!Number.isInteger(pollAttempts) || pollAttempts < 0) throw new Error("poll attempts must be non-negative");
  if (!Number.isFinite(jitterUnit) || jitterUnit < 0 || jitterUnit > 1) throw new Error("jitter must be between zero and one");
  const base = Math.min(120, 15 * 2 ** pollAttempts);
  const availableJitter = Math.min(base * 0.1, 120 - base);
  return Math.min(120, Math.round(base + availableJitter * jitterUnit));
}

export async function runP03SseDiagnosis(context: P03Context): Promise<JsonObject> {
  const baseUrl = process.env.OPENAI_BASE_URL;
  const apiKey = process.env.OPENAI_API_KEY;
  if (!baseUrl || !apiKey) throw new Error("fixed GeToken provider credentials are not configured");
  const before = context.campaignRepository.getCampaign(context.campaignId).llmCalls;
  if (before >= 120) throw new Error("LLM circuit breaker reached");
  context.campaignRepository.incrementLlmCalls(context.campaignId);
  const result = await probeFixedCampaignStreaming({ baseUrl, apiKey, marker: "QF_P03_STREAM_READY" });
  const evidence = asJson({
    schemaVersion: "qf.p03-sse-diagnosis.v1",
    capturedAt: new Date().toISOString(),
    fixedProvider: "openai/getoken",
    fixedModel: "gpt-5.6-sol",
    allowFallback: false,
    documentedContract: {
      url: "https://www.getoken.tech/docs/sdk",
      endpoint: "/v1/chat/completions",
      stream: true,
      sdkBehavior: "async chunk iteration",
    },
    result,
  });
  const artifactSha256 = await registerJsonArtifact(context, evidence, "qf-p03.provider-sse-diagnosis");
  context.campaignRepository.recordBudget({
    campaignId: context.campaignId,
    resourceKind: "LLM_P03_SSE_DIAGNOSTIC",
    actionKey: `${context.campaignId}:P03:SSE:${artifactSha256}`,
    calls: 1,
    expectedEvidence: "fixed-model streaming response headers, event diagnostics and body hash",
  });
  context.campaignRepository.recordGate({
    campaignId: context.campaignId,
    gateName: "G10_P03_FIXED_MODEL_SSE",
    subjectHash: artifactSha256,
    decision: result.ok ? "ALLOW" : "BLOCK",
    reason: result.ok
      ? "The fixed gpt-5.6-sol response satisfied the documented event-stream contract."
      : `The fixed provider response was classified as ${result.diagnostics?.classification ?? "REQUEST_FAILURE"}; fallback and non-streaming substitution remain prohibited.`,
    evidenceArtifactSha256: artifactSha256,
  });
  if (!result.ok) {
    context.campaignRepository.recordFailure({
      campaignId: context.campaignId,
      stage: "p03_provider_sse",
      category: "PROVIDER_SSE",
      summary: `Fixed gpt-5.6-sol streaming protocol blocked: ${result.diagnostics?.classification ?? "REQUEST_FAILURE"}`,
      evidenceArtifactSha256: artifactSha256,
      attempts: 1,
      recoveryCommand: `npm run campaign:p03-sse -- ${context.campaignId}`,
    });
  }
  return asJson({ ...evidence, artifactSha256 });
}

function probabilitySummary(result: JsonObject): JsonObject {
  const entries = Array.isArray(result.result) ? result.result as Array<Record<string, unknown>> : [];
  return asJson({
    resultEntries: entries.length,
    entries: entries.map((entry) => {
      let probabilities: Record<string, unknown> = {};
      if (typeof entry.probability === "string") {
        try {
          probabilities = JSON.parse(entry.probability) as Record<string, unknown>;
        } catch {
          probabilities = {};
        }
      } else if (entry.probability && typeof entry.probability === "object") {
        probabilities = entry.probability as Record<string, unknown>;
      }
      const counts = entry.counts && typeof entry.counts === "object" ? entry.counts as Record<string, unknown> : {};
      return {
        probabilityCount: Object.keys(probabilities).length,
        probabilities,
        countEntries: Object.keys(counts).length,
        counts,
      };
    }),
  });
}

export async function runP03OldCloudQuery(context: P03Context): Promise<JsonObject> {
  const outcomes: JsonObject[] = [];
  for (const queryId of OLD_CLOUD_QUERY_IDS) {
    const job = context.campaignRepository.findQuantumJobByQueryId(context.campaignId, queryId);
    if (!job || job.backend !== "tianyan_sw" || job.targetType !== "SIMULATOR") {
      throw new Error(`existing cloud Query ID ${queryId} is not registered on tianyan_sw`);
    }
    if (job.terminalStatus === "COMPLETED" && job.rawResultArtifactSha256) {
      outcomes.push(asJson({
        queryId,
        backend: job.backend,
        shots: job.shots,
        status: "COMPLETED",
        artifactSha256: job.rawResultArtifactSha256,
        reused: true,
      }));
      continue;
    }
    const queriedAt = new Date().toISOString();
    const query = await runTianyanJob({
      projectRoot: context.projectRoot,
      request: asJson({
        action: "query",
        machine_name: "tianyan_sw",
        query_id: queryId,
        max_wait_seconds: 20,
        poll_interval_seconds: 5,
      }),
      timeoutSeconds: 30,
    });
    context.campaignRepository.addTiming(context.campaignId, "external", query.durationSeconds);
    if (query.exitCode === 0 && terminalResult(query.stdout)) {
      const artifactSha256 = await registerJsonArtifact(
        context,
        asJson({
          schemaVersion: "qf.p03-existing-cloud-terminal.v1",
          queriedAt,
          backend: "tianyan_sw",
          queryId,
          shots: job.shots,
          rawReceipt: query.stdout,
          summary: probabilitySummary(query.stdout),
        }),
        "qf-p03.tianyan-sw-terminal",
      );
      context.campaignRepository.completeQuantumJob(job.quantumJobId, queryId, "COMPLETED", artifactSha256);
      context.campaignRepository.markExternalRequestForQuantumJob(job.quantumJobId, "COMPLETED", artifactSha256);
      outcomes.push(asJson({
        queryId,
        backend: job.backend,
        shots: job.shots,
        status: "COMPLETED",
        artifactSha256,
        summary: probabilitySummary(query.stdout),
        reused: false,
      }));
    } else {
      outcomes.push(asJson({
        queryId,
        backend: job.backend,
        shots: job.shots,
        status: "NON_TERMINAL",
        queriedAt,
        boundedWaitSeconds: 20,
        evidence: boundedFailure(query.stdout),
      }));
      context.campaignRepository.markExternalRequestForQuantumJob(job.quantumJobId, "SUBMITTED");
    }
  }
  const evidence = asJson({
    schemaVersion: "qf.p03-existing-cloud-query.v1",
    capturedAt: new Date().toISOString(),
    queryOnly: true,
    resubmitAuthorized: false,
    outcomes,
    recoveryCommand: `npm run campaign:p03-old-cloud -- ${context.campaignId}`,
  });
  const artifactSha256 = await registerJsonArtifact(context, evidence, "qf-p03.tianyan-sw-bounded-query");
  if (outcomes.some((outcome) => outcome.status !== "COMPLETED")) {
    context.campaignRepository.recordFailure({
      campaignId: context.campaignId,
      stage: "p03_existing_cloud_query",
      category: "TIANYAN_CLOUD_NON_TERMINAL",
      summary: "Existing tianyan_sw jobs remained non-terminal after a bounded read-only query; resubmission is prohibited.",
      evidenceArtifactSha256: artifactSha256,
      attempts: 1,
      recoveryCommand: `npm run campaign:p03-old-cloud -- ${context.campaignId}`,
    });
  }
  return asJson({ ...evidence, artifactSha256 });
}

async function mappedValidationCircuit(context: P03Context): Promise<{
  qcis: string;
  circuitHash: string;
  discoveryArtifactSha256: string;
  backendAvailable: boolean;
  machineStatus: string | null;
}> {
  const sciencePath = path.join(
    context.projectRoot,
    ".local",
    "campaigns",
    context.campaignId,
    "workspace",
    "results",
    "science-result.json",
  );
  const science = JSON.parse(await readFile(sciencePath, "utf8")) as Record<string, unknown>;
  const cqlibLocal = science.cqlib_local as Record<string, unknown>;
  const qgnn = cqlibLocal.qgnn as Record<string, unknown>;
  const parentQcis = String(qgnn.qcis ?? "");
  if (!parentQcis) throw new Error("P02 representative QGNN QCIS is missing");
  const discovery = await runTianyanJob({ projectRoot: context.projectRoot, request: { action: "discover" }, timeoutSeconds: 180 });
  const configResult = await runTianyanJob({
    projectRoot: context.projectRoot,
    request: { action: "config_summary", machine_name: MACHINE_NAME },
    timeoutSeconds: 180,
  });
  if (discovery.exitCode !== 0 || configResult.exitCode !== 0) {
    const artifactSha256 = await registerJsonArtifact(context, asJson({
      schemaVersion: "qf.p03-tianyan176-discovery.v1",
      capturedAt: new Date().toISOString(),
      backend: MACHINE_NAME,
      discovery: boundedFailure(discovery.stdout),
      configuration: boundedFailure(configResult.stdout),
      available: false,
    }), "qf-p03.tianyan176-discovery");
    return { qcis: "", circuitHash: "", discoveryArtifactSha256: artifactSha256, backendAvailable: false, machineStatus: null };
  }
  const backends = Array.isArray(discovery.stdout.backends)
    ? discovery.stdout.backends as Array<Record<string, unknown>>
    : [];
  const backend = backends.find((candidate) => candidate.machine_name === MACHINE_NAME);
  const overview = configResult.stdout.overview as Record<string, unknown>;
  const cycle = findSixQubitCycle(overview.coupler_map as Record<string, unknown>);
  const qcis = remapSixQubits(representativeQgnnSubcircuit(parentQcis), cycle);
  const validation = await runTianyanJob({
    projectRoot: context.projectRoot,
    request: asJson({ action: "validate", machine_name: MACHINE_NAME, qcis }),
    timeoutSeconds: 120,
  });
  const available = Boolean(
    backend
    && backend.status === "running"
    && backend.toll === "free"
    && backend.target_type === "HARDWARE"
    && validation.exitCode === 0
    && validation.stdout.valid === true,
  );
  const artifactSha256 = await registerJsonArtifact(context, asJson({
    schemaVersion: "qf.p03-tianyan176-discovery.v1",
    capturedAt: new Date().toISOString(),
    backend: backend ? {
      machineName: backend.machine_name,
      status: backend.status,
      toll: backend.toll,
      targetType: backend.target_type,
    } : null,
    configuration: configResult.stdout,
    physicalQubitCycle: cycle,
    validation: validation.stdout,
    circuitHash: hash(qcis),
    available,
    queuePosition: null,
    estimatedStartAt: null,
  }), "qf-p03.tianyan176-discovery");
  return {
    qcis,
    circuitHash: hash(qcis),
    discoveryArtifactSha256: artifactSha256,
    backendAvailable: available,
    machineStatus: typeof backend?.status === "string" ? backend.status : null,
  };
}

function queueElapsedSeconds(run: P03QueueRunSummary, at: Date): number {
  return run.queueEnteredAt ? Math.max(0, (at.getTime() - Date.parse(run.queueEnteredAt)) / 1000) : 0;
}

export async function runP03Tianyan176QueueStep(context: P03Context): Promise<JsonObject> {
  assertP03Authorization(P03_AUTHORIZATION);
  if (context.campaignId !== P03_AUTHORIZATION.resumeCampaignId) {
    throw new Error("P03 must resume the authorized P02 Campaign ID");
  }
  let run = context.campaignRepository.ensureP03QueueRun({
    campaignId: context.campaignId,
    ordinal: 1,
    authorizationHash: p03AuthorizationHash(),
    backend: MACHINE_NAME,
    shots: SHOTS,
    purpose: "queue_state_machine_validation",
  });
  let justEnteredSubmitting = false;
  let submissionQcis: string | null = null;

  if (run.lifecycleState === "DISCOVERING") {
    const discovered = await mappedValidationCircuit(context);
    submissionQcis = discovered.qcis;
    context.campaignRepository.recordP03Discovery(
      run.queueRunId,
      discovered.machineStatus,
      discovered.discoveryArtifactSha256,
    );
    if (!discovered.backendAvailable) {
      run = context.campaignRepository.markP03BackendUnavailable(run.queueRunId, discovered.discoveryArtifactSha256);
      context.campaignRepository.recordFailure({
        campaignId: context.campaignId,
        stage: "p03_tianyan176_discovery",
        category: "TIANYAN176_BACKEND_UNAVAILABLE",
        summary: "tianyan176 discovery, free-hardware classification, configuration, or circuit validation was unavailable.",
        evidenceArtifactSha256: discovered.discoveryArtifactSha256,
        attempts: 1,
        recoveryCommand: `npm run campaign:p03-queue-step -- ${context.campaignId}`,
      });
      return asJson({ run, foregroundStop: true, recoveryCommand: `npm run campaign:p03-queue-step -- ${context.campaignId}` });
    }
    const requestSnapshot = {
      schemaVersion: "qf.p03-tianyan176-request.v1",
      campaignId: context.campaignId,
      authorizationHash: p03AuthorizationHash(),
      backend: MACHINE_NAME,
      shots: SHOTS,
      purpose: "queue_state_machine_validation",
      circuitHash: discovered.circuitHash,
      reservedExecutionSeconds: RESERVED_SECONDS,
      formalTestSealed: true,
    };
    const requestHash = hash(requestSnapshot);
    run = context.campaignRepository.prepareP03QueueSubmission({
      queueRunId: run.queueRunId,
      circuitHash: discovered.circuitHash,
      requestHash,
      idempotencyKey: `${context.campaignId}:P03:1:${requestHash}`,
      reservedExecutionSeconds: RESERVED_SECONDS,
    });
  }

  if (run.lifecycleState === "READY_TO_SUBMIT") {
    if (!submissionQcis) {
      const recoveredCircuit = await mappedValidationCircuit(context);
      if (!recoveredCircuit.backendAvailable || recoveredCircuit.circuitHash !== run.circuitHash) {
        throw new Error("recovered P03 circuit or backend no longer matches the atomically prepared request");
      }
      submissionQcis = recoveredCircuit.qcis;
    }
    run = context.campaignRepository.markP03Submitting(run.queueRunId);
    justEnteredSubmitting = true;
  }
  if (run.lifecycleState === "SUBMITTING" && !justEnteredSubmitting) {
    const artifactSha256 = await registerJsonArtifact(context, asJson({
      schemaVersion: "qf.p03-unknown-submission.v1",
      capturedAt: new Date().toISOString(),
      queueRunId: run.queueRunId,
      queryId: run.queryId,
      reason: "process resumed from SUBMITTING; whether the network request crossed the boundary is unknown",
      policy: "query_only_never_resubmit",
    }), "qf-p03.tianyan176-unknown-submission");
    run = context.campaignRepository.markP03SubmissionUnknown(run.queueRunId, artifactSha256);
    return asJson({ run, foregroundStop: true, recoveryCommand: `npm run campaign:p03-queue-step -- ${context.campaignId}` });
  }
  if (run.lifecycleState === "SUBMITTING" && justEnteredSubmitting) {
    const submission = await runTianyanJob({
      projectRoot: context.projectRoot,
      request: asJson({
        action: "submit",
        commit_authorized: true,
        authorization_phase: "P03",
        approval_hash: p03AuthorizationHash(),
        target_type: "HARDWARE",
        purpose: "queue_state_machine_validation",
        shots: SHOTS,
        estimated_execution_seconds: RESERVED_SECONDS,
        previous_p03_hardware_execution_seconds: context.campaignRepository.getP03HardwareBudget(context.campaignId)
          .p03ReservedExecutionSeconds - RESERVED_SECONDS,
        qcis: submissionQcis,
        machine_name: MACHINE_NAME,
      }),
      timeoutSeconds: 180,
    });
    if (submission.exitCode !== 0 || submission.stdout.status === "FAILED" || !submission.stdout.query_id) {
      const artifactSha256 = await registerJsonArtifact(context, asJson({
        schemaVersion: "qf.p03-unknown-submission.v1",
        capturedAt: new Date().toISOString(),
        queueRunId: run.queueRunId,
        evidence: boundedFailure(submission.stdout),
        policy: "query_only_never_resubmit",
      }), "qf-p03.tianyan176-unknown-submission");
      run = context.campaignRepository.markP03SubmissionUnknown(run.queueRunId, artifactSha256);
      context.campaignRepository.recordFailure({
        campaignId: context.campaignId,
        stage: "p03_tianyan176_submission",
        category: "UNKNOWN_SUBMISSION",
        summary: "tianyan176 submission outcome is unknown; automatic resubmission is prohibited.",
        evidenceArtifactSha256: artifactSha256,
        attempts: 1,
        recoveryCommand: `npm run campaign:p03-queue-step -- ${context.campaignId}`,
      });
      return asJson({ run, foregroundStop: true, recoveryCommand: `npm run campaign:p03-queue-step -- ${context.campaignId}` });
    }
    const queryId = String(submission.stdout.query_id);
    const nextQueryAt = new Date(Date.now() + computeP03BackoffSeconds(0, Math.random()) * 1000).toISOString();
    run = context.campaignRepository.markP03Submitted(run.queueRunId, queryId, "UNKNOWN", nextQueryAt);
    const artifactSha256 = await registerJsonArtifact(context, asJson({
      schemaVersion: "qf.p03-tianyan176-submission.v1",
      capturedAt: new Date().toISOString(),
      queueRunId: run.queueRunId,
      queryId,
      backend: MACHINE_NAME,
      shots: SHOTS,
      circuitHash: run.circuitHash,
      lifecycleState: run.lifecycleState,
      providerStatus: "UNKNOWN",
      queuePosition: null,
      estimatedStartAt: null,
    }), "qf-p03.tianyan176-submission", run.discoveryArtifactSha256 ? [run.discoveryArtifactSha256] : []);
    context.campaignRepository.recordP03StateArtifact(run.queueRunId, artifactSha256);
    if (run.quantumJobId) context.campaignRepository.markExternalRequestForQuantumJob(run.quantumJobId, "SUBMITTED", artifactSha256);
    run = context.campaignRepository.getP03QueueRun(run.queueRunId);
    return asJson({ run, submittedNewJob: true, foregroundStop: false });
  }

  if (run.lifecycleState === "UNKNOWN" && !run.queryId) {
    return asJson({
      run,
      submittedNewJob: false,
      foregroundStop: true,
      blocker: "UNKNOWN_SUBMISSION_WITHOUT_QUERY_ID",
      recoveryCommand: `npm run campaign:p03-queue-step -- ${context.campaignId}`,
    });
  }
  if (new Set(["COMPLETED", "FAILED", "CANCELLED", "BACKEND_UNAVAILABLE"]).has(run.lifecycleState)) {
    return asJson({ run, submittedNewJob: false, foregroundStop: true });
  }
  if (!run.queryId) throw new Error(`P03 state ${run.lifecycleState} has no Query ID`);
  if (run.nextQueryAt && Date.parse(run.nextQueryAt) > Date.now()) {
    return asJson({ run, submittedNewJob: false, foregroundStop: false });
  }

  const queriedAt = new Date();
  const query = await runTianyanJob({
    projectRoot: context.projectRoot,
    request: asJson({
      action: "query",
      machine_name: MACHINE_NAME,
      query_id: run.queryId,
      max_wait_seconds: 10,
      poll_interval_seconds: 5,
    }),
    timeoutSeconds: 20,
  });
  context.campaignRepository.addTiming(context.campaignId, "external", query.durationSeconds);
  if (query.exitCode === 0 && terminalResult(query.stdout)) {
    const rawResultArtifactSha256 = await registerJsonArtifact(context, asJson({
      schemaVersion: "qf.p03-tianyan176-terminal.v1",
      capturedAt: queriedAt.toISOString(),
      backend: MACHINE_NAME,
      machineStatus: run.machineStatus,
      queryId: run.queryId,
      shots: run.shots,
      circuitHash: run.circuitHash,
      rawReceipt: query.stdout,
      summary: probabilitySummary(query.stdout),
      conservativeExecutionSeconds: RESERVED_SECONDS,
      queuePosition: null,
      estimatedStartAt: null,
    }), "qf-p03.tianyan176-terminal", run.stateArtifactSha256 ? [run.stateArtifactSha256] : []);
    run = context.campaignRepository.completeP03QueueRun({
      queueRunId: run.queueRunId,
      terminalState: "COMPLETED",
      providerStatus: "COMPLETED",
      rawResultArtifactSha256,
    });
    context.campaignRepository.checkpoint(context.campaignId, "p03_tianyan176_terminal", asJson({
      queueRunId: run.queueRunId,
      queryId: run.queryId,
      backend: run.backend,
      shots: run.shots,
      rawResultArtifactSha256,
      p03Budget: context.campaignRepository.getP03HardwareBudget(context.campaignId),
    }));
    return asJson({ run, submittedNewJob: false, foregroundStop: true });
  }

  const pollArtifactSha256 = await registerJsonArtifact(context, asJson({
    schemaVersion: "qf.p03-tianyan176-poll.v1",
    queriedAt: queriedAt.toISOString(),
    backend: MACHINE_NAME,
    queryId: run.queryId,
    localLifecycleState: "QUEUED",
    providerStatus: "UNKNOWN",
    queuePosition: null,
    estimatedStartAt: null,
    boundedWaitSeconds: 10,
    evidence: boundedFailure(query.stdout),
  }), "qf-p03.tianyan176-poll", run.stateArtifactSha256 ? [run.stateArtifactSha256] : []);
  const nextQueryAt = new Date(
    Date.now() + computeP03BackoffSeconds(run.pollAttempts + 1, Math.random()) * 1000,
  ).toISOString();
  run = context.campaignRepository.recordP03Poll({
    queueRunId: run.queueRunId,
    lifecycleState: run.lifecycleState === "RUNNING" ? "RUNNING" : "QUEUED",
    providerStatus: "UNKNOWN",
    lastQueriedAt: queriedAt.toISOString(),
    nextQueryAt,
    stateArtifactSha256: pollArtifactSha256,
  });
  const elapsed = queueElapsedSeconds(run, queriedAt);
  if (elapsed >= 30 * 60 && !run.checkpoint30mAt) {
    context.campaignRepository.checkpoint(context.campaignId, "p03_tianyan176_queue_30m", asJson({
      queueRunId: run.queueRunId,
      queryId: run.queryId,
      lifecycleState: run.lifecycleState,
      lastQueriedAt: run.lastQueriedAt,
      nextQueryAt: run.nextQueryAt,
      queuePosition: null,
      estimatedStartAt: null,
    }));
    run = context.campaignRepository.markP03Checkpoint30m(run.queueRunId);
  }
  if (elapsed >= 180 * 60) {
    const failureArtifactSha256 = await registerJsonArtifact(context, asJson({
      schemaVersion: "qf.p03-waiting-tianyan176-queue.v1",
      capturedAt: new Date().toISOString(),
      queueRunId: run.queueRunId,
      queryId: run.queryId,
      elapsedQueueSeconds: elapsed,
      lastQueriedAt: run.lastQueriedAt,
      nextQueryAt: run.nextQueryAt,
      recoveryCommand: `npm run campaign:p03-queue-resume -- ${context.campaignId}`,
    }), "qf-p03.tianyan176-queue-waiting", [pollArtifactSha256]);
    context.campaignRepository.recordFailure({
      campaignId: context.campaignId,
      stage: "p03_tianyan176_queue",
      category: "WAITING_TIANYAN176_QUEUE",
      summary: "tianyan176 remained without a terminal result for 180 minutes; foreground ended with the original Query ID preserved.",
      evidenceArtifactSha256: failureArtifactSha256,
      attempts: run.pollAttempts,
      recoveryCommand: `npm run campaign:p03-queue-resume -- ${context.campaignId}`,
    });
    return asJson({ run, submittedNewJob: false, foregroundStop: true, failureArtifactSha256 });
  }
  return asJson({ run, submittedNewJob: false, foregroundStop: false });
}

export function finalizeP03Campaign(context: P03Context): JsonObject {
  const queueRun = context.campaignRepository.listP03QueueRuns(context.campaignId).at(-1) ?? null;
  const normalizedQueueRun = queueRun?.terminalAt
    ? context.campaignRepository.backfillP03TerminalQueryTimestamp(queueRun.queueRunId)
    : queueRun;
  const sseGate = context.campaignRepository.getGateDecision(context.campaignId, "G10_P03_FIXED_MODEL_SSE");
  const cloudJobs = OLD_CLOUD_QUERY_IDS.map((queryId) => (
    context.campaignRepository.findQuantumJobByQueryId(context.campaignId, queryId)
  ));
  const cloudComplete = cloudJobs.every((job) => job?.terminalStatus === "COMPLETED" && job.rawResultArtifactSha256);
  const failures = context.campaignRepository.listFailures(context.campaignId);
  const cloudFailure = [...failures].reverse().find((failure) => failure.category === "TIANYAN_CLOUD_NON_TERMINAL");
  const queueFailure = [...failures].reverse().find((failure) => failure.category === "WAITING_TIANYAN176_QUEUE");
  const providerFailure = [...failures].reverse().find((failure) => failure.category === "PROVIDER_SSE");
  let status: "BLOCKED" | "COMPLETED" = "BLOCKED";
  let blockerCategory = "P03_INCOMPLETE";
  let blockerArtifactSha256: string | null = normalizedQueueRun?.stateArtifactSha256 ?? null;
  if (sseGate?.decision !== "ALLOW") {
    blockerCategory = "BLOCKED_PROVIDER";
    blockerArtifactSha256 = providerFailure?.evidenceArtifactSha256 ?? sseGate?.evidenceArtifactSha256 ?? null;
  } else if (!normalizedQueueRun || normalizedQueueRun.lifecycleState !== "COMPLETED") {
    blockerCategory = normalizedQueueRun?.lifecycleState === "QUEUED"
      ? "WAITING_TIANYAN176_QUEUE"
      : "P03_TIANYAN176_INCOMPLETE";
    blockerArtifactSha256 = queueFailure?.evidenceArtifactSha256 ?? normalizedQueueRun?.stateArtifactSha256 ?? null;
  } else if (!cloudComplete) {
    blockerCategory = "TIANYAN_CLOUD_NON_TERMINAL";
    blockerArtifactSha256 = cloudFailure?.evidenceArtifactSha256 ?? null;
  } else {
    status = "COMPLETED";
    blockerCategory = "";
    blockerArtifactSha256 = null;
  }
  const campaign = context.campaignRepository.setCampaignState(context.campaignId, status, "p03_final_audit", {
    blockerCategory: blockerCategory || null,
    blockerArtifactSha256,
    level: "L4",
  });
  return asJson({
    schemaVersion: "qf.p03-final-audit.v1",
    capturedAt: new Date().toISOString(),
    campaign,
    sseGate,
    oldCloud: cloudJobs.map((job, index) => ({
      queryId: OLD_CLOUD_QUERY_IDS[index],
      status: job?.terminalStatus ?? "UNKNOWN",
      artifactSha256: job?.rawResultArtifactSha256 ?? null,
    })),
    p03QueueRun: normalizedQueueRun,
    p03Budget: context.campaignRepository.getP03HardwareBudget(context.campaignId),
    formalTestSealed: true,
  });
}
