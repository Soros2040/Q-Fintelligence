import { createHash, randomUUID } from "node:crypto";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import path from "node:path";

import type { ArtifactSummary, CampaignRole, ChainLevel, JsonObject } from "@q-fintelligence/contracts";

import { OpenHandsRuntime } from "../agent/openhands-runtime.js";
import { storeArtifact } from "../artifact-store.js";
import { loadRuntimeConfig } from "../config.js";
import { openDatabase } from "../db/migrations.js";
import { WorkspaceRepository } from "../db/repository.js";
import { P02_AUTHORIZATION, authorizationHash } from "./authorization.js";
import { CampaignRepository } from "./repository.js";
import { probeFixedCampaignModel, type CapabilityProbeResult } from "./provider-probe.js";
import { runScienceJob, runTianyanJob } from "./python-runner.js";
import { campaignRoleToolExecutor, campaignRoleToolSpecs, probeCampaignRoleSession } from "./role-session.js";
import { campaignRoleDefinitions } from "./roles.js";
import { TushareAdapter, type TushareDatasetBundle } from "./tushare-adapter.js";

function hash(value: unknown): string {
  return createHash("sha256").update(typeof value === "string" ? value : JSON.stringify(value)).digest("hex");
}

function advanceLevel(current: ChainLevel, candidate: ChainLevel): ChainLevel {
  const order: ChainLevel[] = ["L0", "L1", "L2", "L3", "L4"];
  return order.indexOf(candidate) > order.indexOf(current) ? candidate : current;
}

function asJson(value: unknown): JsonObject {
  return value as JsonObject;
}

function redactedError(error: unknown): JsonObject {
  const message = error instanceof Error ? error.message : String(error);
  return { errorType: error instanceof Error ? error.name : "Error", message: message.slice(0, 1000) };
}

interface WorkerContext {
  projectRoot: string;
  workspaceRoot: string;
  artifactRoot: string;
  campaignId: string;
  conversationId: string;
  workerId: string;
  workspaceRepository: WorkspaceRepository;
  campaignRepository: CampaignRepository;
}

async function atomicJson(pathname: string, value: unknown): Promise<void> {
  await mkdir(path.dirname(pathname), { recursive: true });
  const temporary = `${pathname}.${process.pid}.tmp`;
  await writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, { encoding: "utf8", mode: 0o600 });
  await rename(temporary, pathname);
}

async function readJson<T>(pathname: string): Promise<T> {
  return JSON.parse(await readFile(pathname, "utf8")) as T;
}

async function registerJsonArtifact(
  context: WorkerContext,
  value: unknown,
  producer: string,
  parentHashes: string[] = [],
): Promise<ArtifactSummary> {
  const data = new TextEncoder().encode(`${JSON.stringify(value, null, 2)}\n`);
  const manifest = await storeArtifact({
    root: context.artifactRoot,
    data,
    mediaType: "application/json",
    producer,
    parentHashes,
  });
  return context.workspaceRepository.registerArtifact(context.conversationId, manifest);
}

