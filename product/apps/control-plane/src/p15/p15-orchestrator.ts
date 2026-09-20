import { createHash, randomUUID } from "node:crypto";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import path from "node:path";

import type { JsonObject, JsonValue } from "@q-fintelligence/contracts";

import { readArtifact, storeArtifact } from "../artifact-store.js";
import { runP07QaoaGenerator, runScienceJob, runTianyanJob } from "../campaign/python-runner.js";
import { remapSixQubits } from "../campaign/tianyan176-hardware.js";
import type { WorkspaceRepository } from "../db/repository.js";
import type { P15Repository } from "./p15-repository.js";

function hash(value: string | Uint8Array): string {
  return createHash("sha256").update(value).digest("hex");
}

function json(value: unknown): JsonObject {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("P15 expected a JSON object");
  return value as JsonObject;
}

function array(value: unknown): JsonValue[] {
  if (!Array.isArray(value)) throw new Error("P15 expected a JSON array");
  return value as JsonValue[];
}

function text(row: Record<string, unknown>, key: string): string {
  const value = row[key];
  if (typeof value !== "string") throw new Error(`P15 expected ${key} to be text`);
  return value;
}

function wait(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function atomicJson(filePath: string, value: unknown): Promise<void> {
  await mkdir(path.dirname(filePath), { recursive: true });
  const temporary = `${filePath}.${process.pid}.${randomUUID()}.tmp`;
  await writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, { encoding: "utf8", mode: 0o600 });
  await rename(temporary, filePath);
}

interface CandidateMetric {
  candidateIndex: number;
  circuitHash: string;
  feasibleRate: number;
  optimalHitRate: number;
  bestObservedBitstring: string | null;
  bestObservedObjective: number | null;
}

export function p15BatchWithoutQueryIdsMustRemainQueryOnly(status: string): boolean {
  return status === "COMMITTING" || status === "UNKNOWN";
}

export class P15Orchestrator {
  private readonly running = new Map<string, Promise<void>>();

  constructor(
    private readonly projectRoot: string,
    private readonly artifactRoot: string,
    private readonly repository: WorkspaceRepository,
    private readonly p15: P15Repository,
  ) {}

  start(campaignId: string, approvalId: string): void {
    if (this.running.has(campaignId)) return;
    const execution = this.run(campaignId, approvalId)
      .catch((error: unknown) => this.block(campaignId, error))
      .finally(() => this.running.delete(campaignId));
    this.running.set(campaignId, execution);
  }

  isRunning(campaignId: string): boolean {
    return this.running.has(campaignId);
  }

  private event(campaignId: string, type: "run.started" | "run.heartbeat" | "run.completed" | "run.blocked" | "artifact.created", payload: JsonObject): void {
    const campaign = this.p15.getCampaign(campaignId);
    this.repository.appendEvent({
      conversationId: campaign.conversationId,
      type,
      payload: { ...payload, p15CampaignId: campaignId, formalTestSealed: true },
    });
  }

  private async artifact(campaignId: string, value: Uint8Array, mediaType: string, producer: string, parents: string[] = []): Promise<string> {
    const campaign = this.p15.getCampaign(campaignId);
    const manifest = await storeArtifact({
      root: this.artifactRoot,
      data: value,
      mediaType,
      producer,
      parentHashes: parents,
    });
    const registered = this.repository.registerArtifact(campaign.conversationId, manifest);
    this.event(campaignId, "artifact.created", {
      summary: `${producer} 工件已完成内容寻址登记`,
      sha256: registered.sha256,
      artifactHashes: [registered.sha256],
      mediaType,
    });
    return registered.sha256;
  }

  private async readJsonArtifact(sha256: string): Promise<JsonObject> {
    const manifest = this.repository.getArtifactManifest(sha256);
    const content = await readArtifact({
      root: this.artifactRoot,
      manifest,
      maxBytes: Math.max(manifest.bytes, 8_000_000),
      allowedMediaTypes: [manifest.mediaType],
    });
    return json(JSON.parse(new TextDecoder().decode(content)) as unknown);
  }

  private objectiveTable(exactPortfolios: JsonValue[]): Map<string, number> {
    return new Map(exactPortfolios.map((entry) => {
      const row = json(entry);
      return [String(row.bitstring), Number(row.total_without_penalty)];
    }));
  }

