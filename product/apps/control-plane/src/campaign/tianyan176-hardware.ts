import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import path from "node:path";

import type { JsonObject } from "@q-fintelligence/contracts";

import { storeArtifact } from "../artifact-store.js";
import type { RuntimeConfig } from "../config.js";
import type { WorkspaceRepository } from "../db/repository.js";
import { authorizationHash } from "./authorization.js";
import { runTianyanJob } from "./python-runner.js";
import type { CampaignRepository } from "./repository.js";

const MACHINE_NAME = "tianyan176";
const SHOTS = 100;
const CONSERVATIVE_EXECUTION_SECONDS = 240;

type HardwarePurpose = "representative_financial_qaoa" | "representative_qgnn_subcircuit";

interface CircuitPlan {
  purpose: HardwarePurpose;
  qcis: string;
  circuitHash: string;
}

interface StepResult {
  purpose: HardwarePurpose;
  quantumJobId: string;
  queryId: string | null;
  status: "COMPLETED" | "SUBMITTED" | "UNKNOWN_SUBMISSION";
  resultArtifactSha256: string | null;
}

export interface Tianyan176HardwareResult {
  schemaVersion: "qf.tianyan176-hardware-progress.v1";
  capturedAt: string;
  campaignId: string;
  backend: typeof MACHINE_NAME;
  shotsPerJob: typeof SHOTS;
  conservativeSecondsPerJob: typeof CONSERVATIVE_EXECUTION_SECONDS;
  connection: { status: unknown; toll: unknown; targetType: unknown };
  steps: StepResult[];
  stoppedReason: string | null;
  hardwareJobs: number;
  conservativeExecutionSeconds: number;
  analysisArtifactSha256: string | null;
  analysis: JsonObject | null;
  formalTestSealed: true;
}