async function executeJsonStage<T>(input: {
  context: WorkerContext;
  stage: string;
  actionType: string;
  expectedEvidence: string;
  inputIdentity: unknown;
  outputPath: string;
  timing: "active" | "external";
  run: () => Promise<T>;
  producer: string;
  parentHashes?: string[];
}): Promise<{ value: T; artifact: ArtifactSummary }> {
  const idempotencyKey = `${input.context.campaignId}:${input.stage}:${hash(input.inputIdentity)}`;
  const action = input.context.campaignRepository.beginAction({
    campaignId: input.context.campaignId,
    stage: input.stage,
    actionType: input.actionType,
    idempotencyKey,
    inputHash: hash(input.inputIdentity),
    expectedEvidence: input.expectedEvidence,
  });
  if (action.status === "COMPLETED" && action.outputArtifactSha256) {
    return {
      value: await readJson<T>(input.outputPath),
      artifact: input.context.workspaceRepository.getArtifact(action.outputArtifactSha256),
    };
  }
  const started = performance.now();
  try {
    const value = await input.run();
    await atomicJson(input.outputPath, value);
    const artifact = await registerJsonArtifact(input.context, value, input.producer, input.parentHashes);
    input.context.campaignRepository.completeAction(action.actionId, "COMPLETED", artifact.sha256);
    return { value, artifact };
  } catch (error) {
    const evidence = { schemaVersion: "qf.failure-evidence.v1", stage: input.stage, error: redactedError(error) };
    const artifact = await registerJsonArtifact(input.context, evidence, `${input.producer}.failure`, input.parentHashes);
    input.context.campaignRepository.completeAction(action.actionId, "BLOCKED", artifact.sha256, redactedError(error));
    input.context.campaignRepository.recordFailure({
      campaignId: input.context.campaignId,
      stage: input.stage,
      category: input.stage.includes("tushare") ? "DATA" : input.stage.includes("tianyan") ? "BACKEND" : "INFRA",
      summary: String(redactedError(error).message),
      evidenceArtifactSha256: artifact.sha256,
      attempts: 1,
      recoveryCommand: `npm run campaign:resume -- ${input.context.campaignId}`,
    });
    throw error;
  } finally {
    input.context.campaignRepository.addTiming(
      input.context.campaignId,
      input.timing,
      (performance.now() - started) / 1000,
    );
  }
}

function parseTerminalTianyanResult(result: JsonObject): boolean {
  const payload = result.result;
  return Array.isArray(payload) && payload.length > 0;
}

async function recordBlockingFailure(
  context: WorkerContext,
  stage: string,
  category: string,
  summary: string,
  evidence: unknown,
): Promise<ArtifactSummary> {
  const artifact = await registerJsonArtifact(context, evidence, `qf-campaign.${stage}.failure`);
  context.campaignRepository.recordFailure({
    campaignId: context.campaignId,
    stage,
    category,
    summary,
    evidenceArtifactSha256: artifact.sha256,
    attempts: 1,
    recoveryCommand: `npm run campaign:resume -- ${context.campaignId}`,
  });
  return artifact;
}