  private extractProbabilities(result: JsonObject): Record<string, number> {
    const raw = result.probability ?? result.probabilities;
    if (typeof raw === "string") {
      return Object.fromEntries(Object.entries(JSON.parse(raw) as Record<string, unknown>).map(([key, value]) => [key, Number(value)]));
    }
    if (raw && typeof raw === "object" && !Array.isArray(raw)) {
      return Object.fromEntries(Object.entries(raw).map(([key, value]) => [key, Number(value)]));
    }
    return {};
  }

  private evaluate(results: JsonObject[], circuitHashes: string[], exact: JsonValue[], optimalBitstring: string): CandidateMetric[] {
    const objectives = this.objectiveTable(exact);
    const providerOptimal = [...optimalBitstring].reverse().join("");
    return results.map((result, candidateIndex) => {
      const probabilities = this.extractProbabilities(result);
      const feasible = Object.entries(probabilities)
        .map(([providerBits, probability]) => ({ bitstring: [...providerBits].reverse().join(""), probability }))
        .filter(({ bitstring }) => objectives.has(bitstring));
      feasible.sort((left, right) =>
        (objectives.get(left.bitstring) ?? Number.POSITIVE_INFINITY) - (objectives.get(right.bitstring) ?? Number.POSITIVE_INFINITY));
      return {
        candidateIndex,
        circuitHash: circuitHashes[candidateIndex] ?? "",
        feasibleRate: feasible.reduce((sum, entry) => sum + entry.probability, 0),
        optimalHitRate: Number(probabilities[providerOptimal] ?? 0),
        bestObservedBitstring: feasible.find((entry) => entry.probability > 0)?.bitstring ?? null,
        bestObservedObjective: feasible.find((entry) => entry.probability > 0)
          ? objectives.get(feasible.find((entry) => entry.probability > 0)!.bitstring) ?? null
          : null,
      };
    });
  }

  private async prepareScience(campaignId: string): Promise<{
    scienceSha256: string;
    quboSha256: string;
    exact: JsonValue[];
    exactBest: JsonObject;
  }> {
    const campaign = this.p15.getCampaign(campaignId);
    const workspace = path.join(this.projectRoot, ".local", "p15", "campaigns", campaignId);
    const inputPath = path.join(workspace, "input", "tushare-six-stock-raw-bundle.json");
    const sciencePath = path.join(workspace, "results", "science-result.json");
    const sources = this.repository.listProjectSourcesWithParseResult(campaign.projectId);
    const source = sources.find((item) =>
      !item.quarantined && item.fileName === "tushare-six-stock-raw-bundle.json");
    if (!source) throw new Error("P15 requires the browser-uploaded tushare-six-stock-raw-bundle.json project source");
    const sourcePath = path.resolve(this.projectRoot, source.relativePath);
    const resolvedRoot = path.resolve(this.projectRoot);
    if (!sourcePath.startsWith(`${resolvedRoot}${path.sep}`)) throw new Error("P15 source path escaped the formal workspace");
    const sourceBytes = await readFile(sourcePath);
    if (hash(sourceBytes) !== source.sha256) throw new Error("P15 uploaded source hash no longer matches");
    await mkdir(path.dirname(inputPath), { recursive: true });
    await writeFile(inputPath, sourceBytes, { flag: "wx", mode: 0o600 }).catch(async (error: NodeJS.ErrnoException) => {
      if (error.code !== "EEXIST") throw error;
      const current = await readFile(inputPath);
      if (hash(current) !== source.sha256) throw new Error("P15 checkpoint source hash mismatch");
    });
    const sourceArtifactSha256 = await this.artifact(
      campaignId,
      sourceBytes,
      "application/vnd.qf.tushare-bundle+json",
      "qf-p15.browser-uploaded-tushare-source",
    );
    const execution = await runScienceJob({
      projectRoot: this.projectRoot,
      request: {
        action: "run_pipeline",
        workspace_root: workspace,
        input_path: inputPath,
        output_path: sciencePath,
        seed: 20260725,
      },
      timeoutSeconds: 3_600,
    });
    if (execution.exitCode !== 0 || execution.stdout.status !== "COMPLETED") {
      throw new Error(`P15 deterministic finance pipeline failed: ${String(execution.stdout.message ?? "unknown")}`);
    }
    const scienceBytes = await readFile(sciencePath);
    const science = json(JSON.parse(scienceBytes.toString("utf8")) as unknown);
    const formal = json(science.formal_test);
    const qubo = json(science.qubo);
    const exact = array(qubo.exact_portfolios);
    if (formal.sealed !== true || formal.test_metrics_emitted !== false || exact.length !== 20) {
      throw new Error("P15 exact pipeline violated the 6 choose 3 or SEALED gate");
    }
    const scienceSha256 = await this.artifact(
      campaignId,
      scienceBytes,
      "application/vnd.qf.p15-science-result+json",
      "qf-p15.classical-exact-enumeration",
      [sourceArtifactSha256],
    );
    const quboBytes = new TextEncoder().encode(`${JSON.stringify({
      schemaVersion: "qf.p15.frozen-qubo.v1",
      qubo,
      sourceSha256: source.sha256,
      scienceSha256,
      formalTestSealed: true,
    }, null, 2)}\n`);
    const quboSha256 = await this.artifact(
      campaignId,
      quboBytes,
      "application/vnd.qf.p15-frozen-qubo+json",
      "qf-p15.frozen-qubo",
      [sourceArtifactSha256, scienceSha256],
    );
    return { scienceSha256, quboSha256, exact, exactBest: json(qubo.exact_best) };
  }