function hash(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function qubitOrder(left: string, right: string): number {
  return Number(left.slice(1)) - Number(right.slice(1));
}

export function findSixQubitCycle(couplerMap: Record<string, unknown>): string[] {
  const adjacency = new Map<string, Set<string>>();
  for (const rawEdge of Object.values(couplerMap)) {
    if (!Array.isArray(rawEdge) || rawEdge.length !== 2) continue;
    const [left, right] = rawEdge.map(String);
    if (!/^Q\d+$/u.test(left!) || !/^Q\d+$/u.test(right!)) continue;
    const leftNeighbors = adjacency.get(left!) ?? new Set<string>();
    const rightNeighbors = adjacency.get(right!) ?? new Set<string>();
    leftNeighbors.add(right!);
    rightNeighbors.add(left!);
    adjacency.set(left!, leftNeighbors);
    adjacency.set(right!, rightNeighbors);
  }
  const starts = [...adjacency.keys()].sort(qubitOrder);
  for (const start of starts) {
    const visit = (path: string[]): string[] | null => {
      const current = path.at(-1)!;
      if (path.length === 6) return adjacency.get(current)?.has(start) ? path : null;
      const candidates = [...(adjacency.get(current) ?? [])].sort(qubitOrder);
      for (const candidate of candidates) {
        if (path.includes(candidate)) continue;
        const found = visit([...path, candidate]);
        if (found) return found;
      }
      return null;
    };
    const found = visit([start]);
    if (found) return found;
  }
  throw new Error("tianyan176 configuration has no six-qubit coupling cycle");
}

export function remapSixQubits(qcis: string, physicalQubits: string[]): string {
  return qcis.replace(/\bQ([0-5])\b/gu, (_match, index: string) => physicalQubits[Number(index)]!);
}

export function representativeQgnnSubcircuit(qcis: string): string {
  const lines = qcis.split(/\r?\n/u).map((line) => line.trim()).filter(Boolean);
  const selected = lines.filter((line) => /^RY Q[01] /u.test(line)).slice(0, 2);
  let collecting = false;
  let czCount = 0;
  for (const line of lines) {
    if (!collecting && line === "Y2M Q0") collecting = true;
    if (!collecting) continue;
    const qubits = [...line.matchAll(/\bQ\d+\b/gu)].map((match) => match[0]);
    if (qubits.some((qubit) => qubit !== "Q0" && qubit !== "Q1")) break;
    selected.push(line);
    if (line === "CZ Q1 Q0") czCount += 1;
    if (czCount === 2 && line === "Y2P Q0") break;
  }
  selected.push("M Q0", "M Q1");
  if (czCount !== 2) throw new Error("QGNN parent circuit does not contain the expected Q0-Q1 message block");
  return selected.join("\n");
}

function asJson(value: unknown): JsonObject {
  return value as JsonObject;
}

function terminalResult(value: JsonObject): boolean {
  return Array.isArray(value.result) && value.result.length > 0;
}

async function registerJsonArtifact(input: {
  value: unknown;
  producer: string;
  artifactRoot: string;
  conversationId: string;
  workspaceRepository: WorkspaceRepository;
  parentHashes?: string[];
}): Promise<string> {
  const data = new TextEncoder().encode(`${JSON.stringify(input.value, null, 2)}\n`);
  const manifest = await storeArtifact({
    root: input.artifactRoot,
    data,
    mediaType: "application/json",
    producer: input.producer,
    ...(input.parentHashes ? { parentHashes: input.parentHashes } : {}),
  });
  return input.workspaceRepository.registerArtifact(input.conversationId, manifest).sha256;
}

async function executeStep(input: {
  projectRoot: string;
  campaignId: string;
  conversationId: string;
  artifactRoot: string;
  parentArtifactSha256: string;
  plan: CircuitPlan;
  campaignRepository: CampaignRepository;
  workspaceRepository: WorkspaceRepository;
}): Promise<StepResult> {
  const requestSnapshot = {
    schemaVersion: "qf.tianyan176-hardware-request.v1",
    campaignId: input.campaignId,
    backend: MACHINE_NAME,
    targetType: "HARDWARE",
    purpose: input.plan.purpose,
    circuitHash: input.plan.circuitHash,
    shots: SHOTS,
    conservativeExecutionSeconds: CONSERVATIVE_EXECUTION_SECONDS,
    approvalHash: authorizationHash(),
    userDirection: "2026-07-21 tianyan176 minimal test then continue quantum layer",
  };
  const requestHash = hash(JSON.stringify(requestSnapshot));
  const idempotencyKey = `${input.campaignId}:tianyan176:${input.plan.purpose}:${requestHash}`;
  const existing = input.campaignRepository.listQuantumJobs(input.campaignId).find((candidate) => (
    candidate.targetType === "HARDWARE"
    && candidate.backend === MACHINE_NAME
    && candidate.purpose === input.plan.purpose
    && candidate.circuitHash === input.plan.circuitHash
  ));
  const externalRequestId = input.campaignRepository.prepareExternalRequest({
    campaignId: input.campaignId,
    provider: "tianyan",
    requestKind: "HARDWARE_SUBMIT",
    target: MACHINE_NAME,
    idempotencyKey,
    approvalHash: authorizationHash(),
    requestHash,
  });
  const job = existing ?? input.campaignRepository.createQuantumJob({
    campaignId: input.campaignId,
    externalRequestId,
    purpose: input.plan.purpose,
    backend: MACHINE_NAME,
    targetType: "HARDWARE",
    circuitHash: input.plan.circuitHash,
    shots: SHOTS,
    estimatedExecutionSeconds: CONSERVATIVE_EXECUTION_SECONDS,
  });
  if (job.rawResultArtifactSha256 && job.terminalStatus === "COMPLETED") {
    return {
      purpose: input.plan.purpose,
      quantumJobId: job.quantumJobId,
      queryId: job.queryId,
      status: "COMPLETED",
      resultArtifactSha256: job.rawResultArtifactSha256,
    };
  }

  let queryId = job.queryId;
  let submissionArtifactSha256: string | null = null;
  if (!queryId) {
    if (existing) {
      return {
        purpose: input.plan.purpose,
        quantumJobId: job.quantumJobId,
        queryId: null,
        status: "UNKNOWN_SUBMISSION",
        resultArtifactSha256: null,
      };
    }
    input.campaignRepository.markExternalRequest(externalRequestId, "COMMITTING");
    const submission = await runTianyanJob({
      projectRoot: input.projectRoot,
      request: asJson({
        action: "submit",
        commit_authorized: true,
        approval_hash: authorizationHash(),
        target_type: "HARDWARE",
        purpose: input.plan.purpose,
        shots: SHOTS,
        estimated_execution_seconds: CONSERVATIVE_EXECUTION_SECONDS,
        previous_hardware_execution_seconds: input.campaignRepository.getCampaign(input.campaignId).hardwareExecutionSeconds,
        qcis: input.plan.qcis,
        machine_name: MACHINE_NAME,
      }),
      timeoutSeconds: 180,
    });
    if (submission.exitCode !== 0 || submission.stdout.status === "FAILED") {
      input.campaignRepository.markExternalRequest(externalRequestId, "UNKNOWN");
      return {
        purpose: input.plan.purpose,
        quantumJobId: job.quantumJobId,
        queryId: null,
        status: "UNKNOWN_SUBMISSION",
        resultArtifactSha256: null,
      };
    }
    queryId = String(submission.stdout.query_id ?? "");
    if (!queryId) {
      input.campaignRepository.markExternalRequest(externalRequestId, "UNKNOWN");
      return {
        purpose: input.plan.purpose,
        quantumJobId: job.quantumJobId,
        queryId: null,
        status: "UNKNOWN_SUBMISSION",
        resultArtifactSha256: null,
      };
    }
    input.campaignRepository.markQuantumJobSubmitted(job.quantumJobId, queryId);
    submissionArtifactSha256 = await registerJsonArtifact({
      value: { ...submission.stdout, request: requestSnapshot },
      producer: `qf-tianyan176.${input.plan.purpose}.submission`,
      artifactRoot: input.artifactRoot,
      conversationId: input.conversationId,
      workspaceRepository: input.workspaceRepository,
      parentHashes: [input.parentArtifactSha256],
    });
    input.campaignRepository.markExternalRequest(externalRequestId, "SUBMITTED", queryId, submissionArtifactSha256);
  }

  const query = await runTianyanJob({
    projectRoot: input.projectRoot,
    request: asJson({
      action: "query",
      machine_name: MACHINE_NAME,
      query_id: queryId,
      max_wait_seconds: 240,
      poll_interval_seconds: 5,
    }),
    timeoutSeconds: 270,
  });
  if (query.exitCode !== 0 || !terminalResult(query.stdout)) {
    input.campaignRepository.markExternalRequest(externalRequestId, "SUBMITTED", queryId);
    return {
      purpose: input.plan.purpose,
      quantumJobId: job.quantumJobId,
      queryId,
      status: "SUBMITTED",
      resultArtifactSha256: null,
    };
  }
  const resultArtifactSha256 = await registerJsonArtifact({
    value: query.stdout,
    producer: `qf-tianyan176.${input.plan.purpose}.result`,
    artifactRoot: input.artifactRoot,
    conversationId: input.conversationId,
    workspaceRepository: input.workspaceRepository,
    parentHashes: [submissionArtifactSha256 ?? input.parentArtifactSha256],
  });
  input.campaignRepository.completeQuantumJob(
    job.quantumJobId,
    queryId,
    "COMPLETED",
    resultArtifactSha256,
    CONSERVATIVE_EXECUTION_SECONDS,
  );
  input.campaignRepository.markExternalRequest(externalRequestId, "COMPLETED", queryId, resultArtifactSha256);
  input.campaignRepository.recordBudget({
    campaignId: input.campaignId,
    resourceKind: "TIANYAN_HARDWARE",
    actionKey: idempotencyKey,
    calls: 1,
    shots: SHOTS,
    executionSeconds: CONSERVATIVE_EXECUTION_SECONDS,
    expectedEvidence: "tianyan176 query id, terminal raw result and content-addressed artifact",
  });
  return {
    purpose: input.plan.purpose,
    quantumJobId: job.quantumJobId,
    queryId,
    status: "COMPLETED",
    resultArtifactSha256,
  };
}

export async function runTianyan176Hardware(input: {
  projectRoot: string;
  campaignId: string;
  config: RuntimeConfig;
  campaignRepository: CampaignRepository;
  workspaceRepository: WorkspaceRepository;
}): Promise<Tianyan176HardwareResult> {
  const campaign = input.campaignRepository.getCampaign(input.campaignId);
  const scienceAction = input.campaignRepository.listActions(input.campaignId)
    .find((action) => action.stage === "run_science_pipeline" && action.outputArtifactSha256);
  if (!scienceAction?.outputArtifactSha256) throw new Error("P02 science result artifact is missing");
  const sciencePath = path.join(
    input.projectRoot,
    ".local",
    "campaigns",
    input.campaignId,
    "workspace",
    "results",
    "science-result.json",
  );
  const science = JSON.parse(await readFile(sciencePath, "utf8")) as Record<string, unknown>;
  const cqlibLocal = science.cqlib_local as Record<string, unknown>;
  const qaoa = cqlibLocal.qaoa as Record<string, unknown>;
  const qgnn = cqlibLocal.qgnn as Record<string, unknown>;
  const qaoaQcis = String(qaoa.qcis ?? "");
  const qgnnQcis = String(qgnn.qcis ?? "");
  if (!qaoaQcis || !qgnnQcis) throw new Error("P02 representative QCIS circuit is missing");

  const discovery = await runTianyanJob({
    projectRoot: input.projectRoot,
    request: { action: "discover" },
    timeoutSeconds: 180,
  });
  if (discovery.exitCode !== 0 || discovery.stdout.status === "FAILED") {
    throw new Error(String(discovery.stdout.message ?? "TianYan discovery failed"));
  }
  const backends = Array.isArray(discovery.stdout.backends)
    ? discovery.stdout.backends as Array<Record<string, unknown>>
    : [];
  const backend = backends.find((item) => item.machine_name === MACHINE_NAME);
  if (!backend || backend.status !== "running" || backend.toll !== "free" || backend.target_type !== "HARDWARE") {
    throw new Error("tianyan176 is not currently running, free, and classified as hardware");
  }
  const configResult = await runTianyanJob({
    projectRoot: input.projectRoot,
    request: { action: "config_summary", machine_name: MACHINE_NAME },
    timeoutSeconds: 180,
  });
  if (configResult.exitCode !== 0 || configResult.stdout.status === "FAILED") {
    throw new Error(String(configResult.stdout.message ?? "tianyan176 configuration query failed"));
  }
  const overview = configResult.stdout.overview as Record<string, unknown>;
  const physicalQubits = findSixQubitCycle(overview.coupler_map as Record<string, unknown>);
  const mappedQgnn = remapSixQubits(representativeQgnnSubcircuit(qgnnQcis), physicalQubits);
  const mappedQaoa = remapSixQubits(qaoaQcis, physicalQubits);
  const plans: CircuitPlan[] = [
    {
      purpose: "representative_qgnn_subcircuit",
      qcis: mappedQgnn,
      circuitHash: hash(mappedQgnn),
    },
    {
      purpose: "representative_financial_qaoa",
      qcis: mappedQaoa,
      circuitHash: hash(mappedQaoa),
    },
  ];
  const probeArtifactSha256 = await registerJsonArtifact({
    value: {
      schemaVersion: "qf.tianyan176-hardware-gate.v1",
      capturedAt: new Date().toISOString(),
      backend: {
        machineName: backend.machine_name,
        status: backend.status,
        toll: backend.toll,
        targetType: backend.target_type,
      },
      limits: { maxJobs: 2, maxCumulativeExecutionSeconds: 600, shotsPerJob: SHOTS },
      physicalQubitCycle: physicalQubits,
      circuitHashes: Object.fromEntries(plans.map((plan) => [plan.purpose, plan.circuitHash])),
      circuitPlans: plans.map((plan) => ({
        purpose: plan.purpose,
        qcis: plan.qcis,
        qcisSha256: plan.circuitHash,
      })),
      userDirection: "2026-07-21 tianyan176 minimal test then continue quantum layer",
    },
    producer: "qf-tianyan176.hardware-gate",
    artifactRoot: path.resolve(input.projectRoot, input.config.artifactRoot),
    conversationId: campaign.conversationId,
    workspaceRepository: input.workspaceRepository,
    parentHashes: [scienceAction.outputArtifactSha256],
  });
  input.campaignRepository.recordGate({
    campaignId: input.campaignId,
    gateName: "G8_TIANYAN176_USER_DIRECTED_MINIMAL",
    subjectHash: hash(JSON.stringify({ backend, shots: SHOTS, seconds: CONSERVATIVE_EXECUTION_SECONDS })),
    decision: "ALLOW",
    reason: "User explicitly directed a minimal tianyan176 test; live discovery returned running/free hardware and the standing two-job/600-second caps remain enforced.",
    evidenceArtifactSha256: probeArtifactSha256,
  });

  const steps: StepResult[] = [];
  let stoppedReason: string | null = null;
  for (const plan of plans) {
    const validation = await runTianyanJob({
      projectRoot: input.projectRoot,
      request: asJson({ action: "validate", machine_name: MACHINE_NAME, qcis: plan.qcis }),
      timeoutSeconds: 120,
    });
    if (validation.exitCode !== 0 || validation.stdout.valid !== true) {
      stoppedReason = `${plan.purpose} is not compatible with tianyan176`;
      break;
    }
    const step = await executeStep({
      projectRoot: input.projectRoot,
      campaignId: input.campaignId,
      conversationId: campaign.conversationId,
      artifactRoot: path.resolve(input.projectRoot, input.config.artifactRoot),
      parentArtifactSha256: scienceAction.outputArtifactSha256,
      plan,
      campaignRepository: input.campaignRepository,
      workspaceRepository: input.workspaceRepository,
    });
    steps.push(step);
    input.campaignRepository.checkpoint(input.campaignId, `tianyan176_${plan.purpose}`, asJson({
      backend: MACHINE_NAME,
      shots: SHOTS,
      conservativeExecutionSeconds: CONSERVATIVE_EXECUTION_SECONDS,
      ...step,
    }));
    if (step.status !== "COMPLETED") {
      stoppedReason = step.status === "SUBMITTED"
        ? "first hardware job has no terminal result; second job remains unsubmitted"
        : "submission outcome is unknown; automatic resubmission is prohibited";
      break;
    }
  }
  let analysisArtifactSha256: string | null = null;
  let analysis: JsonObject | null = null;
  if (steps.length === plans.length && steps.every((step) => step.status === "COMPLETED" && step.resultArtifactSha256)) {
    const analyzedSteps = [];
    for (const step of steps) {
      const manifest = input.workspaceRepository.getArtifactManifest(step.resultArtifactSha256!);
      const resultPath = path.join(input.projectRoot, input.config.artifactRoot, ...manifest.relativePath.split("/"));
      const raw = JSON.parse(await readFile(resultPath, "utf8")) as Record<string, unknown>;
      const resultEntry = Array.isArray(raw.result) ? raw.result[0] as Record<string, unknown> : null;
      const probabilities = resultEntry && typeof resultEntry.probability === "string"
        ? JSON.parse(resultEntry.probability) as Record<string, number>
        : {};
      const ranked = Object.entries(probabilities)
        .map(([bitstring, probability]) => ({
          bitstring,
          probability: Number(probability),
          ones: [...bitstring].filter((bit) => bit === "1").length,
        }))
        .sort((left, right) => right.probability - left.probability || left.bitstring.localeCompare(right.bitstring));
      analyzedSteps.push({
        purpose: step.purpose,
        queryId: step.queryId,
        rawResultArtifactSha256: step.resultArtifactSha256,
        resultEntries: Array.isArray(raw.result) ? raw.result.length : 0,
        probabilitySum: ranked.reduce((sum, item) => sum + item.probability, 0),
        topProbabilities: ranked.slice(0, 10),
        ...(step.purpose === "representative_financial_qaoa" ? {
          exactLocalTarget: String((science.qaoa as Record<string, unknown>).exact_optimal_bitstring ?? ""),
          exactLocalTargetProbability: probabilities[String((science.qaoa as Record<string, unknown>).exact_optimal_bitstring ?? "")] ?? 0,
          topFeasibleThreeAssetStates: ranked.filter((item) => item.ones === 3).slice(0, 10),
        } : {}),
      });
    }
    analysis = asJson({
      schemaVersion: "qf.tianyan176-hardware-analysis.v1",
      backend: MACHINE_NAME,
      physicalQubitCycle: physicalQubits,
      shotsPerJob: SHOTS,
      conservativeExecutionSeconds: steps.length * CONSERVATIVE_EXECUTION_SECONDS,
      formalTestSealed: true,
      scientificClaimBoundary: "hardware execution evidence only; no quantum advantage claim",
      steps: analyzedSteps,
    });
    analysisArtifactSha256 = await registerJsonArtifact({
      value: analysis,
      producer: "qf-tianyan176.hardware-analysis",
      artifactRoot: path.resolve(input.projectRoot, input.config.artifactRoot),
      conversationId: campaign.conversationId,
      workspaceRepository: input.workspaceRepository,
      parentHashes: [scienceAction.outputArtifactSha256, ...steps.map((step) => step.resultArtifactSha256!)],
    });
    input.campaignRepository.checkpoint(input.campaignId, "tianyan176_hardware_analysis", asJson({
      analysisArtifactSha256,
      hardwareJobs: steps.length,
      conservativeExecutionSeconds: steps.length * CONSERVATIVE_EXECUTION_SECONDS,
      formalTestSealed: true,
    }));
    input.campaignRepository.recordGate({
      campaignId: input.campaignId,
      gateName: "G9_TIANYAN176_HARDWARE_RESULTS",
      subjectHash: analysisArtifactSha256,
      decision: "ALLOW",
      reason: "Two authorized representative tianyan176 hardware jobs returned terminal raw results within the two-job and 600-second caps.",
      evidenceArtifactSha256: analysisArtifactSha256,
    });
    const current = input.campaignRepository.getCampaign(input.campaignId);
    input.campaignRepository.setCampaignState(input.campaignId, current.status, "final_audit", {
      blockerCategory: current.blockerCategory,
      blockerArtifactSha256: current.blockerArtifactSha256,
      level: "L4",
    });
  }
  const finalCampaign = input.campaignRepository.getCampaign(input.campaignId);
  return {
    schemaVersion: "qf.tianyan176-hardware-progress.v1",
    capturedAt: new Date().toISOString(),
    campaignId: input.campaignId,
    backend: MACHINE_NAME,
    shotsPerJob: SHOTS,
    conservativeSecondsPerJob: CONSERVATIVE_EXECUTION_SECONDS,
    connection: { status: backend.status, toll: backend.toll, targetType: backend.target_type },
    steps,
    stoppedReason,
    hardwareJobs: finalCampaign.hardwareJobs,
    conservativeExecutionSeconds: finalCampaign.hardwareExecutionSeconds,
    analysisArtifactSha256,
    analysis,
    formalTestSealed: true,
  };
}