async function runCloudSimulatorJob(input: {
  context: WorkerContext;
  purpose: "representative_qgnn_subcircuit" | "representative_financial_qaoa";
  backend: string;
  qcis: string;
  parentArtifact: string;
}): Promise<{ completed: boolean; artifactSha256?: string; queryId?: string }> {
  const requestSnapshot = {
    backend: input.backend,
    targetType: "SIMULATOR",
    purpose: "cloud_simulator",
    scientificPurpose: input.purpose,
    circuitHash: hash(input.qcis),
    shots: P02_AUTHORIZATION.simulatorShotsPerJob,
    maxSimulatorJobs: P02_AUTHORIZATION.maxSimulatorJobs,
    approvalHash: authorizationHash(),
  };
  const requestHash = hash(requestSnapshot);
  const idempotencyKey = `${input.context.campaignId}:tianyan:${requestHash}`;
  const externalRequestId = input.context.campaignRepository.prepareExternalRequest({
    campaignId: input.context.campaignId,
    provider: "tianyan",
    requestKind: "CLOUD_SIMULATOR_SUBMIT",
    target: input.backend,
    idempotencyKey,
    approvalHash: authorizationHash(),
    requestHash,
  });
  const job = input.context.campaignRepository.createQuantumJob({
    campaignId: input.context.campaignId,
    externalRequestId,
    purpose: "cloud_simulator",
    backend: input.backend,
    targetType: "SIMULATOR",
    circuitHash: hash(input.qcis),
    shots: P02_AUTHORIZATION.simulatorShotsPerJob,
  });
  if (job.queryId && job.terminalStatus && job.rawResultArtifactSha256) {
    return { completed: true, artifactSha256: job.rawResultArtifactSha256, queryId: job.queryId };
  }
  let queryId = job.queryId;
  let queryParentArtifact = input.parentArtifact;
  const submittedAt = performance.now();
  if (!queryId) {
    input.context.campaignRepository.markExternalRequest(externalRequestId, "COMMITTING");
    const submission = await runTianyanJob({
      projectRoot: input.context.projectRoot,
      request: asJson({
        action: "submit",
        commit_authorized: true,
        approval_hash: authorizationHash(),
        target_type: "SIMULATOR",
        purpose: "cloud_simulator",
        shots: P02_AUTHORIZATION.simulatorShotsPerJob,
        qcis: input.qcis,
        machine_name: input.backend,
      }),
      timeoutSeconds: 180,
    });
    input.context.campaignRepository.addTiming(input.context.campaignId, "external", submission.durationSeconds);
    if (submission.exitCode !== 0 || submission.stdout.status === "FAILED") {
      input.context.campaignRepository.markExternalRequest(externalRequestId, "UNKNOWN");
      throw new Error(`TianYan submission returned unknown state: ${String(submission.stdout.message ?? "adapter failure")}`);
    }
    queryId = String(submission.stdout.query_id ?? "");
    if (!queryId) {
      input.context.campaignRepository.markExternalRequest(externalRequestId, "UNKNOWN");
      throw new Error("TianYan submission returned no query id; automatic resubmission is prohibited");
    }
    input.context.campaignRepository.markQuantumJobSubmitted(job.quantumJobId, queryId);
    input.context.campaignRepository.markExternalRequest(externalRequestId, "SUBMITTED", queryId);
    const submissionArtifact = await registerJsonArtifact(
      input.context,
      submission.stdout,
      "qf-tianyan.submission",
      [input.parentArtifact],
    );
    queryParentArtifact = submissionArtifact.sha256;
    input.context.campaignRepository.markExternalRequest(externalRequestId, "SUBMITTED", queryId, submissionArtifact.sha256);
  }

  const query = await runTianyanJob({
    projectRoot: input.context.projectRoot,
    request: asJson({
      action: "query",
      machine_name: input.backend,
      query_id: queryId,
      max_wait_seconds: 600,
      poll_interval_seconds: 10,
    }),
    timeoutSeconds: 660,
  });
  input.context.campaignRepository.addTiming(input.context.campaignId, "external", query.durationSeconds);
  if (query.exitCode !== 0 || !parseTerminalTianyanResult(query.stdout)) {
    input.context.campaignRepository.markExternalRequest(externalRequestId, "SUBMITTED", queryId);
    return { completed: false, queryId };
  }
  const resultArtifact = await registerJsonArtifact(
    input.context,
    query.stdout,
    "qf-tianyan.query-result",
    [queryParentArtifact],
  );
  input.context.campaignRepository.completeQuantumJob(job.quantumJobId, queryId, "COMPLETED", resultArtifact.sha256);
  input.context.campaignRepository.markExternalRequest(externalRequestId, "COMPLETED", queryId, resultArtifact.sha256);
  input.context.campaignRepository.recordBudget({
    campaignId: input.context.campaignId,
    resourceKind: "TIANYAN_SIMULATOR",
    actionKey: idempotencyKey,
    calls: 1,
    shots: P02_AUTHORIZATION.simulatorShotsPerJob,
    executionSeconds: (performance.now() - submittedAt) / 1000,
    expectedEvidence: "query id, terminal result, backend and raw response artifact",
  });
  return { completed: true, artifactSha256: resultArtifact.sha256, queryId };
}