  private async runGeneration(input: {
    campaignId: string;
    generationIndex: number;
    approvalId: string;
    calibrationSnapshotId: string;
    normalizedCalibration: JsonObject;
    rawMachineConfig: JsonObject;
    exact: JsonValue[];
    optimalBitstring: string;
    purpose: "p15_evolution" | "p15_independent_confirmation";
  }): Promise<{ caughtUp: boolean; top5: JsonObject[]; bestObjective: number | null; batchId: string }> {
    const generated = await runP07QaoaGenerator({
      projectRoot: this.projectRoot,
      request: { action: "generate_batch", generation_index: input.generationIndex },
      timeoutSeconds: 900,
    });
    if (generated.exitCode !== 0 || Number(generated.stdout.circuit_count) !== 50) {
      throw new Error("P15 QAOA generation failed to produce exactly 50 unique candidates");
    }
    const circuits = array(generated.stdout.circuits).map(json);
    const mapping = await runTianyanJob({
      projectRoot: this.projectRoot,
      request: {
        action: "noise_aware_mapping",
        machine_name: "tianyan176",
        logical_qubits: 6,
        normalized_snapshot: input.normalizedCalibration,
      },
      timeoutSeconds: 180,
    });
    const physicalQubits = array(json(mapping.stdout.selected).physical_qubits).map(String);
    if (mapping.exitCode !== 0 || physicalQubits.length !== 6) throw new Error("P15 noise-aware mapping failed");
    const generationId = this.p15.createGeneration({
      campaignId: input.campaignId,
      generationIndex: input.generationIndex,
      strategy: input.purpose === "p15_evolution" ? "noise_aware_qaoa_evolution" : "independent_hardware_confirmation",
      calibrationSnapshotId: input.calibrationSnapshotId,
      candidateCount: circuits.length,
      approvalId: input.approvalId,
    });
    const mapped: Array<{ qcis: string; qcisSha256: string; circuitHash: string; manifest: JsonObject }> = [];
    for (const [candidateIndex, candidate] of circuits.entries()) {
      const ir = json(candidate.circuit_ir);
      const manifest = json(candidate.manifest);
      const qcis = remapSixQubits(String(candidate.qcis), physicalQubits);
      const qcisSha256 = hash(qcis);
      const artifactSha256 = await this.artifact(
        input.campaignId,
        new TextEncoder().encode(`${JSON.stringify({
          schemaVersion: "qf.p15.mapped-candidate.v1",
          generationIndex: input.generationIndex,
          candidateIndex,
          circuitIr: ir,
          manifest,
          mapping: { logicalToPhysical: Object.fromEntries(physicalQubits.map((qubit, index) => [String(index), qubit])) },
          qcis,
          qcisSha256,
          calibrationSnapshotId: input.calibrationSnapshotId,
          formalTestSealed: true,
        }, null, 2)}\n`),
        "application/vnd.qf.p15-candidate+json",
        "qf-p15.noise-aware-qcis",
      );
      this.p15.registerCandidate({
        generationId,
        candidateIndex,
        family: String(manifest.family),
        parameters: json(ir.parameters),
        qcis,
        qcisSha256,
        circuitArtifactSha256: artifactSha256,
        mappedQubits: physicalQubits,
        localMetrics: json(manifest.local_preflight),
      });
      mapped.push({ qcis, qcisSha256, circuitHash: String(ir.circuit_hash), manifest });
    }
    if (new Set(mapped.map((candidate) => candidate.qcisSha256)).size !== mapped.length) {
      throw new Error("P15 generation contains duplicate mapped QCIS");
    }
    const validation = await runTianyanJob({
      projectRoot: this.projectRoot,
      request: {
        action: "validate_batch",
        authorization_phase: "P15",
        machine_name: "tianyan176",
        circuits: mapped.map((candidate) => candidate.qcis),
      },
      timeoutSeconds: 900,
    });
    if (validation.exitCode !== 0 || Number(validation.stdout.valid_count) !== mapped.length) {
      throw new Error(`P15 QCIS compatibility ${String(validation.stdout.valid_count ?? 0)}/${mapped.length}`);
    }
    const requestSha256 = hash(JSON.stringify({
      campaignId: input.campaignId,
      generationIndex: input.generationIndex,
      calibrationSnapshotId: input.calibrationSnapshotId,
      purpose: input.purpose,
      shots: 100,
      qcisSha256: mapped.map((candidate) => candidate.qcisSha256),
    }));
    const batchId = this.p15.createBatch({
      campaignId: input.campaignId,
      generationId,
      calibrationSnapshotId: input.calibrationSnapshotId,
      approvalId: input.approvalId,
      leaseKey: `${input.campaignId}:${input.generationIndex}:${requestSha256}`,
      requestSha256,
    });
    let batch = this.p15.getBatch(batchId);
    let queryIds = JSON.parse(text(batch, "query_ids_json")) as string[];
    const recoveredBatchStatus = text(batch, "status");
    if (queryIds.length === 0 && p15BatchWithoutQueryIdsMustRemainQueryOnly(recoveredBatchStatus)) {
      if (recoveredBatchStatus === "COMMITTING") this.p15.updateBatch(batchId, "UNKNOWN");
      this.p15.updateGeneration(generationId, { status: "BLOCKED", batchId });
      throw new Error(
        "P15 submission is UNKNOWN without Query IDs; recovery is query-only and automatic resubmission is forbidden",
      );
    }
    if (queryIds.length === 0) {
      this.p15.updateBatch(batchId, "COMMITTING");
      const approvalHash = hash(JSON.stringify({
        campaignId: input.campaignId,
        approvalId: input.approvalId,
        requestSha256,
        hardware: "tianyan176",
        purpose: input.purpose,
        shots: 100,
        formalTestSealed: true,
      }));
      const submission = await runTianyanJob({
        projectRoot: this.projectRoot,
        request: {
          action: "submit_batch",
          authorization_phase: "P15",
          commit_authorized: true,
          approval_hash: approvalHash,
          machine_name: "tianyan176",
          purpose: input.purpose,
          batch_index: input.generationIndex,
          shots: 100,
          circuits: mapped.map((candidate) => candidate.qcis),
          qcis_sha256: mapped.map((candidate) => candidate.qcisSha256),
        },
        timeoutSeconds: 900,
      });
      if (submission.exitCode !== 0 || !Array.isArray(submission.stdout.query_ids)
        || submission.stdout.query_ids.length !== mapped.length) {
        this.p15.updateBatch(batchId, "UNKNOWN");
        this.p15.updateGeneration(generationId, { status: "BLOCKED", batchId });
        const reason = String(submission.stdout.message ?? "adapter failure")
          .replace(/[\r\n\t]+/gu, " ")
          .slice(0, 280);
        throw new Error(
          `P15 submission entered UNKNOWN; persisted request hash forbids automatic resubmission (${reason})`,
        );
      }
      queryIds = submission.stdout.query_ids.map(String);
      this.p15.updateBatch(batchId, "SUBMITTED", queryIds);
      this.event(input.campaignId, "run.heartbeat", {
        summary: `P15 第 ${input.generationIndex} 代 ${mapped.length} 条唯一线路已提交；Query ID 已先持久化`,
        stage: "hardware_submitted",
        generationIndex: input.generationIndex,
        batchId,
        queryIds,
        requestSha256,
        physicalQubits,
      });
    }
    this.p15.updateGeneration(generationId, { status: "QUERYING", batchId });
    this.p15.updateBatch(batchId, "QUERYING", queryIds);
    for (let attempt = 1; attempt <= 720; attempt += 1) {
      const query = await runTianyanJob({
        projectRoot: this.projectRoot,
        request: {
          action: "query_batch_p15",
          machine_name: "tianyan176",
          query_ids: queryIds,
          machine_config: input.rawMachineConfig,
          max_wait_seconds: 60,
          poll_interval_seconds: 3,
        },
        timeoutSeconds: 150,
      });
      const rawResults = Array.isArray(query.stdout.raw_results) ? query.stdout.raw_results.map(json) : [];
      const correctedResults = Array.isArray(query.stdout.readout_corrected_results)
        ? query.stdout.readout_corrected_results.map(json)
        : [];
      if (query.exitCode === 0 && rawResults.length === mapped.length && correctedResults.length === mapped.length) {
        const rawSha256 = await this.artifact(
          input.campaignId,
          new TextEncoder().encode(`${JSON.stringify({ batchId, queryIds, results: rawResults }, null, 2)}\n`),
          "application/vnd.qf.p15-tianyan-raw-results+json",
          "qf-p15.tianyan176-raw-results",
        );
        const correctedSha256 = await this.artifact(
          input.campaignId,
          new TextEncoder().encode(`${JSON.stringify({
            batchId,
            queryIds,
            results: correctedResults,
            readoutCalibration: true,
            calibrationSnapshotId: input.calibrationSnapshotId,
          }, null, 2)}\n`),
          "application/vnd.qf.p15-tianyan-corrected-results+json",
          "qf-p15.tianyan176-readout-corrected-results",
          [rawSha256],
        );
        const metrics = this.evaluate(correctedResults, mapped.map((candidate) => candidate.circuitHash), input.exact, input.optimalBitstring);
        const ranked = [...metrics].sort((left, right) =>
          right.optimalHitRate - left.optimalHitRate
          || right.feasibleRate - left.feasibleRate
          || left.candidateIndex - right.candidateIndex);
        const top5 = ranked.slice(0, 5).map((metric) => metric as unknown as JsonObject);
        for (const metric of metrics) {
          this.p15.updateCandidateResult({
            generationId,
            candidateIndex: metric.candidateIndex,
            queryId: queryIds[metric.candidateIndex]!,
            rawSha256,
            correctedSha256,
            metrics: metric as unknown as JsonObject,
          });
        }
        const bestObjective = ranked
          .map((metric) => metric.bestObservedObjective)
          .filter((value): value is number => value !== null)
          .sort((left, right) => left - right)[0] ?? null;
        const caughtUp = ranked.some((metric) => metric.optimalHitRate > 0);
        const metricsArtifactSha256 = await this.artifact(
          input.campaignId,
          new TextEncoder().encode(`${JSON.stringify({
            schemaVersion: "qf.p15.generation-metrics.v1",
            generationIndex: input.generationIndex,
            purpose: input.purpose,
            candidateCount: mapped.length,
            top5,
            caughtUpWithExactBest: caughtUp,
            exactBestBitstring: input.optimalBitstring,
            bestObservedObjective: bestObjective,
            rawResultsSha256: rawSha256,
            correctedResultsSha256: correctedSha256,
            superiorityClaim: false,
            formalTestSealed: true,
          }, null, 2)}\n`),
          "application/vnd.qf.p15-generation-metrics+json",
          "qf-p15.generation-metrics",
          [rawSha256, correctedSha256],
        );
        this.p15.updateGeneration(generationId, {
          status: "COMPLETED",
          top5,
          metrics: {
            caughtUpWithExactBest: caughtUp,
            bestObservedObjective: bestObjective,
            metricsArtifactSha256,
            rawResultsSha256: rawSha256,
            correctedResultsSha256: correctedSha256,
          },
          batchId,
          completed: true,
        });
        this.p15.updateBatch(batchId, "COMPLETED", queryIds, true);
        this.event(input.campaignId, "run.heartbeat", {
          summary: `P15 第 ${input.generationIndex} 代 50/50 真机结果完成，Top 5 已保留`,
          stage: "generation_completed",
          generationIndex: input.generationIndex,
          batchId,
          caughtUpWithExactBest: caughtUp,
          bestObservedObjective: bestObjective,
          metricsArtifactSha256,
          queryIdCount: queryIds.length,
          resubmitted: false,
        });
        return { caughtUp, top5, bestObjective, batchId };
      }
      this.event(input.campaignId, "run.heartbeat", {
        summary: `继续查询第 ${input.generationIndex} 代同一组 Query ID（${attempt}/720）`,
        stage: "hardware_querying",
        generationIndex: input.generationIndex,
        queryIdCount: queryIds.length,
        rawResultCount: rawResults.length,
        correctedResultCount: correctedResults.length,
        resubmitted: false,
      });
      await wait(Math.min(30_000, 3_000 + attempt * 1_000));
      batch = this.p15.getBatch(batchId);
      if (text(batch, "status") === "UNKNOWN") throw new Error("P15 batch entered UNKNOWN; only manual query recovery is allowed");
    }
    throw new Error("P15 hardware query exceeded the bounded polling window");
  }

  private async run(campaignId: string, approvalId: string): Promise<void> {
    const campaign = this.p15.getCampaign(campaignId);
    if (campaign.status === "COMPLETED") return;
    this.p15.updateCampaign(campaignId, {
      status: "RUNNING",
      stage: "classical_exact_and_qubo_freeze",
      newExternalCallsAllowed: true,
      reopened: true,
      error: null,
    });
    this.event(campaignId, "run.started", {
      summary: "P15 正式 Campaign 已启动：经典精确枚举与 QUBO 冻结先行，正式测试保持 SEALED",
      stage: "classical_exact_and_qubo_freeze",
      provider: campaign.provider,
      model: campaign.modelId,
    });
    const science = await this.prepareScience(campaignId);
    const targetObjective = Number(science.exactBest.total_without_penalty);
    const optimalBitstring = String(science.exactBest.bitstring);
    this.p15.updateCampaign(campaignId, {
      stage: "qubo_frozen",
      quboSha256: science.quboSha256,
      classicalResultSha256: science.scienceSha256,
      targetObjective,
      checkpoint: {
        quboFrozen: true,
        exactPortfolioCount: science.exact.length,
        optimalBitstring,
        approvalId,
        formalTestSealed: true,
      },
    });
    this.event(campaignId, "run.heartbeat", {
      summary: "QUBO 已冻结；经典精确枚举验证 6 choose 3 = 20，开始持续量子候选生成",
      stage: "qubo_frozen",
      quboSha256: science.quboSha256,
      classicalResultSha256: science.scienceSha256,
      exactPortfolioCount: science.exact.length,
      targetObjective,
      optimalBitstring,
    });
    const calibration = this.repository.getLatestP15Calibration(campaign.projectId);
    if (!calibration || calibration.completeness === "UNAVAILABLE") {
      throw new Error("P15 has no usable tianyan176 calibration snapshot");
    }
    const normalizedCalibration = await this.readJsonArtifact(calibration.normalizedSha256);
    const rawCalibration = await this.readJsonArtifact(calibration.rawSha256);
    const rawMachineConfig = json(rawCalibration.config);
    let generationIndex = Number(campaign.checkpoint.nextGenerationIndex ?? 1);
    let caughtUp = false;
    let confirmationBatches = campaign.confirmationBatches;
    let bestObjective: number | null = campaign.bestQuantumObjective;
    while (!caughtUp) {
      const generation = await this.runGeneration({
        campaignId,
        generationIndex,
        approvalId,
        calibrationSnapshotId: calibration.snapshotId,
        normalizedCalibration: { ...normalizedCalibration, normalized_sha256: calibration.normalizedSha256 },
        rawMachineConfig,
        exact: science.exact,
        optimalBitstring,
        purpose: "p15_evolution",
      });
      caughtUp = generation.caughtUp;
      bestObjective = bestObjective === null ? generation.bestObjective
        : generation.bestObjective === null ? bestObjective : Math.min(bestObjective, generation.bestObjective);
      generationIndex += 1;
      this.p15.updateCampaign(campaignId, {
        stage: caughtUp ? "exact_optimum_matched" : "evolving",
        bestQuantumObjective: bestObjective,
        checkpoint: {
          quboFrozen: true,
          exactPortfolioCount: science.exact.length,
          optimalBitstring,
          nextGenerationIndex: generationIndex,
          caughtUp,
          approvalId,
          formalTestSealed: true,
        },
      });
    }
    while (confirmationBatches < 3) {
      const confirmation = await this.runGeneration({
        campaignId,
        generationIndex,
        approvalId,
        calibrationSnapshotId: calibration.snapshotId,
        normalizedCalibration: { ...normalizedCalibration, normalized_sha256: calibration.normalizedSha256 },
        rawMachineConfig,
        exact: science.exact,
        optimalBitstring,
        purpose: "p15_independent_confirmation",
      });
      if (!confirmation.caughtUp) {
        caughtUp = false;
        this.p15.updateCampaign(campaignId, { stage: "confirmation_missed_resume_evolution" });
        while (!caughtUp) {
          generationIndex += 1;
          const recovery = await this.runGeneration({
            campaignId,
            generationIndex,
            approvalId,
            calibrationSnapshotId: calibration.snapshotId,
            normalizedCalibration: { ...normalizedCalibration, normalized_sha256: calibration.normalizedSha256 },
            rawMachineConfig,
            exact: science.exact,
            optimalBitstring,
            purpose: "p15_evolution",
          });
          caughtUp = recovery.caughtUp;
        }
        confirmationBatches = 0;
      } else {
        confirmationBatches += 1;
      }
      generationIndex += 1;
      this.p15.updateCampaign(campaignId, {
        stage: "independent_confirmation",
        confirmationBatches,
        bestQuantumObjective: targetObjective,
        checkpoint: {
          quboFrozen: true,
          exactPortfolioCount: science.exact.length,
          optimalBitstring,
          nextGenerationIndex: generationIndex,
          caughtUp: true,
          confirmationBatches,
          approvalId,
          formalTestSealed: true,
        },
      });
    }
    const finalRecord: JsonObject = {
      schemaVersion: "qf.p15.final-result.v1",
      campaignId,
      status: "COMPLETED",
      provider: campaign.provider,
      model: campaign.modelId,
      quboSha256: science.quboSha256,
      classicalResultSha256: science.scienceSha256,
      exactPortfolioCount: science.exact.length,
      exactBest: science.exactBest,
      bestQuantumObjective: targetObjective,
      independentHardwareConfirmations: confirmationBatches,
      nextGenerationIndex: generationIndex,
      calibrationSnapshotId: calibration.snapshotId,
      readoutCalibration: true,
      formalTestSealed: true,
      superiorityClaim: false,
      scientificConclusion: "The hardware samples reproduced the classical exact-best bitstring for this sealed six-stock instance; this is not evidence of general quantum advantage.",
    };
    const finalSha256 = await this.artifact(
      campaignId,
      new TextEncoder().encode(`${JSON.stringify(finalRecord, null, 2)}\n`),
      "application/vnd.qf.p15-final-result+json",
      "qf-p15.final-result",
      [science.quboSha256, science.scienceSha256],
    );
    this.p15.updateCampaign(campaignId, {
      status: "COMPLETED",
      stage: "completed",
      bestCandidateSha256: finalSha256,
      targetObjective,
      bestQuantumObjective: targetObjective,
      confirmationBatches,
      newExternalCallsAllowed: false,
      checkpoint: { ...finalRecord, finalArtifactSha256: finalSha256 },
      completed: true,
    });
    this.event(campaignId, "run.completed", {
      summary: "P15 COMPLETED：量子样本追平经典精确最优，并完成 3 个独立真机确认批次",
      stage: "completed",
      finalArtifactSha256: finalSha256,
      confirmationBatches,
      superiorityClaim: false,
    });
  }

  private block(campaignId: string, error: unknown): void {
    const message = error instanceof Error ? error.message : String(error);
    const current = this.p15.getCampaign(campaignId);
    const status: "BLOCKED" | "FAILED" = /TIANYAN|provider|network|timeout|UNKNOWN|calibration|query/iu.test(message)
      ? "BLOCKED"
      : "FAILED";
    this.p15.updateCampaign(campaignId, {
      status,
      stage: "terminal_blocked",
      newExternalCallsAllowed: false,
      error: { code: "P15_ORCHESTRATOR_STOPPED", message, retryable: status === "BLOCKED" },
      checkpoint: { ...current.checkpoint, stoppedAt: new Date().toISOString(), formalTestSealed: true },
      completed: true,
    });
    this.event(campaignId, "run.blocked", {
      summary: `P15 ${status}：${message}`,
      stage: "terminal_blocked",
      code: "P15_ORCHESTRATOR_STOPPED",
      retryable: status === "BLOCKED",
    });
  }
}