async function main(): Promise<void> {
  if (process.env.QF_PROCESS_NAMESPACE !== "qfintelligence") throw new Error("campaign worker namespace is invalid");
  const projectRoot = process.cwd();
  
  const campaignId = process.argv[2] ?? process.env.QF_CAMPAIGN_ID;
  if (!campaignId) throw new Error("campaign id is required");
  const config = loadRuntimeConfig();
  const migration = openDatabase(path.resolve(projectRoot, config.sqlitePath), path.join(projectRoot, "infra", "sqlite"));
  const workspaceRepository = new WorkspaceRepository(migration.database);
  const campaignRepository = new CampaignRepository(migration.database);
  const campaign = campaignRepository.getCampaign(campaignId);
  const workspaceRoot = path.join(projectRoot, ".local", "campaigns", campaignId, "workspace");
  const context: WorkerContext = {
    projectRoot,
    workspaceRoot,
    artifactRoot: path.resolve(projectRoot, config.artifactRoot),
    campaignId,
    conversationId: campaign.conversationId,
    workerId: `campaign_worker_${randomUUID()}`,
    workspaceRepository,
    campaignRepository,
  };
  await Promise.all(["input", "normalized", "results", "plots", "logs", "manifests", "sessions", "code"]
    .map((directory) => mkdir(path.join(workspaceRoot, directory), { recursive: true })));
  campaignRepository.acquireLease(campaignId, context.workerId);
  const heartbeat = setInterval(() => campaignRepository.heartbeat(campaignId, context.workerId), 30_000);
  heartbeat.unref();
  let highestLevel: ChainLevel = campaign.highestChainLevel;
  const blockers: Array<{ category: string; artifactSha256: string }> = [];
  try {
    campaignRepository.setCampaignState(campaignId, "PREFLIGHT", "environment_and_authorization");
    campaignRepository.checkpoint(campaignId, "environment_and_authorization", asJson({
      authorizationHash: authorizationHash(),
      formalTestSealed: true,
      fixedModel: "openai/getoken/gpt-5.6-sol",
      hardwareMaxJobs: 2,
      hardwareMaxCumulativeExecutionSeconds: 600,
    }));

    const providerPath = path.join(workspaceRoot, "manifests", "provider-capability.json");
    const providerStage = await executeJsonStage<CapabilityProbeResult>({
      context,
      stage: "preflight_provider_capabilities",
      actionType: "PROVIDER_CAPABILITY_PROBE",
      expectedEvidence: "catalog, text, stream, tool and JSON schema results for fixed gpt-5.6-sol",
      inputIdentity: { model: "gpt-5.6-sol", authorizationHash: authorizationHash() },
      outputPath: providerPath,
      timing: "external",
      producer: "qf-provider.capability-probe",
      run: async () => {
        const baseUrl = process.env.OPENAI_BASE_URL;
        const apiKey = process.env.OPENAI_API_KEY;
        if (!baseUrl || !apiKey) throw new Error("fixed provider credentials are not configured");
        return probeFixedCampaignModel({
          baseUrl,
          apiKey,
          onBillableCall: () => {
            const calls = campaignRepository.incrementLlmCalls(campaignId);
            if (calls > 120) throw new Error("LLM circuit breaker reached");
          },
        });
      },
    });
    campaignRepository.recordBudget({
      campaignId,
      resourceKind: "LLM",
      actionKey: `${campaignId}:provider-capability`,
      calls: campaignRepository.getCampaign(campaignId).llmCalls,
      expectedEvidence: "fixed-model capability matrix",
    });
    if (providerStage.value.overall !== "READY") {
      for (const definition of campaignRoleDefinitions()) {
        campaignRepository.registerAgentSessionState(campaignId, definition.role, "BLOCKED_PROVIDER");
      }
      campaignRepository.recordGate({
        campaignId,
        gateName: "G1_MODEL_CAPABILITY",
        subjectHash: hash(providerStage.value),
        decision: "BLOCK",
        reason: "The fixed model failed at least one required capability and fallback is prohibited.",
        evidenceArtifactSha256: providerStage.artifact.sha256,
      });
      const failure = campaignRepository.recordFailure({
        campaignId,
        stage: "preflight_provider_capabilities",
        category: "PROVIDER",
        summary: "Fixed gpt-5.6-sol capability gate is blocked; deterministic local and external adapters continue without model substitution.",
        evidenceArtifactSha256: providerStage.artifact.sha256,
        attempts: 1,
        recoveryCommand: `npm run campaign:resume -- ${campaignId}`,
      });
      blockers.push({ category: "BLOCKED_PROVIDER", artifactSha256: failure.evidenceArtifactSha256 });
    } else {
      campaignRepository.recordGate({
        campaignId,
        gateName: "G1_MODEL_CAPABILITY",
        subjectHash: hash(providerStage.value),
        decision: "ALLOW",
        reason: "Fixed model passed catalog, text, streaming, tool and structured-output probes.",
        evidenceArtifactSha256: providerStage.artifact.sha256,
      });
      const baseUrl = process.env.OPENAI_BASE_URL!;
      const apiKey = process.env.OPENAI_API_KEY!;
      const parentConversation = workspaceRepository.getConversation(campaign.conversationId);
      for (const definition of campaignRoleDefinitions()) {
        const roleStarted = performance.now();
        campaignRepository.incrementLlmCalls(campaignId);
        try {
          const roleTitle = `P02 ${campaignId} ${definition.role}`;
          const existingRoleConversation = workspaceRepository
            .listConversations(parentConversation.projectId, true)
            .find((conversation) => conversation.title === roleTitle);
          const roleConversation = existingRoleConversation ?? workspaceRepository.createConversation({
            projectId: parentConversation.projectId,
            title: roleTitle,
            mode: "OPENHANDS",
            provider: "openai",
            modelId: "gpt-5.6-sol",
          });
          if (
            roleConversation.mode !== "OPENHANDS"
            || roleConversation.archived
            || roleConversation.provider !== "openai"
            || roleConversation.modelId !== "gpt-5.6-sol"
          ) {
            throw new Error(`P02 role ${definition.role} has a legacy read-only runtime conversation`);
          }
          const sessionRoot = path.resolve(projectRoot, config.openHandsSessionRoot);
          const runtime = await OpenHandsRuntime.create({
            projectRoot,
            sessionRoot,
            sidecarPath: path.resolve(projectRoot, config.openHandsSidecarPath),
            promptVersion: definition.promptVersion,
            provider: "openai",
            modelId: "gpt-5.6-sol",
            baseUrl,
            apiKey,
            repository: workspaceRepository,
            conversationId: roleConversation.conversationId,
            systemPrompt: definition.prompt,
            systemPromptHash: definition.promptHash,
            toolNames: definition.toolNames,
            toolSpecs: campaignRoleToolSpecs(definition),
            toolExecutor: campaignRoleToolExecutor(definition),
          });
          const runtimeSession = workspaceRepository.getSession(roleConversation.conversationId);
          const roleResult = await probeCampaignRoleSession({
            runtime,
            sessionId: runtime.runtimeSessionId,
            sessionFile: runtimeSession?.relativeSessionFile
              ? path.resolve(sessionRoot, runtimeSession.relativeSessionFile)
              : null,
            definition,
          });
          campaignRepository.registerAgentSessionState(campaignId, definition.role, "READY");
          await registerJsonArtifact(context, roleResult, `qf-openhands-role.${definition.role}`, [providerStage.artifact.sha256]);
        } catch (error) {
          const artifact = await recordBlockingFailure(
            context,
            `openhands_role_${definition.role}`,
            "PROVIDER",
            `OpenHands role ${definition.role} failed its real tool probe`,
            { role: definition.role, error: redactedError(error) },
          );
          campaignRepository.registerAgentSessionState(campaignId, definition.role, "BLOCKED_PROVIDER");
          blockers.push({ category: "BLOCKED_PROVIDER", artifactSha256: artifact.sha256 });
          break;
        } finally {
          campaignRepository.addTiming(campaignId, "external", (performance.now() - roleStarted) / 1000);
        }
      }
    }

    campaignRepository.setCampaignState(campaignId, "RUNNING", "fetch_tushare_raw_data");
    const bundlePath = path.join(workspaceRoot, "input", "tushare-bundle.json");
    let bundleStage: { value: TushareDatasetBundle; artifact: ArtifactSummary };
    try {
      bundleStage = await executeJsonStage<TushareDatasetBundle>({
        context,
        stage: "fetch_tushare_raw_data",
        actionType: "TUSHARE_READONLY_FETCH",
        expectedEvidence: "point-in-time HS300, historical SW L1 membership, 2019 liquidity and 2019-2023 cache manifest",
        inputIdentity: {
          snapshot: "2020-01",
          membershipEffective: "2019-12-31",
          range: ["2019-01-01", "2023-12-31"],
          targets: ["银行", "食品饮料", "医药生物", "电子", "公用事业", "交通运输"],
        },
        outputPath: bundlePath,
        timing: "external",
        producer: "qf-tushare.dataset-bundle",
        run: async () => {
          const token = process.env.TUSHARE_TOKEN;
          if (!token) throw new Error("TUSHARE_TOKEN is not configured");
          return new TushareAdapter(token, path.join(projectRoot, ".local", "market-cache", "tushare"))
            .fetchHistoricalBundle();
        },
      });
    } catch (error) {
      const failure = campaignRepository.listFailures(campaignId).at(-1);
      campaignRepository.setCampaignState(campaignId, "BLOCKED", "fetch_tushare_raw_data", {
        blockerCategory: "BLOCKED_DATA",
        blockerArtifactSha256: failure?.evidenceArtifactSha256 ?? null,
        level: highestLevel,
      });
      return;
    }
    highestLevel = advanceLevel(highestLevel, "L1");
    campaignRepository.recordGate({
      campaignId,
      gateName: "G4_POINT_IN_TIME_DATA",
      subjectHash: bundleStage.artifact.sha256,
      decision: "ALLOW",
      reason: "Historical component, industry effective dates, liquidity selection and sealed date range were proven.",
      evidenceArtifactSha256: bundleStage.artifact.sha256,
    });
    campaignRepository.setCampaignState(campaignId, "RUNNING", "run_science_pipeline", { level: highestLevel });

    const sciencePath = path.join(workspaceRoot, "results", "science-result.json");
    const scienceStage = await executeJsonStage<JsonObject>({
      context,
      stage: "run_science_pipeline",
      actionType: "REGISTERED_SCIENCE_JOB",
      expectedEvidence: "labels, Ridge VAR/gFEVD, validation-only baselines, 6-qubit QGNN, 20 portfolios, p=1 QAOA and cqlib local results",
      inputIdentity: { bundleSha256: bundleStage.artifact.sha256, seed: 20260721, protocol: authorizationHash() },
      outputPath: sciencePath,
      timing: "active",
      producer: "qf-science.pipeline-result",
      parentHashes: [bundleStage.artifact.sha256],
      run: async () => {
        const result = await runScienceJob({
          projectRoot,
          request: asJson({
            action: "run_pipeline",
            workspace_root: workspaceRoot,
            input_path: bundlePath,
            output_path: sciencePath,
            seed: 20260721,
          }),
        });
        if (result.exitCode !== 0 || result.stdout.status !== "COMPLETED") {
          throw new Error(`science runner failed: ${String(result.stdout.message ?? "unknown failure")}`);
        }
        return readJson<JsonObject>(sciencePath);
      },
    });
    highestLevel = advanceLevel(highestLevel, "L2");
    campaignRepository.recordGate({
      campaignId,
      gateName: "G6_SCIENCE_AND_LOCAL_QUANTUM",
      subjectHash: scienceStage.artifact.sha256,
      decision: "ALLOW",
      reason: "Validation-only science, exact enumeration, QAOA and cqlib local simulation completed with a sealed formal test interval.",
      evidenceArtifactSha256: scienceStage.artifact.sha256,
    });
    campaignRepository.setCampaignState(campaignId, "RUNNING", "discover_tianyan_backends", { level: highestLevel });

    const discoveryPath = path.join(workspaceRoot, "manifests", "tianyan-backends.json");
    let discoveryStage: { value: JsonObject; artifact: ArtifactSummary } | null = null;
    try {
      discoveryStage = await executeJsonStage<JsonObject>({
        context,
        stage: "discover_tianyan_backends",
        actionType: "TIANYAN_READONLY_DISCOVERY",
        expectedEvidence: "live non-secret backend names, states, simulator/hardware classification and runtime-bound availability",
        inputIdentity: { cqlib: "1.3.11", authorizationHash: authorizationHash() },
        outputPath: discoveryPath,
        timing: "external",
        producer: "qf-tianyan.backend-discovery",
        run: async () => {
          const result = await runTianyanJob({ projectRoot, request: { action: "discover" } });
          if (result.exitCode !== 0 || result.stdout.status === "FAILED") {
            throw new Error(String(result.stdout.message ?? "TianYan discovery failed"));
          }
          return result.stdout;
        },
      });
    } catch (error) {
      const failure = campaignRepository.listFailures(campaignId).at(-1);
      if (failure) blockers.push({ category: "BLOCKED_TIANYAN_DISCOVERY", artifactSha256: failure.evidenceArtifactSha256 });
    }

    if (discoveryStage) {
      const backends = Array.isArray(discoveryStage.value.backends)
        ? discoveryStage.value.backends as Array<Record<string, unknown>>
        : [];
      const simulator = backends.find((item) => item.machine_name === "tianyan_sw" && item.status === "running")
        ?? backends.find((item) => item.target_type === "SIMULATOR" && item.status === "running");
      if (simulator && typeof simulator.machine_name === "string") {
        const local = scienceStage.value.cqlib_local as JsonObject;
        const circuits = [
          { purpose: "representative_qgnn_subcircuit" as const, detail: local.qgnn as JsonObject },
          { purpose: "representative_financial_qaoa" as const, detail: local.qaoa as JsonObject },
        ];
        let cloudCompleted = 0;
        for (const circuit of circuits) {
          const qcis = String(circuit.detail.qcis ?? "");
          try {
            const validation = await runTianyanJob({
              projectRoot,
              request: asJson({ action: "validate", machine_name: simulator.machine_name, qcis }),
              timeoutSeconds: 120,
            });
            campaignRepository.addTiming(campaignId, "external", validation.durationSeconds);
            if (validation.exitCode !== 0 || validation.stdout.valid !== true) {
              throw new Error(`TianYan rejected ${circuit.purpose} circuit compatibility`);
            }
            const cloud = await runCloudSimulatorJob({
              context,
              purpose: circuit.purpose,
              backend: simulator.machine_name,
              qcis,
              parentArtifact: scienceStage.artifact.sha256,
            });
            if (cloud.completed) cloudCompleted += 1;
          } catch (error) {
            const artifact = await recordBlockingFailure(
              context,
              `tianyan_cloud_${circuit.purpose}`,
              "BACKEND",
              `TianYan cloud simulator did not reach a terminal result for ${circuit.purpose}`,
              { error: redactedError(error), backend: simulator.machine_name, circuitHash: hash(qcis) },
            );
          blockers.push({ category: "BLOCKED_TIANYAN_SIMULATOR", artifactSha256: artifact.sha256 });
            break;
          }
        }
        if (cloudCompleted === circuits.length) highestLevel = advanceLevel(highestLevel, "L3");
      } else {
        const artifact = await recordBlockingFailure(
          context,
          "discover_tianyan_backends",
          "BACKEND",
          "No running compatible TianYan simulator was discovered",
          discoveryStage.value,
        );
        blockers.push({ category: "BLOCKED_TIANYAN_SIMULATOR", artifactSha256: artifact.sha256 });
      }

      const hasRuntimeBound = discoveryStage.value.hardware_runtime_bound_available === true;
      const completedHardwareJobs = campaignRepository.listQuantumJobs(campaignId).filter((job) => (
        job.targetType === "HARDWARE"
        && job.terminalStatus === "COMPLETED"
        && job.rawResultArtifactSha256
      ));
      if (!hasRuntimeBound && completedHardwareJobs.length < P02_AUTHORIZATION.maxHardwareJobs) {
        const artifact = await recordBlockingFailure(
          context,
          "prepare_tianyan_hardware",
          "BACKEND",
          "TianYan discovery did not provide a conservative hardware execution-time bound, so the 600-second limit cannot be proven and no hardware job was submitted.",
          {
            schemaVersion: "qf.hardware-runtime-gate.v1",
            maxJobs: 2,
            maxCumulativeExecutionSeconds: 600,
            runtimeBoundAvailable: false,
            submittedHardwareJobs: 0,
            policy: "fail_closed",
          },
        );
        blockers.push({ category: "BLOCKED_HARDWARE_RUNTIME", artifactSha256: artifact.sha256 });
      }
    }

    const effectiveSeconds = () => {
      const status = campaignRepository.getCampaign(campaignId);
      return status.activeComputeSeconds + status.externalWaitSeconds;
    };
    let seedStart = 20260722;
    let block = 0;
    campaignRepository.setCampaignState(campaignId, "RUNNING", "active_reproducibility", { level: highestLevel });
    while (effectiveSeconds() < campaignRepository.getCampaign(campaignId).minimumRuntimeSeconds) {
        const current = campaignRepository.getCampaign(campaignId);
        if (current.wallClockSeconds >= current.maximumRuntimeSeconds) break;
        const outputPath = path.join(workspaceRoot, "results", `reproducibility-block-${String(block).padStart(3, "0")}.json`);
        await executeJsonStage<JsonObject>({
          context,
          stage: `reproducibility_block_${block}`,
          actionType: "ACTIVE_REPRODUCIBILITY_BLOCK",
          expectedEvidence: "new seed-indexed bootstrap stability digest and bounded active compute record",
          inputIdentity: { scienceSha256: scienceStage.artifact.sha256, seedStart, minimumComputeSeconds: 280 },
          outputPath,
          timing: "active",
          producer: "qf-science.active-reproducibility",
          parentHashes: [scienceStage.artifact.sha256],
          run: async () => {
            const result = await runScienceJob({
              projectRoot,
              request: asJson({
                action: "active_reproducibility_block",
                workspace_root: workspaceRoot,
                input_path: sciencePath,
                output_path: outputPath,
                seed_start: seedStart,
                minimum_compute_seconds: 280,
              }),
              timeoutSeconds: 600,
            });
            if (result.exitCode !== 0 || result.stdout.status !== "COMPLETED") {
              throw new Error(String(result.stdout.message ?? "active reproducibility block failed"));
            }
            return readJson<JsonObject>(outputPath);
          },
        });
        seedStart += 10_000;
        block += 1;
        campaignRepository.checkpoint(campaignId, "active_reproducibility", asJson({
          block,
          effectiveSeconds: effectiveSeconds(),
          highestLevel,
        }));
    }

    campaignRepository.checkpoint(campaignId, "final_audit", asJson({
      highestLevel,
      blockers,
      formalTestSealed: true,
      hardwareJobs: campaignRepository.getCampaign(campaignId).hardwareJobs,
      hardwareExecutionSeconds: campaignRepository.getCampaign(campaignId).hardwareExecutionSeconds,
    }));
    if (blockers.length > 0) {
      const primary = blockers[0]!;
      campaignRepository.setCampaignState(campaignId, "BLOCKED", "final_audit", {
        blockerCategory: primary.category,
        blockerArtifactSha256: primary.artifactSha256,
        level: highestLevel,
      });
    } else if (effectiveSeconds() >= campaignRepository.getCampaign(campaignId).minimumRuntimeSeconds) {
      campaignRepository.setCampaignState(campaignId, "COMPLETED", "final_audit", { level: highestLevel });
    } else {
      const artifact = await recordBlockingFailure(
        context,
        "final_audit",
        "CAMPAIGN",
        "Campaign reached its maximum window before the 150-minute effective runtime gate",
        campaignRepository.getCampaign(campaignId),
      );
      campaignRepository.setCampaignState(campaignId, "BLOCKED", "final_audit", {
        blockerCategory: "BLOCKED_RUNTIME",
        blockerArtifactSha256: artifact.sha256,
        level: highestLevel,
      });
    }
  } finally {
    clearInterval(heartbeat);
    campaignRepository.releaseLease(campaignId, context.workerId);
    workspaceRepository.close();
  }
}

await main();
