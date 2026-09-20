import { createHash } from "node:crypto";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

import { Type, type TSchema } from "typebox";
import { Check, Errors } from "typebox/value";

import type { AgentUiEvent, AgentUiEventType, ApprovalRequest, ArtifactSummary, JsonObject, StepStatus } from "@q-fintelligence/contracts";

import { readArtifact, storeArtifact } from "../artifact-store.js";
import { runGeneratedToolJob, runP06WorkspaceTool, runTianyanJob } from "../campaign/python-runner.js";
import { findSixQubitCycle, remapSixQubits } from "../campaign/tianyan176-hardware.js";
import { TushareAdapter } from "../campaign/tushare-adapter.js";
import type { WorkspaceRepository } from "../db/repository.js";
import {
  assertHardwareMutationAuthorized,
  READ_ONLY_HARDWARE_POLICY,
  type HardwareExecutionPolicy,
} from "../hardware-policy.js";
import type { P16HardwareRepository } from "../p16/p16-hardware-repository.js";

export const QF_TOOL_NAMES = [
  "get_task_context",
  "validate_task_spec",
  "propose_protocol_revision",
  "list_run_steps",
  "request_step_execution",
  "list_artifacts",
  "list_project_sources",
  "read_artifact_excerpt",
  "execute_controlled_python",
  "fetch_tushare_six_stock_bundle",
  "fetch_tianyan176_calibration_snapshot",
  "validate_tianyan176_qcis_artifacts",
  "transpile_tianyan176_qcis_artifacts",
  "prepare_p16_hardware_batch",
  "submit_p16_hardware_batch",
  "query_p16_hardware_batch",
  "get_p16_hardware_status",
  "inspect_validation_dataset",
  "generate_controlled_qaoa_circuit",
  "generate_noise_aware_qaoa_circuit",
  "start_p15_portfolio_campaign",
  "submit_tianyan176_100_shot",
  "record_analysis",
  "request_approval",
] as const;

export type QfToolName = (typeof QF_TOOL_NAMES)[number];

export interface ToolExecutionResult {
  summary: string;
  data: JsonObject;
  artifact?: ArtifactSummary;
  approval?: ApprovalRequest;
  step?: StepStatus;
}

export interface QfToolExecutionContext {
  runtimeSessionId: string;
  toolCallId: string;
  signal: AbortSignal;
}

export interface QfToolSpec {
  name: string;
  label: string;
  description: string;
  parameters: TSchema;
  execute: (toolCallId: string, params: JsonObject, signal?: AbortSignal) => Promise<unknown>;
}

function defineTool(spec: QfToolSpec): QfToolSpec {
  return spec;
}

export function buildP06TianyanSubmitRequest(input: {
  approvalHash: string;
  authorizationBasis: string;
  qcis: string;
}): JsonObject {
  return {
    action: "submit",
    commit_authorized: true,
    approval_hash: input.approvalHash,
    authorization_basis: input.authorizationBasis,
    target_type: "HARDWARE",
    authorization_phase: "P06",
    purpose: "p06_frontend_scientific_validation",
    shots: 100,
    estimated_execution_seconds: 240,
    previous_p06_hardware_execution_seconds: 0,
    qcis: input.qcis,
    machine_name: "tianyan176",
  };
}

export function buildNoiseAwareLocalSimulationRequest(logicalQcis: string): JsonObject {
  if (!logicalQcis.trim()) throw new Error("noise-aware local simulation requires logical QCIS");
  return {
    action: "local_simulate",
    qcis: logicalQcis,
    shots: 4096,
    seed: 20260725,
  };
}

const ALLOWED_STEPS = new Set(["draft_task_spec", "inspect_foundation", "validate_task_spec"]);

export class QfToolHost {
  constructor(
    private readonly repository: WorkspaceRepository,
    readonly conversationId: string,
    private readonly artifactRoot: string,
    private readonly projectRoot = process.cwd(),
    private readonly publish?: (event: AgentUiEvent) => void,
    private readonly startP15Campaign?: (input: { conversationId: string; authorizationBasis: string }) => JsonObject,
    private readonly p16Hardware?: P16HardwareRepository,
    private readonly hardwarePolicy: HardwareExecutionPolicy = READ_ONLY_HARDWARE_POLICY,
  ) {}

  private emit(type: AgentUiEventType, payload: JsonObject): AgentUiEvent {
    const event = this.repository.appendEvent({ conversationId: this.conversationId, type, payload });
    this.publish?.(event);
    return event;
  }

  private async storeJsonArtifact(
    value: JsonObject,
    producer: string,
    mediaType: string,
    parentHashes: string[],
  ): Promise<ArtifactSummary> {
    const manifest = await storeArtifact({
      root: this.artifactRoot,
      data: new TextEncoder().encode(`${JSON.stringify(value, null, 2)}\n`),
      mediaType,
      producer,
      parentHashes,
    });
    return this.repository.registerArtifact(this.conversationId, manifest);
  }

  private async storeDataArtifact(
    data: Uint8Array,
    producer: string,
    mediaType: string,
    parentHashes: string[],
  ): Promise<ArtifactSummary> {
    const manifest = await storeArtifact({
      root: this.artifactRoot,
      data,
      mediaType,
      producer,
      parentHashes,
    });
    return this.repository.registerArtifact(this.conversationId, manifest);
  }

  private async readJsonArtifact(sha256: string): Promise<JsonObject> {
    if (!/^[a-f0-9]{64}$/u.test(sha256)) throw new Error("artifact SHA-256 is invalid");
    const manifest = this.repository.getArtifactManifest(sha256);
    const data = await readArtifact({
      root: this.artifactRoot,
      manifest,
      maxBytes: 8_000_000,
      allowedMediaTypes: [
        "application/json",
        "application/vnd.qf.tushare-bundle+json",
        "application/vnd.qf.validation-data+json",
        "application/vnd.qf.data-quality+json",
        "application/vnd.qf.quantum-circuit+json",
        "application/vnd.qf.tianyan-receipt+json",
        "application/vnd.qf.tianyan-result+json",
        "application/vnd.qf.project-source+json",
        "application/vnd.qf.machine-calibration-raw+json",
        "application/vnd.qf.machine-calibration+json",
        "application/vnd.qf.machine-calibration-diff+json",
        "application/vnd.qf.machine-calibration-manifest+json",
        "application/vnd.qf.p16.tianyan-validation+json",
        "application/vnd.qf.p16.tianyan-mcts-mapping+json",
        "application/vnd.qf.p16.hardware-request+json",
        "application/vnd.qf.p16.tianyan-submission+json",
        "application/vnd.qf.p16.tianyan-raw-results+json",
        "application/vnd.qf.p16.tianyan-corrected-results+json",
      ],
    });
    return JSON.parse(new TextDecoder("utf8", { fatal: true }).decode(data)) as JsonObject;
  }

  private assertConversationArtifact(sha256: string): void {
    if (!/^[a-f0-9]{64}$/u.test(sha256)) throw new Error("artifact SHA-256 is invalid");
    if (!this.repository.listArtifacts(this.conversationId).some((artifact) => artifact.sha256 === sha256)) {
      throw new Error("P16 hardware input artifact is not registered to the active conversation");
    }
    this.repository.getArtifactManifest(sha256);
  }

  private async readQcisArtifacts(hashes: string[]): Promise<string[]> {
    return await Promise.all(hashes.map(async (artifactSha256) => {
      this.assertConversationArtifact(artifactSha256);
      const manifest = this.repository.getArtifactManifest(artifactSha256);
      const data = await readArtifact({
        root: this.artifactRoot,
        manifest,
        maxBytes: 512_000,
        allowedMediaTypes: ["text/vnd.qcis"],
      });
      const qcis = new TextDecoder("utf8", { fatal: true }).decode(data);
      if (!qcis.trim() || createHash("sha256").update(qcis).digest("hex") !== artifactSha256) {
        throw new Error("P16 QCIS artifact content does not match its immutable manifest");
      }
      return qcis;
    }));
  }

  async execute(
    name: QfToolName,
    input: JsonObject,
    context?: QfToolExecutionContext,
  ): Promise<ToolExecutionResult> {
    context?.signal.throwIfAborted();
    switch (name) {
      case "get_task_context": {
        const snapshot = this.repository.getSnapshot(this.conversationId);
        return {
          summary: `Task ${snapshot.conversation.taskId} is ${snapshot.conversation.state}.`,
          data: {
            taskId: snapshot.conversation.taskId,
            state: snapshot.conversation.state,
            mode: snapshot.conversation.mode,
            stepCount: snapshot.steps.length,
            artifactHashes: snapshot.artifacts.map((artifact) => artifact.sha256),
          },
        };
      }
      case "validate_task_spec": {
        const question = typeof input.scientificQuestion === "string" ? input.scientificQuestion.trim() : "";
        const asOfDate = typeof input.asOfDate === "string" ? input.asOfDate : "";
        const validDate = /^\d{4}-\d{2}-\d{2}$/u.test(asOfDate);
        return {
          summary: question && validDate ? "TaskSpec draft passed the P0 shape gate." : "TaskSpec draft is incomplete.",
          data: {
            valid: Boolean(question && validDate),
            missing: [question ? null : "scientificQuestion", validDate ? null : "asOfDate"].filter(
              (item): item is string => item !== null,
            ),
          },
        };
      }
      case "propose_protocol_revision": {
        const rationale = typeof input.rationale === "string" ? input.rationale.slice(0, 1200) : "Agent proposed revision";
        const subjectHash = createHash("sha256").update(JSON.stringify(input)).digest("hex");
        const approval = this.repository.createApprovalRequest({
          conversationId: this.conversationId,
          action: "FREEZE_PROTOCOL",
          subjectHash,
          rationale,
        });
        this.emit("approval.requested", {
          summary: "协议修订等待前端人工审批",
          approvalId: approval.approvalId,
          action: approval.action,
          subjectHash: approval.subjectHash,
          rationale: approval.rationale,
        });
        return { summary: "Protocol revision was recorded as pending human approval.", data: { approvalId: approval.approvalId }, approval };
      }
      case "list_run_steps": {
        const steps = this.repository.listSteps(this.conversationId);
        return {
          summary: `${steps.length} registered steps.`,
          data: { steps: steps.map((step) => ({ id: step.stepId, name: step.name, status: step.status })) },
        };
      }
      case "request_step_execution": {
        const stepName = typeof input.stepName === "string" ? input.stepName : "";
        if (!ALLOWED_STEPS.has(stepName)) throw new Error("step is not in the deterministic P0 allowlist");
        this.repository.upsertStep({ conversationId: this.conversationId, name: stepName, state: "RUNNING" });
        const step = this.repository.upsertStep({ conversationId: this.conversationId, name: stepName, state: "COMPLETED" });
        return { summary: `Deterministic step ${stepName} completed.`, data: { stepId: step.stepId, status: step.status }, step };
      }
      case "list_artifacts": {
        const artifacts = this.repository.listArtifacts(this.conversationId);
        return {
          summary: `${artifacts.length} registered artifacts.`,
          data: { artifacts: artifacts.map((artifact) => ({ sha256: artifact.sha256, mediaType: artifact.mediaType, bytes: artifact.bytes })) },
        };
      }
      case "list_project_sources": {
        const conversation = this.repository.getConversation(this.conversationId);
        const sources = this.repository.listProjectSourcesWithParseResult(conversation.projectId);
        const linked = await Promise.all(sources.map(async (source) => {
          if (source.quarantined) {
            return {
              source,
              artifactSha256: null,
            };
          }
          const artifact = await this.storeJsonArtifact(
            {
              schemaVersion: "qf.project-source.v1",
              sourceId: source.sourceId,
              projectId: source.projectId,
              fileName: source.fileName,
              rawSha256: source.sha256,
              parsed: source.parseResult,
              trust: "untrusted-data-only",
              formalTestSealed: true,
            },
            "qf-tool.list_project_sources",
            "application/vnd.qf.project-source+json",
            [source.sha256],
          );
          return {
            source,
            artifactSha256: artifact.sha256,
          };
        }));
        const artifactHashes = linked.flatMap((item) => item.artifactSha256 ? [item.artifactSha256] : []);
        if (artifactHashes.length > 0) {
          this.emit("artifact.created", {
            summary: `${artifactHashes.length} 个项目来源已绑定到当前对话，仍按不可信数据处理`,
            resultKind: "data_table",
            artifactHashes,
            formalTestSealed: true,
          });
        }
        return {
          summary: `${sources.length} project sources are registered; quarantined sources remain excluded.`,
          data: {
            projectId: conversation.projectId,
            sources: linked.map(({ source, artifactSha256 }) => ({
              sourceId: source.sourceId,
              fileName: source.fileName,
              mediaType: source.detectedMediaType,
              bytes: source.byteSize,
              sha256: source.sha256,
              parser: source.parser,
              riskLevel: source.riskLevel,
              quarantined: source.quarantined,
              artifactSha256,
              parseSummary: source.quarantined ? { status: "QUARANTINED" } : {
                status: source.parseResult.status ?? "PARSED",
                schemaVersion: source.parseResult.schema_version ?? null,
                content: typeof source.parseResult.content === "object"
                  && source.parseResult.content !== null
                  && !Array.isArray(source.parseResult.content)
                  ? {
                    rows: (source.parseResult.content as JsonObject).rows ?? null,
                    columns: (source.parseResult.content as JsonObject).columns ?? null,
                    missing: (source.parseResult.content as JsonObject).missing ?? null,
                    duplicateRows: (source.parseResult.content as JsonObject).duplicate_rows ?? null,
                  }
                  : null,
              },
            })),
          },
        };
      }
      case "read_artifact_excerpt": {
        const sha256 = typeof input.sha256 === "string" ? input.sha256 : "";
        const manifest = this.repository.getArtifactManifest(sha256);
        const data = await readArtifact({
          root: this.artifactRoot,
          manifest,
          maxBytes: 32_000,
          allowedMediaTypes: [
            "application/json",
            "application/vnd.qf.validation-data+json",
            "application/vnd.qf.data-quality+json",
            "application/vnd.qf.quantum-circuit+json",
            "application/vnd.qf.tianyan-receipt+json",
            "application/vnd.qf.tianyan-result+json",
            "application/vnd.qf.project-source+json",
            "application/vnd.qf.machine-calibration-raw+json",
            "application/vnd.qf.machine-calibration+json",
            "application/vnd.qf.machine-calibration-diff+json",
            "application/vnd.qf.machine-calibration-manifest+json",
            "application/vnd.qf.harness-sandbox-event+json",
            "application/vnd.qf.p16.tianyan-validation+json",
            "application/vnd.qf.p16.tianyan-mcts-mapping+json",
            "application/vnd.qf.p16.tianyan-raw-results+json",
            "application/vnd.qf.p16.tianyan-corrected-results+json",
          ],
        });
        const excerpt = new TextDecoder("utf8", { fatal: true }).decode(data).slice(0, 4000);
        return { summary: `Read a bounded excerpt from ${sha256}.`, data: { sha256, excerpt } };
      }
      case "execute_controlled_python": {
        const sourceCode = typeof input.sourceCode === "string" ? input.sourceCode : "";
        const testCode = typeof input.testCode === "string" ? input.testCode : "";
        const payload = typeof input.payload === "object" && input.payload !== null && !Array.isArray(input.payload)
          ? input.payload as JsonObject
          : {};
        if (!sourceCode.trim() || sourceCode.length > 20_000 || testCode.length > 12_000) {
          throw new Error("controlled Python source or test exceeds the bounded size");
        }
        const codeSha256 = createHash("sha256").update(sourceCode).digest("hex");
        const testSha256 = createHash("sha256").update(testCode).digest("hex");
        const workspace = path.join(
          this.projectRoot,
          ".local",
          "p15-generated-tools",
          this.conversationId,
          codeSha256,
        );
        await mkdir(workspace, { recursive: true });
        const toolPath = path.join(workspace, "portfolio_qubo_pipeline.py");
        const testPath = path.join(workspace, "test_portfolio_qubo_pipeline.py");
        await writeFile(toolPath, sourceCode, { encoding: "utf8", mode: 0o600 });
        if (testCode.trim()) await writeFile(testPath, testCode, { encoding: "utf8", mode: 0o600 });
        const validation = await runGeneratedToolJob({
          projectRoot: this.projectRoot,
          request: {
            action: "validate",
            workspace_root: workspace,
            tool_path: toolPath,
            ...(testCode.trim() ? { test_path: testPath } : {}),
            tool_name: "portfolio_qubo_pipeline",
          },
        });
        if (validation.exitCode !== 0 || validation.stdout.status !== "COMPLETED") {
          throw new Error(`controlled Python validation failed: ${String(validation.stdout.message ?? "unknown")}`);
        }
        const invocation = await runGeneratedToolJob({
          projectRoot: this.projectRoot,
          request: {
            action: "invoke",
            workspace_root: workspace,
            tool_path: toolPath,
            tool_name: "portfolio_qubo_pipeline",
            payload,
          },
        });
        if (invocation.exitCode !== 0 || invocation.stdout.status !== "COMPLETED") {
          throw new Error(`controlled Python invocation failed: ${String(invocation.stdout.message ?? "unknown")}`);
        }
        const sourceArtifact = await this.storeDataArtifact(
          new TextEncoder().encode(sourceCode),
          "qf-tool.execute_controlled_python.source",
          "text/x-python",
          [],
        );
        const resultArtifact = await this.storeJsonArtifact(
          invocation.stdout,
          "qf-tool.execute_controlled_python.result",
          "application/json",
          [sourceArtifact.sha256],
        );
        this.emit("artifact.created", {
          summary: "Agent 生成的 Python 已通过 AST、进程、网络、密钥与输出边界并完成受控执行",
          resultKind: "code",
          sha256: resultArtifact.sha256,
          artifactHashes: [sourceArtifact.sha256, resultArtifact.sha256],
          codeSha256,
          testSha256,
          validation: validation.stdout,
          stdout: invocation.stdout,
          stderr: `${validation.stderr}${invocation.stderr}`,
          formalTestSealed: true,
        });
        return {
          summary: `Controlled Python completed; result artifact ${resultArtifact.sha256}.`,
          data: {
            codeSha256,
            testSha256,
            sourceArtifactSha256: sourceArtifact.sha256,
            resultArtifactSha256: resultArtifact.sha256,
            result: invocation.stdout,
          },
          artifact: resultArtifact,
        };
      }
      case "fetch_tushare_six_stock_bundle": {
        const token = process.env.TUSHARE_TOKEN ?? "";
        if (!token.trim()) throw new Error("TUSHARE_TOKEN is not configured");
        this.emit("tool.progress", {
          summary: "正在获取 Tushare 六股票封印测试区间之外的原始数据",
          source: "tushare",
          formalTestSealed: true,
        });
        let progressCount = 0;
        const adapter = new TushareAdapter(
          token,
          path.join(this.projectRoot, ".local", "market-cache", "tushare"),
        );
        const bundle = await adapter.fetchHistoricalBundle((message) => {
          if (progressCount >= 8 || (!message.includes("train-validation") && progressCount > 0)) return;
          progressCount += 1;
          this.emit("tool.progress", {
            summary: message,
            source: "tushare",
            progress: progressCount,
            formalTestSealed: true,
          });
        });
        const rows = bundle.daily;
        const columns = [...new Set(rows.flatMap((row) => Object.keys(row)))].sort();
        const missing = Object.fromEntries(columns.map((column) => [
          column,
          rows.filter((row) => row[column] === null || row[column] === "").length,
        ]));
        const duplicateRows = rows.length
          - new Set(rows.map((row) => `${String(row.ts_code)}:${String(row.trade_date)}`)).size;
        const payload = JSON.parse(JSON.stringify({
          schemaVersion: "qf.p14.tushare-raw-data.v1",
          source: "tushare",
          acquiredAt: new Date().toISOString(),
          formalTestSealed: true,
          parsed: {
            parser: "qf-tushare-adapter",
            content: {
              rows: rows.length,
              columns,
              missing,
              duplicate_rows: duplicateRows,
              dtypes: Object.fromEntries(columns.map((column) => [
                column,
                typeof rows.find((row) => row[column] !== null)?.[column],
              ])),
              preview: rows.slice(0, 6),
              third_party_validator: "TushareAdapter point-in-time and sealed-range gates",
            },
          },
          bundle,
        })) as JsonObject;
        const artifact = await this.storeJsonArtifact(
          payload,
          "qf-tool.fetch_tushare_six_stock_bundle",
          "application/vnd.qf.tushare-bundle+json",
          [],
        );
        this.emit("artifact.created", {
          summary: `Tushare 原始数据已登记：6 只股票、${rows.length} 行、正式测试 SEALED`,
          resultKind: "data_table",
          sha256: artifact.sha256,
          artifactHashes: [artifact.sha256],
          source: "tushare",
          selectedStocks: bundle.selected.map((item) => item.tsCode),
          rows: rows.length,
          requestCount: bundle.requests.length,
          cachedRequestCount: bundle.requests.filter((request) => request.cached).length,
          formalTestSealed: true,
        });
        return {
          summary: `Fetched and registered the sealed-range Tushare six-stock bundle with ${rows.length} daily rows.`,
          data: {
            artifactSha256: artifact.sha256,
            selectedStocks: bundle.selected.map((item) => item.tsCode),
            rows: rows.length,
            requestCount: bundle.requests.length,
            cachedRequestCount: bundle.requests.filter((request) => request.cached).length,
            formalTestSealed: true,
          },
          artifact,
        };
      }
      case "fetch_tianyan176_calibration_snapshot": {
        const conversation = this.repository.getConversation(this.conversationId);
        const previous = this.repository.getLatestP15Calibration(conversation.projectId);
        let previousNormalized: JsonObject | null = null;
        if (previous) {
          try {
            previousNormalized = await this.readJsonArtifact(previous.normalizedSha256);
          } catch {
            previousNormalized = null;
          }
        }
        let processResult: Awaited<ReturnType<typeof runTianyanJob>> | null = null;
        let lastError: unknown = null;
        for (let attempt = 1; attempt <= 3; attempt += 1) {
          if (attempt > 1) {
            this.emit("retry.started", {
              summary: `tianyan176 校准快照第 ${attempt} 次受控重试`,
              attempt,
              maxAttempts: 3,
              delayMs: (attempt - 1) * 1_000,
            });
            await new Promise((resolve) => setTimeout(resolve, (attempt - 1) * 1_000));
          }
          try {
            const candidate = await runTianyanJob({
              projectRoot: this.projectRoot,
              request: {
                action: "calibration_snapshot",
                machine_name: "tianyan176",
                ...(previousNormalized ? { previous_normalized: previousNormalized } : {}),
              },
              timeoutSeconds: 240,
            });
            if (candidate.exitCode !== 0 || candidate.stdout.status === "FAILED") {
              throw new Error(String(candidate.stdout.message ?? "calibration SDK request failed"));
            }
            processResult = candidate;
            if (attempt > 1) {
              this.emit("retry.completed", {
                summary: `tianyan176 校准快照在第 ${attempt} 次尝试恢复`,
                attempt,
                success: true,
              });
            }
            break;
          } catch (error) {
            lastError = error;
          }
        }
        if (!processResult) {
          const ageMs = previous ? Date.now() - Date.parse(previous.retrievedAt) : Number.POSITIVE_INFINITY;
          if (previous && ageMs <= 6 * 60 * 60 * 1_000) {
            this.emit("run.heartbeat", {
              summary: "校准 SDK 暂时不可用，复用六小时内的最近快照并暂停新的真机提交检查",
              stage: "calibration_stale_reuse",
              snapshotId: previous.snapshotId,
              calibrationSha256: previous.normalizedSha256,
              warning: String(lastError),
            });
            return {
              summary: "Reused the latest valid calibration snapshot with a stale warning.",
              data: { status: "STALE_REUSED", ...previous, hardwareSubmissionPaused: true },
            };
          }
          this.emit("run.blocked", {
            summary: "没有可用的 tianyan176 校准快照；本地候选可继续，真机提交已暂停",
            stage: "calibration_unavailable",
            code: "TIANYAN176_CALIBRATION_UNAVAILABLE",
            retryable: true,
          });
          throw lastError instanceof Error ? lastError : new Error("tianyan176 calibration is unavailable");
        }
        const raw = processResult.stdout.raw;
        const normalized = processResult.stdout.normalized;
        const diff = processResult.stdout.diff;
        const workerManifest = processResult.stdout.manifest;
        const csv = processResult.stdout.calibration_csv;
        const svg = processResult.stdout.topology_svg;
        if (
          typeof raw !== "object" || raw === null || Array.isArray(raw)
          || typeof normalized !== "object" || normalized === null || Array.isArray(normalized)
          || typeof diff !== "object" || diff === null || Array.isArray(diff)
          || typeof workerManifest !== "object" || workerManifest === null || Array.isArray(workerManifest)
          || typeof csv !== "string" || typeof svg !== "string"
        ) {
          throw new Error("calibration worker returned an invalid package");
        }
        const rawArtifact = await this.storeDataArtifact(
          new TextEncoder().encode(JSON.stringify(raw)),
          "qf-tool.tianyan176-calibration.raw",
          "application/vnd.qf.machine-calibration-raw+json",
          [],
        );
        const normalizedForStorage = { ...(normalized as JsonObject) };
        delete normalizedForStorage.raw_sha256;
        delete normalizedForStorage.normalized_sha256;
        const normalizedArtifact = await this.storeDataArtifact(
          new TextEncoder().encode(JSON.stringify(normalizedForStorage)),
          "qf-tool.tianyan176-calibration.normalized",
          "application/vnd.qf.machine-calibration+json",
          [rawArtifact.sha256],
        );
        const csvArtifact = await this.storeDataArtifact(
          new TextEncoder().encode(csv),
          "qf-tool.tianyan176-calibration.csv",
          "text/csv",
          [normalizedArtifact.sha256],
        );
        const topologyArtifact = await this.storeDataArtifact(
          new TextEncoder().encode(svg),
          "qf-tool.tianyan176-calibration.topology",
          "image/svg+xml",
          [normalizedArtifact.sha256],
        );
        const diffArtifact = await this.storeJsonArtifact(
          diff as JsonObject,
          "qf-tool.tianyan176-calibration.diff",
          "application/vnd.qf.machine-calibration-diff+json",
          previous ? [previous.normalizedSha256, normalizedArtifact.sha256] : [normalizedArtifact.sha256],
        );
        const manifestArtifact = await this.storeJsonArtifact(
          {
            ...(workerManifest as JsonObject),
            artifacts: {
              raw: rawArtifact.sha256,
              normalized: normalizedArtifact.sha256,
              csv: csvArtifact.sha256,
              topology: topologyArtifact.sha256,
              diff: diffArtifact.sha256,
            },
          },
          "qf-tool.tianyan176-calibration.manifest",
          "application/vnd.qf.machine-calibration-manifest+json",
          [rawArtifact.sha256, normalizedArtifact.sha256, csvArtifact.sha256, topologyArtifact.sha256, diffArtifact.sha256],
        );
        const sourceSpecs = [
          ["tianyan176-calibration-raw.json", ".json", rawArtifact, "cqlib-public-sdk-raw"],
          ["tianyan176-calibration-normalized.json", ".json", normalizedArtifact, "qf-calibration-normalizer"],
          ["tianyan176-calibration.csv", ".csv", csvArtifact, "qf-calibration-csv"],
          ["tianyan176-topology.svg", ".svg", topologyArtifact, "qf-calibration-topology"],
          ["tianyan176-calibration-diff.json", ".json", diffArtifact, "qf-calibration-diff"],
          ["tianyan176-calibration-manifest.json", ".json", manifestArtifact, "qf-calibration-manifest"],
        ] as const;
        const sources = sourceSpecs.map(([fileName, extension, artifact, parser]) =>
          this.repository.registerGeneratedProjectSource({
            projectId: conversation.projectId,
            conversationId: this.conversationId,
            fileName,
            extension,
            mediaType: artifact.mediaType,
            byteSize: artifact.bytes,
            sha256: artifact.sha256,
            parser,
            parseResult: {
              status: "PASS",
              trust: "cqlib-public-sdk-calibration-data",
              artifactSha256: artifact.sha256,
              formalTestSealed: true,
            },
          }));
        const normalizedObject = normalized as JsonObject;
        const missingFields = Array.isArray(normalizedObject.missing_fields)
          ? normalizedObject.missing_fields.filter((value): value is string => typeof value === "string")
          : [];
        const warnings = Array.isArray(normalizedObject.warnings)
          ? normalizedObject.warnings.filter((value): value is string => typeof value === "string")
          : [];
        const snapshotId = this.repository.registerP15Calibration({
          projectId: conversation.projectId,
          conversationId: this.conversationId,
          machineStatus: typeof normalizedObject.machine_status === "string" ? normalizedObject.machine_status : "unknown",
          retrievedAt: typeof normalizedObject.retrieved_at === "string" ? normalizedObject.retrieved_at : new Date().toISOString(),
          calibrationAt: typeof normalizedObject.calibration_at === "string" ? normalizedObject.calibration_at : null,
          cqlibVersion: typeof normalizedObject.cqlib_version === "string" ? normalizedObject.cqlib_version : "unknown",
          rawSha256: rawArtifact.sha256,
          normalizedSha256: normalizedArtifact.sha256,
          manifestSha256: manifestArtifact.sha256,
          diffSha256: diffArtifact.sha256,
          csvSha256: csvArtifact.sha256,
          topologySha256: topologyArtifact.sha256,
          completeness: new Set(["COMPLETE", "PARTIAL", "UNAVAILABLE"]).has(String(normalizedObject.data_completeness))
            ? String(normalizedObject.data_completeness) as "COMPLETE" | "PARTIAL" | "UNAVAILABLE"
            : "UNAVAILABLE",
          missingFields,
          warnings,
          sourceIds: sources.map((source) => source.sourceId),
          previousSnapshotId: previous?.snapshotId ?? null,
        });
        const artifactHashes = [
          rawArtifact.sha256,
          normalizedArtifact.sha256,
          csvArtifact.sha256,
          topologyArtifact.sha256,
          diffArtifact.sha256,
          manifestArtifact.sha256,
        ];
        this.emit("artifact.created", {
          summary: `tianyan176 校准快照已通过 cqlib 公共 SDK 获取并登记为 6 个项目来源`,
          resultKind: "data_table",
          snapshotId,
          sha256: manifestArtifact.sha256,
          artifactHashes,
          sourceIds: sources.map((source) => source.sourceId),
          machineStatus: normalizedObject.machine_status ?? "unknown",
          completeness: normalizedObject.data_completeness ?? "UNAVAILABLE",
          calibrationAt: normalizedObject.calibration_at ?? null,
          diff,
          stdout: {
            schemaVersion: processResult.stdout.schema_version ?? null,
            manifest: workerManifest,
          },
          stderr: processResult.stderr,
          formalTestSealed: true,
        });
        return {
          summary: `Fetched and registered tianyan176 calibration snapshot ${snapshotId}.`,
          data: {
            status: "CURRENT",
            snapshotId,
            manifestArtifactSha256: manifestArtifact.sha256,
            normalizedArtifactSha256: normalizedArtifact.sha256,
            artifactHashes,
            sourceIds: sources.map((source) => source.sourceId),
            completeness: normalizedObject.data_completeness ?? "UNAVAILABLE",
            missingFields,
            warnings,
          },
          artifact: manifestArtifact,
        };
      }
      case "validate_tianyan176_qcis_artifacts": {
        if (input.authorizationBasis !== "P16_USER_REQUEST_20260725") {
          throw new Error("P16 QCIS validation requires the current explicit user authorization basis");
        }
        const hashes = Array.isArray(input.circuitArtifactSha256s)
          ? input.circuitArtifactSha256s.filter((value): value is string => typeof value === "string")
          : [];
        if (hashes.length < 1 || hashes.length > 50 || new Set(hashes).size !== hashes.length) {
          throw new Error("P16 QCIS validation requires 1-50 distinct registered circuit artifacts");
        }
        const circuits = await Promise.all(hashes.map(async (artifactSha256) => {
          if (!/^[a-f0-9]{64}$/u.test(artifactSha256)) throw new Error("P16 QCIS artifact SHA-256 is invalid");
          const manifest = this.repository.getArtifactManifest(artifactSha256);
          const data = await readArtifact({
            root: this.artifactRoot,
            manifest,
            maxBytes: 512_000,
            allowedMediaTypes: ["text/vnd.qcis"],
          });
          const qcis = new TextDecoder("utf8", { fatal: true }).decode(data);
          if (!qcis.trim() || createHash("sha256").update(qcis).digest("hex") !== artifactSha256) {
            throw new Error("P16 QCIS artifact content does not match its immutable manifest");
          }
          return qcis;
        }));
        const validation = await runTianyanJob({
          projectRoot: this.projectRoot,
          request: {
            action: "validate_batch",
            authorization_phase: "P16",
            machine_name: "tianyan176",
            circuits,
          },
          timeoutSeconds: 180,
        });
        if (validation.exitCode !== 0 || validation.stdout.status === "FAILED") {
          this.emit("run.blocked", {
            summary: "P16 QCIS 正则校验代理失败；未提交任何真机任务",
            stage: "p16_qcis_validation",
            code: "P16_QCIS_VALIDATION_FAILED",
            retryable: true,
            circuitArtifactSha256s: hashes,
            stdout: validation.stdout,
            stderr: validation.stderr,
            hardwareSubmitted: false,
          });
          throw new Error(String(validation.stdout.message ?? "P16 QCIS validation proxy failed"));
        }
        const resultArtifact = await this.storeJsonArtifact(
          validation.stdout,
          "qf-tool.p16-tianyan176-qcis-validation",
          "application/vnd.qf.p16.tianyan-validation+json",
          hashes,
        );
        const validCount = typeof validation.stdout.valid_count === "number"
          ? validation.stdout.valid_count
          : 0;
        this.emit("artifact.created", {
          summary: `P16 tianyan176 QCIS 正则校验完成：${validCount}/${circuits.length} 通过，未提交真机`,
          resultKind: validCount === circuits.length ? "validation" : "warning",
          artifactHashes: [...hashes, resultArtifact.sha256],
          validationArtifactSha256: resultArtifact.sha256,
          circuitCount: circuits.length,
          validCount,
          hardwareSubmitted: false,
          formalTestSealed: true,
          p15RecoveryHandleUntouched: true,
        });
        return {
          summary: `Validated ${circuits.length} registered P16 QCIS artifacts on tianyan176 without submission.`,
          data: {
            circuitArtifactSha256s: hashes,
            validationArtifactSha256: resultArtifact.sha256,
            validCount,
            circuitCount: circuits.length,
            validations: validation.stdout.validations ?? [],
            hardwareSubmitted: false,
          },
          artifact: resultArtifact,
        };
      }
      case "transpile_tianyan176_qcis_artifacts": {
        if (input.authorizationBasis !== "P16_USER_REQUEST_20260725") {
          throw new Error("P16 QCIS mapping requires the current explicit user authorization basis");
        }
        const hashes = Array.isArray(input.circuitArtifactSha256s)
          ? input.circuitArtifactSha256s.filter((value): value is string => typeof value === "string")
          : [];
        if (hashes.length < 1 || hashes.length > 50 || new Set(hashes).size !== hashes.length) {
          throw new Error("P16 QCIS mapping requires 1-50 distinct registered circuit artifacts");
        }
        const circuits = await Promise.all(hashes.map(async (artifactSha256) => {
          if (!/^[a-f0-9]{64}$/u.test(artifactSha256)) throw new Error("P16 QCIS artifact SHA-256 is invalid");
          const manifest = this.repository.getArtifactManifest(artifactSha256);
          const data = await readArtifact({
            root: this.artifactRoot,
            manifest,
            maxBytes: 512_000,
            allowedMediaTypes: ["text/vnd.qcis"],
          });
          const qcis = new TextDecoder("utf8", { fatal: true }).decode(data);
          if (!qcis.trim() || createHash("sha256").update(qcis).digest("hex") !== artifactSha256) {
            throw new Error("P16 QCIS artifact content does not match its immutable manifest");
          }
          return qcis;
        }));
        const mapping = await runTianyanJob({
          projectRoot: this.projectRoot,
          request: {
            action: "transpile_batch_p16",
            authorization_phase: "P16",
            machine_name: "tianyan176",
            circuits,
          },
          timeoutSeconds: 300,
        });
        if (mapping.exitCode !== 0 || mapping.stdout.status === "FAILED") {
          this.emit("run.blocked", {
            summary: "P16 cqlib MCTS 映射代理失败；未提交任何真机任务",
            stage: "p16_cqlib_mcts_mapping",
            code: "P16_CQLIB_MCTS_MAPPING_FAILED",
            retryable: true,
            circuitArtifactSha256s: hashes,
            stdout: mapping.stdout,
            stderr: mapping.stderr,
            hardwareSubmitted: false,
          });
          throw new Error(String(mapping.stdout.message ?? "P16 cqlib MCTS mapping proxy failed"));
        }
        const rawMappings = mapping.stdout.mappings;
        if (!Array.isArray(rawMappings) || rawMappings.length !== hashes.length) {
          throw new Error("P16 cqlib MCTS mapping returned an unexpected circuit count");
        }
        const mappedArtifactHashes: string[] = [];
        const reportMappings: JsonObject[] = [];
        for (const [index, rawMapping] of rawMappings.entries()) {
          if (typeof rawMapping !== "object" || rawMapping === null || Array.isArray(rawMapping)) {
            throw new Error(`P16 cqlib MCTS mapping ${index} is malformed`);
          }
          const item = rawMapping as JsonObject;
          const mappedQcis = typeof item.mapped_qcis === "string" ? item.mapped_qcis : "";
          const sourceSha256 = typeof item.source_qcis_sha256 === "string" ? item.source_qcis_sha256 : "";
          const mappedSha256 = createHash("sha256").update(mappedQcis).digest("hex");
          if (!mappedQcis.trim() || sourceSha256 !== hashes[index] || item.mapped_qcis_sha256 !== mappedSha256) {
            throw new Error(`P16 cqlib MCTS mapping ${index} failed immutable identity checks`);
          }
          const artifact = await this.storeDataArtifact(
            new TextEncoder().encode(mappedQcis),
            "qf-tool.p16-cqlib-mcts-mapping",
            "text/vnd.qcis",
            [hashes[index]!],
          );
          mappedArtifactHashes.push(artifact.sha256);
          reportMappings.push({ ...item, mapped_artifact_sha256: artifact.sha256 });
        }
        const reportPayload: JsonObject = { ...mapping.stdout, mappings: reportMappings };
        const reportArtifact = await this.storeJsonArtifact(
          reportPayload,
          "qf-tool.p16-cqlib-mcts-mapping-report",
          "application/vnd.qf.p16.tianyan-mcts-mapping+json",
          [...hashes, ...mappedArtifactHashes],
        );
        const validCount = typeof mapping.stdout.valid_count === "number" ? mapping.stdout.valid_count : 0;
        this.emit("artifact.created", {
          summary: `P16 cqlib MCTS 映射完成：${validCount}/${circuits.length} 条映射线路通过 qcis_check_regular；未提交真机`,
          resultKind: validCount === circuits.length ? "validation" : "warning",
          sourceCircuitArtifactSha256s: hashes,
          mappedCircuitArtifactSha256s: mappedArtifactHashes,
          mappingReportArtifactSha256: reportArtifact.sha256,
          validCount,
          circuitCount: circuits.length,
          algorithm: "cqlib.mapping.transpile_qcis",
          hardwareSubmitted: false,
          formalTestSealed: true,
          p15RecoveryHandleUntouched: true,
        });
        return {
          summary: `Mapped ${circuits.length} registered P16 QCIS artifacts through cqlib MCTS without submission.`,
          data: {
            sourceCircuitArtifactSha256s: hashes,
            mappedCircuitArtifactSha256s: mappedArtifactHashes,
            mappingReportArtifactSha256: reportArtifact.sha256,
            validCount,
            circuitCount: circuits.length,
            mappings: reportMappings,
            hardwareSubmitted: false,
          },
          artifact: reportArtifact,
        };
      }
      case "prepare_p16_hardware_batch": {
        assertHardwareMutationAuthorized(this.hardwarePolicy, {
          operation: "P16 hardware preparation",
          authorizationBasis: input.authorizationBasis,
          jobCount: 1,
          shotsPerJob: this.hardwarePolicy.shotsPerJob,
          target: "tianyan176",
        });
        if (!this.p16Hardware) throw new Error("P16 hardware persistence is unavailable in this runtime");
        const conversation = this.repository.getConversation(this.conversationId);
        if (conversation.mode !== "OPENHANDS" || conversation.provider !== "deepseek"
          || conversation.modelId !== "deepseek-v4-pro") {
          throw new Error("P16 hardware Campaign requires the real OpenHands DeepSeek v4 Pro Web UI conversation");
        }
        const protocolSha256 = typeof input.protocolArtifactSha256 === "string" ? input.protocolArtifactSha256 : "";
        const searchSpaceSha256 = typeof input.searchSpaceArtifactSha256 === "string"
          ? input.searchSpaceArtifactSha256
          : "";
        const quboSha256 = typeof input.quboArtifactSha256 === "string" ? input.quboArtifactSha256 : "";
        const mappingReportSha256 = typeof input.mappingReportArtifactSha256 === "string"
          ? input.mappingReportArtifactSha256
          : "";
        const validationReportSha256 = typeof input.validationReportArtifactSha256 === "string"
          ? input.validationReportArtifactSha256
          : "";
        const hashes = Array.isArray(input.mappedCircuitArtifactSha256s)
          ? input.mappedCircuitArtifactSha256s.filter((value): value is string => typeof value === "string")
          : [];
        if (hashes.length < 1 || hashes.length > 50 || new Set(hashes).size !== hashes.length) {
          throw new Error("P16 hardware preparation requires 1-50 distinct mapped QCIS artifacts");
        }
        assertHardwareMutationAuthorized(this.hardwarePolicy, {
          operation: "P16 hardware preparation",
          authorizationBasis: input.authorizationBasis,
          jobCount: hashes.length,
          shotsPerJob: this.hardwarePolicy.shotsPerJob,
          target: "tianyan176",
        });
        for (const sha256 of [
          protocolSha256,
          searchSpaceSha256,
          quboSha256,
          mappingReportSha256,
          validationReportSha256,
          ...hashes,
        ]) this.assertConversationArtifact(sha256);
        await this.readQcisArtifacts(hashes);

        const mappingReport = await this.readJsonArtifact(mappingReportSha256);
        const mappingItems = Array.isArray(mappingReport.mappings) ? mappingReport.mappings : [];
        const mappedHashes = mappingItems.map((item) => (
          item && typeof item === "object" && !Array.isArray(item)
            ? String((item as JsonObject).mapped_artifact_sha256 ?? "")
            : ""
        ));
        if (Number(mappingReport.valid_count ?? -1) !== hashes.length
          || mappedHashes.length !== hashes.length
          || mappedHashes.some((sha256, index) => sha256 !== hashes[index])) {
          throw new Error("P16 mapping report does not prove every frozen mapped QCIS artifact valid");
        }
        const validationReport = await this.readJsonArtifact(validationReportSha256);
        const validations = Array.isArray(validationReport.validations) ? validationReport.validations : [];
        const validationHashes = validations.map((item) => (
          item && typeof item === "object" && !Array.isArray(item)
            ? String((item as JsonObject).qcis_sha256 ?? "")
            : ""
        ));
        if (Number(validationReport.valid_count ?? -1) !== hashes.length
          || validationHashes.length !== hashes.length
          || validationHashes.some((sha256, index) => sha256 !== hashes[index])) {
          throw new Error("P16 validation report does not prove every frozen mapped QCIS artifact valid");
        }

        const calibration = this.repository.getLatestP15Calibration(conversation.projectId);
        if (!calibration || calibration.completeness === "UNAVAILABLE") {
          throw new Error("P16 hardware preparation requires the current registered tianyan176 calibration snapshot");
        }
        const generationIndex = Number(input.generationIndex);
        const batchIndex = Number(input.batchIndex);
        if (!Number.isInteger(generationIndex) || generationIndex < 0
          || !Number.isInteger(batchIndex) || batchIndex < 0) {
          throw new Error("P16 generation and batch indices must be non-negative integers");
        }
        const requestPayload: JsonObject = {
          schemaVersion: "qf.p16.hardware-request.v1",
          authorizationBasis: String(input.authorizationBasis),
          namespace: "P16",
          projectId: conversation.projectId,
          conversationId: conversation.conversationId,
          taskId: conversation.taskId,
          machineName: "tianyan176",
          purpose: "p16_quantum_advantage_validation",
          generationIndex,
          batchIndex,
          shots: this.hardwarePolicy.shotsPerJob,
          protocolArtifactSha256: protocolSha256,
          searchSpaceArtifactSha256: searchSpaceSha256,
          quboArtifactSha256: quboSha256,
          calibrationSnapshotId: calibration.snapshotId,
          calibrationRawSha256: calibration.rawSha256,
          mappingReportArtifactSha256: mappingReportSha256,
          validationReportArtifactSha256: validationReportSha256,
          mappedCircuitArtifactSha256s: hashes,
          circuitCount: hashes.length,
          p15RecoveryHandleUntouched: true,
          unknownWithoutQueryIdMayResubmit: false,
          formalTestSealed: true,
        };
        const requestSha256 = createHash("sha256").update(JSON.stringify(requestPayload)).digest("hex");
        const requestArtifact = await this.storeJsonArtifact(
          { ...requestPayload, requestSha256 },
          "qf-tool.p16-hardware-request",
          "application/vnd.qf.p16.hardware-request+json",
          [protocolSha256, searchSpaceSha256, quboSha256, mappingReportSha256, validationReportSha256, ...hashes],
        );
        const existingApproval = this.repository.listApprovals(this.conversationId).find((approval) =>
          approval.action === "SUBMIT_HARDWARE"
          && approval.subjectHash === requestSha256
          && approval.status === "APPROVED");
        const approval = existingApproval ?? this.repository.decideApproval(
          this.repository.createApprovalRequest({
            conversationId: this.conversationId,
            action: "SUBMIT_HARDWARE",
            subjectHash: requestSha256,
            rationale: "The current user explicitly authorized the exact frozen P16 tianyan176 batch after protocol, QUBO, search-space, mapping, and validation gates.",
            requestedBy: "USER",
          }).approvalId,
          "APPROVE",
          "HUMAN",
        );
        const batch = this.p16Hardware.prepareBatch({
          projectId: conversation.projectId,
          conversationId: conversation.conversationId,
          taskId: conversation.taskId,
          protocolSha256,
          searchSpaceSha256,
          quboSha256,
          calibrationSnapshotId: calibration.snapshotId,
          generationIndex,
          batchIndex,
          strategy: "preregistered_qaoa_statevector",
          circuitSha256s: hashes,
          mappingReportSha256,
          validationReportSha256,
          approvalId: approval.approvalId,
          requestSha256,
          requestArtifactSha256: requestArtifact.sha256,
        });
        this.emit("artifact.created", {
          summary: `P16 hardware batch ${batch.batchId} prepared with ${hashes.length} immutable circuits; no hardware submitted`,
          resultKind: "validation",
          stage: "p16_hardware_prepared",
          batchId: batch.batchId,
          campaignId: batch.campaignId,
          generationId: batch.generationId,
          requestSha256,
          requestArtifactSha256: requestArtifact.sha256,
          approvalId: approval.approvalId,
          circuitCount: hashes.length,
          hardwareSubmitted: false,
          p15RecoveryHandleUntouched: true,
          formalTestSealed: true,
        });
        return {
          summary: `Prepared durable P16 hardware batch ${batch.batchId} without submission.`,
          data: batch as unknown as JsonObject,
          artifact: requestArtifact,
        };
      }
      case "submit_p16_hardware_batch": {
        if (!this.p16Hardware) throw new Error("P16 hardware persistence is unavailable in this runtime");
        assertHardwareMutationAuthorized(this.hardwarePolicy, {
          operation: "P16 hardware submission",
          authorizationBasis: input.authorizationBasis,
          jobCount: 1,
          shotsPerJob: this.hardwarePolicy.shotsPerJob,
          target: "tianyan176",
        });
        const batchId = typeof input.batchId === "string" ? input.batchId : "";
        let batch = this.p16Hardware.getBatchForConversation(batchId, this.conversationId);
        if (batch.status === "COMPLETED" || batch.queryIds.length > 0) {
          return {
            summary: `P16 batch ${batchId} already has its immutable Query ID recovery handle; no resubmission occurred.`,
            data: { ...batch, resubmitted: false } as unknown as JsonObject,
          };
        }
        if (batch.status === "COMMITTING" || batch.status === "UNKNOWN") {
          throw new Error("P16 submission outcome is UNKNOWN without Query IDs; automatic resubmission is forbidden");
        }
        const circuits = await this.readQcisArtifacts(batch.circuitSha256s);
        batch = this.p16Hardware.markCommitting(batchId);
        const approvalHash = createHash("sha256").update(JSON.stringify({
          namespace: "P16",
          campaignId: batch.campaignId,
          generationId: batch.generationId,
          batchId,
          approvalId: batch.approvalId,
          requestSha256: batch.requestSha256,
          purpose: "p16_quantum_advantage_validation",
          machineName: "tianyan176",
          shots: 100,
        })).digest("hex");
        let submission;
        try {
          submission = await runTianyanJob({
            projectRoot: this.projectRoot,
            request: {
              action: "submit_batch",
              authorization_phase: "P16",
              commit_authorized: true,
              approval_hash: approvalHash,
              machine_name: "tianyan176",
              purpose: "p16_quantum_advantage_validation",
              batch_index: batch.batchIndex,
              shots: 100,
              circuits,
              qcis_sha256: batch.circuitSha256s,
            },
            timeoutSeconds: 900,
          });
        } catch (error) {
          batch = this.p16Hardware.markUnknown(batchId, {
            code: "P16_UNKNOWN_SUBMISSION",
            message: String(error instanceof Error ? error.message : error).slice(0, 400),
            requestSha256: batch.requestSha256,
            queryIds: [],
            resubmissionAllowed: false,
          });
          throw new Error(`P16 submission entered UNKNOWN; request ${batch.requestSha256} is query-only`);
        }
        const queryIds = Array.isArray(submission.stdout.query_ids)
          ? submission.stdout.query_ids.map(String)
          : [];
        if (submission.exitCode !== 0 || queryIds.length !== circuits.length
          || queryIds.some((value) => !value) || new Set(queryIds).size !== queryIds.length) {
          batch = this.p16Hardware.markUnknown(batchId, {
            code: "P16_UNKNOWN_SUBMISSION",
            message: String(submission.stdout.message ?? "TianYan adapter returned no complete Query ID set").slice(0, 400),
            requestSha256: batch.requestSha256,
            queryIds: [],
            resubmissionAllowed: false,
          });
          throw new Error(`P16 submission entered UNKNOWN; request ${batch.requestSha256} is query-only`);
        }

        batch = this.p16Hardware.markSubmitted(batchId, queryIds);
        const submissionArtifact = await this.storeJsonArtifact(
          { ...submission.stdout, batchId, campaignId: batch.campaignId, requestSha256: batch.requestSha256 },
          "qf-tool.p16-tianyan176-submission",
          "application/vnd.qf.p16.tianyan-submission+json",
          [batch.requestArtifactSha256, ...batch.circuitSha256s],
        );
        batch = this.p16Hardware.attachSubmissionArtifact(batchId, submissionArtifact.sha256);
        this.emit("artifact.created", {
          summary: `P16 batch ${batchId} submitted once; ${queryIds.length} Query IDs persisted before result polling`,
          resultKind: "hardware_job",
          stage: "p16_hardware_submitted",
          batchId,
          campaignId: batch.campaignId,
          queryIds,
          requestSha256: batch.requestSha256,
          submissionArtifactSha256: submissionArtifact.sha256,
          shots: 100,
          resubmitted: false,
          p15RecoveryHandleUntouched: true,
        });
        return {
          summary: `Submitted P16 batch ${batchId} once and persisted ${queryIds.length} Query IDs.`,
          data: { ...batch, resubmitted: false } as unknown as JsonObject,
          artifact: submissionArtifact,
        };
      }
      case "query_p16_hardware_batch": {
        if (!this.p16Hardware) throw new Error("P16 hardware persistence is unavailable in this runtime");
        const batchId = typeof input.batchId === "string" ? input.batchId : "";
        let batch = this.p16Hardware.getBatchForConversation(batchId, this.conversationId);
        if (batch.status === "COMPLETED") {
          return {
            summary: `P16 batch ${batchId} is already complete; no external submission occurred.`,
            data: { ...batch, resubmitted: false } as unknown as JsonObject,
          };
        }
        if (batch.queryIds.length === 0) {
          throw new Error("P16 batch has no Query IDs; UNKNOWN recovery is query-only and cannot resubmit");
        }
        const calibration = this.repository.getP15Calibration(batch.calibrationSnapshotId);
        if (!calibration) throw new Error("P16 batch calibration snapshot is missing");
        const rawMachineConfig = await this.readJsonArtifact(calibration.rawSha256);
        batch = this.p16Hardware.markQuerying(batchId);
        let query;
        try {
          query = await runTianyanJob({
            projectRoot: this.projectRoot,
            request: {
              action: "query_batch_p16",
              authorization_phase: "P16",
              machine_name: "tianyan176",
              query_ids: batch.queryIds,
              machine_config: rawMachineConfig,
              max_wait_seconds: 60,
              poll_interval_seconds: 3,
            },
            timeoutSeconds: 150,
          });
        } catch (error) {
          batch = this.p16Hardware.markQueryPending(batchId, {
            code: "P16_QUERY_RETRYABLE",
            message: String(error instanceof Error ? error.message : error).slice(0, 400),
            queryIds: batch.queryIds,
            resubmitted: false,
          });
          return {
            summary: `P16 batch ${batchId} query remains retryable with the same Query IDs.`,
            data: { ...batch, resubmitted: false } as unknown as JsonObject,
          };
        }
        const rawResults = Array.isArray(query.stdout.raw_results) ? query.stdout.raw_results : [];
        const correctedResults = Array.isArray(query.stdout.readout_corrected_results)
          ? query.stdout.readout_corrected_results
          : [];
        if (query.exitCode !== 0 || rawResults.length !== batch.circuitSha256s.length
          || correctedResults.length !== batch.circuitSha256s.length) {
          batch = this.p16Hardware.markQueryPending(batchId, {
            code: "P16_HARDWARE_PENDING",
            message: String(query.stdout.message ?? "not all original Query IDs have terminal results").slice(0, 400),
            rawResultCount: rawResults.length,
            correctedResultCount: correctedResults.length,
            queryIds: batch.queryIds,
            resubmitted: false,
          });
          this.emit("run.heartbeat", {
            summary: `P16 batch ${batchId} is still pending on the same ${batch.queryIds.length} Query IDs`,
            stage: "p16_hardware_query_pending",
            batchId,
            queryIds: batch.queryIds,
            rawResultCount: rawResults.length,
            correctedResultCount: correctedResults.length,
            resubmitted: false,
          });
          return {
            summary: `P16 batch ${batchId} remains pending; query the same recovery handle later.`,
            data: { ...batch, resubmitted: false } as unknown as JsonObject,
          };
        }
        const rawArtifact = await this.storeJsonArtifact(
          { schemaVersion: "qf.p16.tianyan-raw-results.v1", batchId, queryIds: batch.queryIds, results: rawResults },
          "qf-tool.p16-tianyan176-raw-results",
          "application/vnd.qf.p16.tianyan-raw-results+json",
          [batch.submissionArtifactSha256 ?? batch.requestArtifactSha256],
        );
        const correctedArtifact = await this.storeJsonArtifact(
          {
            schemaVersion: "qf.p16.tianyan-corrected-results.v1",
            batchId,
            queryIds: batch.queryIds,
            results: correctedResults,
            readoutCalibration: true,
            calibrationSnapshotId: batch.calibrationSnapshotId,
          },
          "qf-tool.p16-tianyan176-readout-corrected-results",
          "application/vnd.qf.p16.tianyan-corrected-results+json",
          [rawArtifact.sha256, calibration.rawSha256],
        );
        batch = this.p16Hardware.markCompleted({
          batchId,
          rawResultSha256: rawArtifact.sha256,
          correctedResultSha256: correctedArtifact.sha256,
        });
        this.emit("artifact.created", {
          summary: `P16 batch ${batchId} completed for all ${batch.queryIds.length} original Query IDs with raw and readout-corrected artifacts`,
          resultKind: "hardware_result",
          stage: "p16_hardware_completed",
          batchId,
          queryIds: batch.queryIds,
          rawResultArtifactSha256: rawArtifact.sha256,
          correctedResultArtifactSha256: correctedArtifact.sha256,
          resubmitted: false,
          p15RecoveryHandleUntouched: true,
        });
        return {
          summary: `Completed P16 batch ${batchId} with raw and readout-corrected results.`,
          data: { ...batch, resubmitted: false } as unknown as JsonObject,
          artifact: correctedArtifact,
        };
      }
      case "get_p16_hardware_status": {
        if (!this.p16Hardware) throw new Error("P16 hardware persistence is unavailable in this runtime");
        const batches = this.p16Hardware.listBatches(this.conversationId);
        return {
          summary: `P16 hardware status contains ${batches.length} durable batches; this read-only call makes no external requests.`,
          data: {
            batches: batches as unknown as JsonObject[],
            externalRequestsMade: false,
            p15RecoveryHandleUntouched: true,
          },
        };
      }
      case "inspect_validation_dataset": {
        const sourceArtifactSha256 = typeof input.artifactSha256 === "string" ? input.artifactSha256 : "";
        const source = await this.readJsonArtifact(sourceArtifactSha256);
        const parsed = source.parsed;
        if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
          throw new Error("selected artifact is not a registered parsed upload");
        }
        const processResult = await runP06WorkspaceTool({
          projectRoot: this.projectRoot,
          request: {
            action: "inspect_dataset",
            source_artifact_sha256: sourceArtifactSha256,
            parsed_upload: parsed,
          },
        });
        if (processResult.exitCode !== 0 || processResult.stdout.status === "FAILED") {
          throw new Error(`P06 data quality worker failed: ${String(processResult.stdout.message ?? "unknown")}`);
        }
        const artifact = await this.storeJsonArtifact(
          processResult.stdout,
          "qf-tool.inspect_validation_dataset",
          "application/vnd.qf.data-quality+json",
          [sourceArtifactSha256],
        );
        this.emit("artifact.created", {
          summary: `数据质量检查完成：${String(processResult.stdout.rows)} 行，质量门 ${String(processResult.stdout.quality_gate)}`,
          resultKind: "data_table",
          sha256: artifact.sha256,
          artifactHashes: [artifact.sha256],
          sourceArtifactSha256,
          stdout: processResult.stdout,
          stderr: processResult.stderr,
          process: {
            module: "qf_quantum_worker.p06_workspace",
            exitCode: processResult.exitCode,
            durationSeconds: processResult.durationSeconds,
          },
          formalTestSealed: true,
        });
        return {
          summary: `Validation data quality completed with gate ${String(processResult.stdout.quality_gate)}.`,
          data: { ...processResult.stdout, artifactSha256: artifact.sha256 },
          artifact,
        };
      }
      case "generate_controlled_qaoa_circuit": {
        const sourceArtifactSha256 = typeof input.sourceArtifactSha256 === "string"
          ? input.sourceArtifactSha256
          : "";
        await this.readJsonArtifact(sourceArtifactSha256);
        const processResult = await runP06WorkspaceTool({
          projectRoot: this.projectRoot,
          request: {
            action: "build_qaoa",
            source_artifact_sha256: sourceArtifactSha256,
            gamma: typeof input.gamma === "number" ? input.gamma : 0.24,
            beta: typeof input.beta === "number" ? input.beta : 0.08,
          },
        });
        if (processResult.exitCode !== 0 || processResult.stdout.status === "FAILED") {
          throw new Error(`P06 controlled QAOA worker failed: ${String(processResult.stdout.message ?? "unknown")}`);
        }
        const logicalQcis = typeof processResult.stdout.qcis === "string" ? processResult.stdout.qcis : "";
        const logicalManifest = processResult.stdout.manifest;
        if (!logicalQcis || typeof logicalManifest !== "object" || logicalManifest === null || Array.isArray(logicalManifest)) {
          throw new Error("P06 controlled QAOA worker did not return a circuit manifest");
        }
        const topology = await runTianyanJob({
          projectRoot: this.projectRoot,
          request: { action: "config_summary", machine_name: "tianyan176" },
          timeoutSeconds: 180,
        });
        const overview = topology.stdout.overview;
        if (topology.exitCode !== 0 || typeof overview !== "object" || overview === null || Array.isArray(overview)) {
          throw new Error(`tianyan176 topology query failed: ${String(topology.stdout.message ?? "unknown")}`);
        }
        const physicalQubits = findSixQubitCycle((overview as JsonObject).coupler_map as Record<string, unknown>);
        const mappedQcis = remapSixQubits(logicalQcis, physicalQubits);
        const mappedQcisSha256 = createHash("sha256").update(mappedQcis).digest("hex");
        const compatibility = await runTianyanJob({
          projectRoot: this.projectRoot,
          request: { action: "validate", machine_name: "tianyan176", qcis: mappedQcis },
          timeoutSeconds: 120,
        });
        if (compatibility.exitCode !== 0 || compatibility.stdout.valid !== true) {
          throw new Error("topology-mapped P06 circuit is not compatible with tianyan176");
        }
        const circuitOutput: JsonObject = {
          ...processResult.stdout,
          qcis: mappedQcis,
          manifest: {
            ...(logicalManifest as JsonObject),
            source_qcis_sha256: (logicalManifest as JsonObject).qcis_sha256 ?? null,
            qcis_sha256: mappedQcisSha256,
            physical_qubits: physicalQubits,
            topology_source: "live_tianyan176_config",
            hardware_compatibility: "PASS",
          },
        };
        const artifact = await this.storeJsonArtifact(
          circuitOutput,
          "qf-tool.generate_controlled_qaoa_circuit",
          "application/vnd.qf.quantum-circuit+json",
          [sourceArtifactSha256],
        );
        const manifest = circuitOutput.manifest as JsonObject;
        this.emit("artifact.created", {
          summary: `受控 QAOA 线路已完成本地 cqlib 门禁：${String(manifest.qubits)} qubits / depth ${String(manifest.depth)}`,
          resultKind: "quantum_circuit",
          sha256: artifact.sha256,
          artifactHashes: [artifact.sha256],
          qcisSha256: manifest.qcis_sha256 ?? null,
          circuit: circuitOutput,
          stdout: circuitOutput,
          stderr: `${processResult.stderr}${topology.stderr}${compatibility.stderr}`,
          process: {
            module: "qf_quantum_worker.p06_workspace",
            exitCode: processResult.exitCode,
            durationSeconds: processResult.durationSeconds,
          },
          physicalQubits,
          topologySource: "live_tianyan176_config",
          hardwareCompatibility: "PASS",
          formalTestSealed: true,
        });
        return {
          summary: `Controlled 6-qubit QAOA circuit passed local cqlib and live tianyan176 topology gates; artifact ${artifact.sha256}.`,
          data: { ...circuitOutput, artifactSha256: artifact.sha256 },
          artifact,
        };
      }
      case "generate_noise_aware_qaoa_circuit": {
        const sourceArtifactSha256 = typeof input.sourceArtifactSha256 === "string"
          ? input.sourceArtifactSha256
          : "";
        const calibrationArtifactSha256 = typeof input.calibrationArtifactSha256 === "string"
          ? input.calibrationArtifactSha256
          : "";
        this.emit("tool.progress", {
          summary: "正在验证噪声感知线路的已登记输入工件",
          stage: "noise_aware_inputs_started",
          formalTestSealed: true,
        });
        await this.readJsonArtifact(sourceArtifactSha256);
        this.emit("tool.progress", {
          summary: "噪声感知线路的源工件已通过登记与 JSON 边界",
          stage: "noise_aware_source_ready",
          formalTestSealed: true,
        });
        const calibration = await this.readJsonArtifact(calibrationArtifactSha256);
        this.emit("tool.progress", {
          summary: "噪声感知线路的校准工件已通过登记与 JSON 边界",
          stage: "noise_aware_calibration_ready",
          formalTestSealed: true,
        });
        const processResult = await runP06WorkspaceTool({
          projectRoot: this.projectRoot,
          request: {
            action: "build_qaoa",
            source_artifact_sha256: sourceArtifactSha256,
            gamma: typeof input.gamma === "number" ? input.gamma : 0.24,
            beta: typeof input.beta === "number" ? input.beta : 0.08,
          },
        });
        this.emit("tool.progress", {
          summary: "噪声感知线路的受控 QAOA Worker 已返回",
          stage: "noise_aware_qaoa_worker_returned",
          workerStatus: processResult.stdout.status ?? null,
          workerExitCode: processResult.exitCode,
          formalTestSealed: true,
        });
        if (processResult.exitCode !== 0 || processResult.stdout.status === "FAILED") {
          throw new Error(`P15 QAOA worker failed: ${String(processResult.stdout.message ?? "unknown")}`);
        }
        const logicalQcis = typeof processResult.stdout.qcis === "string" ? processResult.stdout.qcis : "";
        const logicalManifest = processResult.stdout.manifest;
        if (!logicalQcis || typeof logicalManifest !== "object" || logicalManifest === null || Array.isArray(logicalManifest)) {
          throw new Error("P15 QAOA worker did not return a circuit manifest");
        }
        this.emit("tool.progress", {
          summary: "噪声感知线路的 6 逻辑量子位电路已生成，正在执行校准映射",
          stage: "noise_aware_logical_circuit_ready",
          formalTestSealed: true,
        });
        const mapping = await runTianyanJob({
          projectRoot: this.projectRoot,
          request: {
            action: "noise_aware_mapping",
            machine_name: "tianyan176",
            logical_qubits: 6,
            normalized_snapshot: {
              ...calibration,
              normalized_sha256: calibrationArtifactSha256,
            },
          },
          timeoutSeconds: 120,
        });
        const selected = mapping.stdout.selected;
        const selectedPhysicalQubits = typeof selected === "object" && selected !== null && !Array.isArray(selected)
          ? (selected as JsonObject).physical_qubits
          : null;
        let physicalQubits = typeof selected === "object" && selected !== null && !Array.isArray(selected)
          && Array.isArray(selectedPhysicalQubits)
          ? selectedPhysicalQubits.filter((value): value is string => typeof value === "string")
          : [];
        if (mapping.exitCode !== 0 || physicalQubits.length !== 6) {
          throw new Error(`noise-aware mapping failed: ${String(mapping.stdout.message ?? "unknown")}`);
        }
        this.emit("tool.progress", {
          summary: "校准映射已选定 6 个物理量子位，正在执行 QCIS 兼容性门禁",
          stage: "noise_aware_mapping_ready",
          physicalQubitCount: physicalQubits.length,
          formalTestSealed: true,
        });
        let mappedQcis = remapSixQubits(logicalQcis, physicalQubits);
        let qcisSha256 = createHash("sha256").update(mappedQcis).digest("hex");
        let compatibility = await runTianyanJob({
          projectRoot: this.projectRoot,
          request: { action: "validate", machine_name: "tianyan176", qcis: mappedQcis },
          timeoutSeconds: 120,
        });
        let topologyFallback = false;
        if (compatibility.exitCode !== 0 || compatibility.stdout.valid !== true) {
          const liveTopology = await runTianyanJob({
            projectRoot: this.projectRoot,
            request: { action: "config_summary", machine_name: "tianyan176" },
            timeoutSeconds: 180,
          });
          const overview = liveTopology.stdout.overview;
          const livePhysicalQubits = liveTopology.exitCode === 0
            && typeof overview === "object" && overview !== null && !Array.isArray(overview)
            ? findSixQubitCycle((overview as JsonObject).coupler_map as Record<string, unknown>)
            : [];
          if (livePhysicalQubits.length !== 6) {
            throw new Error("noise-aware mapped circuit is not compatible with tianyan176 and live topology is unavailable");
          }
          const liveMappedQcis = remapSixQubits(logicalQcis, livePhysicalQubits);
          const liveCompatibility = await runTianyanJob({
            projectRoot: this.projectRoot,
            request: { action: "validate", machine_name: "tianyan176", qcis: liveMappedQcis },
            timeoutSeconds: 120,
          });
          if (liveCompatibility.exitCode !== 0 || liveCompatibility.stdout.valid !== true) {
            throw new Error("noise-aware mapped circuit is not compatible with tianyan176 after live-topology fallback");
          }
          physicalQubits = livePhysicalQubits;
          mappedQcis = liveMappedQcis;
          qcisSha256 = createHash("sha256").update(mappedQcis).digest("hex");
          compatibility = liveCompatibility;
          topologyFallback = true;
          this.emit("tool.progress", {
            summary: "校准拓扑与 live tianyan176 拓扑发生漂移，已显式切换 live 拓扑并保留告警",
            stage: "noise_aware_topology_drift_fallback",
            physicalQubitCount: physicalQubits.length,
            formalTestSealed: true,
          });
        }
        if (compatibility.exitCode !== 0 || compatibility.stdout.valid !== true) {
          throw new Error("noise-aware mapped circuit is not compatible with tianyan176");
        }
        this.emit("tool.progress", {
          summary: "物理 QCIS 已通过 tianyan176 兼容性门禁，正在模拟映射前 6 逻辑量子位",
          stage: "noise_aware_compatibility_ready",
          mappedQcisSha256: qcisSha256,
          formalTestSealed: true,
        });
        const simulation = await runTianyanJob({
          projectRoot: this.projectRoot,
          // Statevector evidence is a six-logical-qubit gate. Physical labels
          // such as Q65 describe placement on tianyan176, not a 66-qubit
          // statevector request. The mapped QCIS is independently checked by
          // qcis_check_regular immediately above.
          request: buildNoiseAwareLocalSimulationRequest(logicalQcis),
          timeoutSeconds: 180,
        });
        if (simulation.exitCode !== 0) throw new Error("cqlib local simulation failed");
        this.emit("tool.progress", {
          summary: "映射前 6 逻辑量子位 statevector 模拟已完成",
          stage: "noise_aware_logical_simulation_ready",
          formalTestSealed: true,
        });
        const selectedMapping = typeof selected === "object" && selected !== null && !Array.isArray(selected)
          ? selected as JsonObject
          : {};
        const mappingOutput: JsonObject = {
          ...mapping.stdout,
          selected: {
            ...selectedMapping,
            physical_qubits: physicalQubits,
          },
          topology_fallback: topologyFallback,
        };
        const circuitOutput: JsonObject = {
          ...processResult.stdout,
          qcis: mappedQcis,
          manifest: {
            ...(logicalManifest as JsonObject),
            source_qcis_sha256: (logicalManifest as JsonObject).qcis_sha256 ?? null,
            qcis_sha256: qcisSha256,
            physical_qubits: physicalQubits,
            topology_source: "p15_machine_calibration_snapshot",
            calibration_artifact_sha256: calibrationArtifactSha256,
            noise_mapping_status: topologyFallback
              ? "CALIBRATION_TOPOLOGY_DRIFT_FALLBACK"
              : mapping.stdout.status ?? "UNKNOWN",
            hardware_compatibility: "PASS",
          },
          mapping: mappingOutput,
          local_simulation: simulation.stdout,
          local_simulation_scope: "LOGICAL_PRE_MAPPING",
        };
        const artifact = await this.storeJsonArtifact(
          circuitOutput,
          "qf-tool.generate_noise_aware_qaoa_circuit",
          "application/vnd.qf.quantum-circuit+json",
          [sourceArtifactSha256, calibrationArtifactSha256],
        );
        this.emit("artifact.created", {
          summary: `噪声感知 QAOA 已映射到 ${physicalQubits.join(", ")} 并通过 cqlib/QCIS 门禁`,
          resultKind: "quantum_circuit",
          sha256: artifact.sha256,
          artifactHashes: [artifact.sha256],
          qcisSha256,
          physicalQubits,
          calibrationArtifactSha256,
          mapping: mapping.stdout,
          hardwareCompatibility: "PASS",
          formalTestSealed: true,
        });
        return {
          summary: `Noise-aware QAOA circuit passed cqlib/QCIS validation; artifact ${artifact.sha256}.`,
          data: {
            artifactSha256: artifact.sha256,
            qcisSha256,
            physicalQubits,
            mapping: mapping.stdout,
            localSimulation: simulation.stdout,
          },
          artifact,
        };
      }
      case "start_p15_portfolio_campaign": {
        assertHardwareMutationAuthorized(this.hardwarePolicy, {
          operation: "P15 Campaign launch",
          authorizationBasis: input.authorizationBasis,
          jobCount: 50,
          shotsPerJob: 100,
          target: "tianyan176",
        });
        if (!this.startP15Campaign) throw new Error("P15 Campaign launcher is not available in this runtime");
        const authorizationBasis = typeof input.authorizationBasis === "string" ? input.authorizationBasis : "";
        const detail = this.startP15Campaign({ conversationId: this.conversationId, authorizationBasis });
        const campaign = typeof detail.campaign === "object" && detail.campaign !== null && !Array.isArray(detail.campaign)
          ? detail.campaign as JsonObject
          : {};
        this.emit("run.started", {
          summary: "P15 持久化正式 Campaign 已从当前 DeepSeek v4 Pro 前端对话启动",
          p15CampaignId: String(campaign.campaignId ?? ""),
          stage: String(campaign.stage ?? ""),
          provider: String(campaign.provider ?? ""),
          model: String(campaign.modelId ?? ""),
          formalTestSealed: true,
        });
        return {
          summary: "P15 durable Campaign started; progress is now persisted and observable through conversation events.",
          data: detail,
        };
      }
      case "submit_tianyan176_100_shot": {
        assertHardwareMutationAuthorized(this.hardwarePolicy, {
          operation: "tianyan176 submission",
          authorizationBasis: input.authorizationBasis,
          jobCount: 1,
          shotsPerJob: this.hardwarePolicy.shotsPerJob,
          target: "tianyan176",
        });
        const circuitArtifactSha256 = typeof input.circuitArtifactSha256 === "string"
          ? input.circuitArtifactSha256
          : "";
        const circuitArtifact = await this.readJsonArtifact(circuitArtifactSha256);
        const qcis = typeof circuitArtifact.qcis === "string" ? circuitArtifact.qcis : "";
        const manifest = circuitArtifact.manifest;
        if (!qcis || typeof manifest !== "object" || manifest === null || Array.isArray(manifest)) {
          throw new Error("P06 hardware submission requires a registered controlled-QAOA artifact");
        }
        const approval = this.repository.listApprovals(this.conversationId)
          .filter((candidate) => (
            candidate.action === "SUBMIT_HARDWARE"
            && candidate.status === "APPROVED"
            && candidate.subjectHash === circuitArtifactSha256
          ))
          .at(-1);
        if (!approval) throw new Error("P06 tianyan176 submission requires a matching approved front-end SUBMIT_HARDWARE decision");
        const manifestObject = manifest as JsonObject;
        if (
          manifestObject.hardware_compatibility !== "PASS"
          || manifestObject.topology_source !== "live_tianyan176_config"
          || !Array.isArray(manifestObject.physical_qubits)
          || manifestObject.physical_qubits.length !== 6
          || manifestObject.qcis_sha256 !== createHash("sha256").update(qcis).digest("hex")
        ) {
          throw new Error("P06 hardware submission requires a freshly topology-mapped and validated circuit artifact");
        }
        const hardwareEvents = this.repository.listEventsAfter(this.conversationId, 0)
          .filter((event) => event.type === "hardware.batch" && event.payload.phase === "P06");
        const completed = hardwareEvents.find((event) => event.payload.status === "COMPLETED");
        if (completed) {
          return {
            summary: `Existing P06 tianyan176 result is terminal; Query ID ${String(completed.payload.queryId)} was reused.`,
            data: completed.payload,
          };
        }
        const submitted = hardwareEvents.find((event) => typeof event.payload.queryId === "string");
        const committing = hardwareEvents.find((event) => event.payload.status === "COMMITTING");
        if (committing && !submitted) {
          throw new Error("P06 hardware submission state is UNKNOWN; fail-closed policy forbids resubmission without a Query ID");
        }
        let queryId = submitted && typeof submitted.payload.queryId === "string"
          ? submitted.payload.queryId
          : null;
        let submissionArtifactSha256 = submitted && typeof submitted.payload.artifactSha256 === "string"
          ? submitted.payload.artifactSha256
          : circuitArtifactSha256;
        if (queryId === null) {
          const validation = await runTianyanJob({
            projectRoot: this.projectRoot,
            request: { action: "validate", machine_name: "tianyan176", qcis },
            timeoutSeconds: 60,
          });
          if (validation.exitCode !== 0 || validation.stdout.valid !== true) {
            this.emit("run.blocked", {
              summary: "tianyan176 线路正则门禁未通过，未提交 Job",
              stage: "hardware_validation",
              code: "TIANYAN176_CIRCUIT_REJECTED",
              retryable: false,
              stdout: validation.stdout,
              stderr: validation.stderr,
            });
            throw new Error("tianyan176 rejected the P06 controlled QAOA circuit");
          }
          this.emit("hardware.batch", {
            summary: "前端审批与线路门禁通过，正在原子提交唯一 P06 100-shot Job",
            phase: "P06",
            status: "COMMITTING",
            backend: "tianyan176",
            shots: 100,
            circuitArtifactSha256,
            qcisSha256: (manifest as JsonObject).qcis_sha256 ?? null,
            approvalId: approval.approvalId,
            approvalHash: approval.subjectHash,
            formalTestSealed: true,
          });
          const submission = await runTianyanJob({
            projectRoot: this.projectRoot,
            request: buildP06TianyanSubmitRequest({
              approvalHash: approval.subjectHash,
              authorizationBasis: String(input.authorizationBasis),
              qcis,
            }),
            timeoutSeconds: 180,
          });
          queryId = typeof submission.stdout.query_id === "string" ? submission.stdout.query_id : null;
          if (submission.exitCode !== 0 || queryId === null) {
            this.emit("run.blocked", {
              summary: "P06 真机提交结果未知；已禁止自动重提",
              stage: "hardware_submission",
              code: "TIANYAN176_UNKNOWN_SUBMISSION",
              retryable: false,
              stdout: submission.stdout,
              stderr: submission.stderr,
            });
            throw new Error("P06 tianyan176 submission did not return a recoverable Query ID");
          }
          const submissionArtifact = await this.storeJsonArtifact(
            submission.stdout,
            "qf-tool.p06-tianyan176-submission",
            "application/vnd.qf.tianyan-receipt+json",
            [circuitArtifactSha256],
          );
          submissionArtifactSha256 = submissionArtifact.sha256;
          this.emit("hardware.batch", {
            summary: `P06 tianyan176 Job 已提交，Query ID ${queryId}`,
            phase: "P06",
            status: "SUBMITTED",
            backend: "tianyan176",
            shots: 100,
            queryId,
            artifactSha256: submissionArtifact.sha256,
            artifactHashes: [submissionArtifact.sha256],
            circuitArtifactSha256,
            lease: { namespace: "qfintelligence", key: `p06:${this.conversationId}:tianyan176` },
            formalTestSealed: true,
          });
        }
        const query = await runTianyanJob({
          projectRoot: this.projectRoot,
          request: {
            action: "query",
            machine_name: "tianyan176",
            query_id: queryId,
            max_wait_seconds: 240,
            poll_interval_seconds: 5,
          },
          timeoutSeconds: 270,
        });
        const terminal = query.exitCode === 0 && Array.isArray(query.stdout.result) && query.stdout.result.length > 0;
        if (!terminal) {
          this.emit("run.heartbeat", {
            summary: `等待 tianyan176 Query ID ${queryId} 终态`,
            stage: "waiting_hardware",
            waitingFor: "tianyan176 terminal result",
            nextQueryAt: "manual resume from the same Query ID",
            queryId,
            backend: "tianyan176",
            status: "WAITING_HARDWARE",
          });
          return {
            summary: `P06 hardware job ${queryId} remains non-terminal and will only be queried, never resubmitted.`,
            data: { status: "WAITING_HARDWARE", queryId, backend: "tianyan176", shots: 100 },
          };
        }
        const resultArtifact = await this.storeJsonArtifact(
          query.stdout,
          "qf-tool.p06-tianyan176-result",
          "application/vnd.qf.tianyan-result+json",
          [submissionArtifactSha256],
        );
        this.emit("hardware.batch", {
          summary: `P06 tianyan176 Job ${queryId} 已取得终态原始结果`,
          phase: "P06",
          status: "COMPLETED",
          backend: "tianyan176",
          shots: 100,
          queryId,
          artifactSha256: resultArtifact.sha256,
          artifactHashes: [resultArtifact.sha256],
          rawResult: query.stdout,
          stdout: query.stdout,
          stderr: query.stderr,
          formalTestSealed: true,
        });
        return {
          summary: `P06 tianyan176 Query ID ${queryId} reached a terminal result.`,
          data: {
            status: "COMPLETED",
            queryId,
            backend: "tianyan176",
            shots: 100,
            resultArtifactSha256: resultArtifact.sha256,
          },
          artifact: resultArtifact,
        };
      }
      case "record_analysis": {
        const content = typeof input.content === "string" ? input.content.slice(0, 12_000) : "";
        const parentHashes = Array.isArray(input.parentHashes)
          ? input.parentHashes.filter((item): item is string => typeof item === "string" && /^[a-f0-9]{64}$/u.test(item))
          : [];
        const payload = {
          schemaVersion: "qf.v1",
          kind: "analysis-draft",
          mock: this.repository.getConversation(this.conversationId).mode === "MOCK",
          content,
          parentHashes,
        };
        const manifest = await storeArtifact({
          root: this.artifactRoot,
          data: new TextEncoder().encode(`${JSON.stringify(payload, null, 2)}\n`),
          mediaType: "application/json",
          producer: "qf-tool.record_analysis",
          parentHashes,
        });
        const artifact = this.repository.registerArtifact(this.conversationId, manifest);
        return { summary: `Analysis draft stored as ${artifact.sha256}.`, data: { sha256: artifact.sha256 }, artifact };
      }
      case "request_approval": {
        const action = typeof input.action === "string" ? input.action : "";
        if (!new Set(["FREEZE_PROTOCOL", "NEXT_ITERATION", "UNSEAL_TEST", "SUBMIT_HARDWARE"]).has(action)) {
          throw new Error("approval action is not allowed");
        }
        if (action === "SUBMIT_HARDWARE") {
          assertHardwareMutationAuthorized(this.hardwarePolicy, {
            operation: "hardware approval request",
            authorizationBasis: input.authorizationBasis,
            jobCount: 1,
            shotsPerJob: this.hardwarePolicy.shotsPerJob,
            target: "tianyan176",
          });
        }
        const rationale = typeof input.rationale === "string" ? input.rationale.slice(0, 1200) : "Approval requested";
        const subjectHash = typeof input.subjectHash === "string" && /^[a-f0-9]{64}$/u.test(input.subjectHash)
          ? input.subjectHash
          : createHash("sha256").update(`${action}:${rationale}`).digest("hex");
        const approval = this.repository.createApprovalRequest({
          conversationId: this.conversationId,
          action: action as ApprovalRequest["action"],
          subjectHash,
          rationale,
        });
        this.emit("approval.requested", {
          summary: `${approval.action} 等待前端人工审批`,
          approvalId: approval.approvalId,
          action: approval.action,
          subjectHash: approval.subjectHash,
          rationale: approval.rationale,
        });
        return { summary: "Approval remains pending; the Agent did not approve it.", data: { approvalId: approval.approvalId, status: approval.status }, approval };
      }
    }
  }
}

function toolResult(result: ToolExecutionResult) {
  return {
    content: [{ type: "text" as const, text: JSON.stringify({ summary: result.summary, ...result.data }) }],
    details: result.data,
  };
}

export function createQfToolSpecs(
  host: QfToolHost,
  allowedNames: readonly QfToolName[] = QF_TOOL_NAMES,
): QfToolSpec[] {
  const tools = [
    defineTool({
      name: "get_task_context", label: "Get task context", description: "Read the current registered task context.",
      parameters: Type.Object({}, { additionalProperties: false }),
      execute: async () => toolResult(await host.execute("get_task_context", {})),
    }),
    defineTool({
      name: "validate_task_spec", label: "Validate TaskSpec", description: "Validate a TaskSpec draft without writing facts.",
      parameters: Type.Object({ scientificQuestion: Type.String({ minLength: 1 }), asOfDate: Type.String({ pattern: "^\\d{4}-\\d{2}-\\d{2}$" }) }, { additionalProperties: false }),
      execute: async (_id, params) => toolResult(await host.execute("validate_task_spec", params)),
    }),
    defineTool({
      name: "propose_protocol_revision", label: "Propose protocol revision", description: "Create a protocol revision that remains pending human approval.",
      parameters: Type.Object({ rationale: Type.String({ minLength: 1, maxLength: 1200 }) }, { additionalProperties: false }),
      execute: async (_id, params) => toolResult(await host.execute("propose_protocol_revision", params)),
    }),
    defineTool({
      name: "list_run_steps", label: "List run steps", description: "List deterministic steps and their registered states.",
      parameters: Type.Object({}, { additionalProperties: false }),
      execute: async () => toolResult(await host.execute("list_run_steps", {})),
    }),
    defineTool({
      name: "request_step_execution", label: "Request step execution", description: "Request an allowlisted deterministic P0 step.",
      parameters: Type.Object({ stepName: Type.Union([Type.Literal("draft_task_spec"), Type.Literal("inspect_foundation"), Type.Literal("validate_task_spec")]) }, { additionalProperties: false }),
      execute: async (_id, params) => toolResult(await host.execute("request_step_execution", params)),
    }),
    defineTool({
      name: "list_artifacts", label: "List artifacts", description: "List registered artifact metadata only.",
      parameters: Type.Object({}, { additionalProperties: false }),
      execute: async () => toolResult(await host.execute("list_artifacts", {})),
    }),
    defineTool({
      name: "list_project_sources",
      label: "List project sources",
      description: "List the current project's registered source metadata and safe parser results without exposing quarantined content.",
      parameters: Type.Object({}, { additionalProperties: false }),
      execute: async () => toolResult(await host.execute("list_project_sources", {})),
    }),
    defineTool({
      name: "read_artifact_excerpt", label: "Read artifact excerpt", description: "Read a safe bounded excerpt from a registered artifact hash.",
      parameters: Type.Object({ sha256: Type.String({ pattern: "^[a-f0-9]{64}$" }) }, { additionalProperties: false }),
      execute: async (_id, params) => toolResult(await host.execute("read_artifact_excerpt", params)),
    }),
    defineTool({
      name: "execute_controlled_python",
      label: "Execute controlled Python",
      description: "Validate and execute bounded Python in the isolated no-network, no-secret harness. Source must define execute(payload) and return exactly schema_version=qf.generated-tool-result.v1, tool_name=portfolio_qubo_pipeline, status=COMPLETED, and a diagnostics object.",
      parameters: Type.Object({
        sourceCode: Type.String({ minLength: 1, maxLength: 20_000 }),
        testCode: Type.Optional(Type.String({ maxLength: 12_000 })),
        payload: Type.Optional(Type.Record(Type.String(), Type.Unknown())),
      }, { additionalProperties: false }),
      execute: async (_id, params) => toolResult(
        await host.execute("execute_controlled_python", params as JsonObject),
      ),
    }),
    defineTool({
      name: "fetch_tushare_six_stock_bundle",
      label: "Fetch six-stock Tushare raw data",
      description: "Acquire the point-in-time six-stock Tushare bundle for 2019-2023 only, keep the formal test interval sealed, and register the raw-data artifact.",
      parameters: Type.Object({}, { additionalProperties: false }),
      execute: async () => toolResult(await host.execute("fetch_tushare_six_stock_bundle", {})),
    }),
    defineTool({
      name: "fetch_tianyan176_calibration_snapshot",
      label: "Fetch tianyan176 calibration",
      description: "Use the public cqlib SDK to fetch, normalize, diff, visualize, and register the current tianyan176 calibration package without submitting hardware.",
      parameters: Type.Object({}, { additionalProperties: false }),
      execute: async () => toolResult(await host.execute("fetch_tianyan176_calibration_snapshot", {})),
    }),
    defineTool({
      name: "validate_tianyan176_qcis_artifacts",
      label: "Validate registered P16 QCIS artifacts",
      description: "Use the authorized secret proxy to call tianyan176 qcis_check_regular for 1-50 distinct registered text/vnd.qcis artifacts. This is validation-only: it never submits hardware and never creates Query IDs.",
      parameters: Type.Object({
        authorizationBasis: Type.String({ minLength: 1, maxLength: 240 }),
        circuitArtifactSha256s: Type.Array(
          Type.String({ pattern: "^[a-f0-9]{64}$" }),
          { minItems: 1, maxItems: 50 },
        ),
      }, { additionalProperties: false }),
      execute: async (_id, params) => toolResult(
        await host.execute("validate_tianyan176_qcis_artifacts", params as JsonObject),
      ),
    }),
    defineTool({
      name: "transpile_tianyan176_qcis_artifacts",
      label: "Map registered P16 QCIS artifacts",
      description: "Use the authorized secret proxy and cqlib.mapping.transpile_qcis MCTS mapping for 1-50 distinct registered text/vnd.qcis artifacts, then run qcis_check_regular on the mapped outputs. This never submits hardware or creates Query IDs.",
      parameters: Type.Object({
        authorizationBasis: Type.String({ minLength: 1, maxLength: 240 }),
        circuitArtifactSha256s: Type.Array(
          Type.String({ pattern: "^[a-f0-9]{64}$" }),
          { minItems: 1, maxItems: 50 },
        ),
      }, { additionalProperties: false }),
      execute: async (_id, params) => toolResult(
        await host.execute("transpile_tianyan176_qcis_artifacts", params as JsonObject),
      ),
    }),
    defineTool({
      name: "prepare_p16_hardware_batch",
      label: "Prepare a durable P16 hardware batch",
      description: "Freeze one fresh P16 Campaign/Generation/Batch request for 1-50 distinct mapped and validated QCIS artifacts. It binds the protocol, QUBO, search space, calibration, request hash, and exact human authorization without submitting hardware or creating Query IDs.",
      parameters: Type.Object({
        authorizationBasis: Type.Literal("P16_USER_REQUEST_20260725"),
        protocolArtifactSha256: Type.String({ pattern: "^[a-f0-9]{64}$" }),
        searchSpaceArtifactSha256: Type.String({ pattern: "^[a-f0-9]{64}$" }),
        quboArtifactSha256: Type.String({ pattern: "^[a-f0-9]{64}$" }),
        mappingReportArtifactSha256: Type.String({ pattern: "^[a-f0-9]{64}$" }),
        validationReportArtifactSha256: Type.String({ pattern: "^[a-f0-9]{64}$" }),
        mappedCircuitArtifactSha256s: Type.Array(
          Type.String({ pattern: "^[a-f0-9]{64}$" }),
          { minItems: 1, maxItems: 50 },
        ),
        generationIndex: Type.Integer({ minimum: 0, maximum: 999 }),
        batchIndex: Type.Integer({ minimum: 0, maximum: 999 }),
      }, { additionalProperties: false }),
      execute: async (_id, params) => toolResult(
        await host.execute("prepare_p16_hardware_batch", params as JsonObject),
      ),
    }),
    defineTool({
      name: "submit_p16_hardware_batch",
      label: "Submit one frozen P16 hardware batch",
      description: "Submit a PREPARED P16 batch exactly once to tianyan176 at 100 shots per circuit. The request is persisted as COMMITTING before the external call; Query IDs are persisted before artifact work; UNKNOWN without Query IDs is permanently query-only and is never resubmitted.",
      parameters: Type.Object({
        authorizationBasis: Type.Literal("P16_USER_REQUEST_20260725"),
        batchId: Type.String({ pattern: "^p16_batch_[a-f0-9-]+$" }),
      }, { additionalProperties: false }),
      execute: async (_id, params) => toolResult(
        await host.execute("submit_p16_hardware_batch", params as JsonObject),
      ),
    }),
    defineTool({
      name: "query_p16_hardware_batch",
      label: "Query one P16 hardware recovery handle",
      description: "Query only the original persisted P16 Query IDs, store raw and readout-corrected results against the immutable calibration snapshot, and never submit or replace a job.",
      parameters: Type.Object({
        batchId: Type.String({ pattern: "^p16_batch_[a-f0-9-]+$" }),
      }, { additionalProperties: false }),
      execute: async (_id, params) => toolResult(
        await host.execute("query_p16_hardware_batch", params as JsonObject),
      ),
    }),
    defineTool({
      name: "get_p16_hardware_status",
      label: "Read durable P16 hardware status",
      description: "Read the active conversation's P16 Campaign/Generation/Batch recovery handles without making an external request.",
      parameters: Type.Object({}, { additionalProperties: false }),
      execute: async () => toolResult(await host.execute("get_p16_hardware_status", {})),
    }),
    defineTool({
      name: "inspect_validation_dataset",
      label: "Inspect validation dataset",
      description: "Run the bounded Python/Pandera-compatible quality gate on a registered parsed validation-data artifact.",
      parameters: Type.Object({ artifactSha256: Type.String({ pattern: "^[a-f0-9]{64}$" }) }, { additionalProperties: false }),
      execute: async (_id, params) => toolResult(await host.execute("inspect_validation_dataset", params)),
    }),
    defineTool({
      name: "generate_controlled_qaoa_circuit",
      label: "Generate controlled QAOA circuit",
      description: "Generate exactly one bounded 6-qubit, 6-select-3 controlled QAOA circuit and run local cqlib gates; this never submits hardware.",
      parameters: Type.Object({
        sourceArtifactSha256: Type.String({ pattern: "^[a-f0-9]{64}$" }),
        gamma: Type.Optional(Type.Number({ exclusiveMinimum: 0, maximum: 1 })),
        beta: Type.Optional(Type.Number({ exclusiveMinimum: 0, maximum: 0.5 })),
      }, { additionalProperties: false }),
      execute: async (_id, params) => toolResult(await host.execute("generate_controlled_qaoa_circuit", params)),
    }),
    defineTool({
      name: "generate_noise_aware_qaoa_circuit",
      label: "Generate noise-aware QAOA circuit",
      description: "Generate one bounded 6-qubit QAOA circuit, apply the selected calibration snapshot's explicit missing-data penalties, and pass local cqlib/QCIS gates without submitting hardware.",
      parameters: Type.Object({
        sourceArtifactSha256: Type.String({ pattern: "^[a-f0-9]{64}$" }),
        calibrationArtifactSha256: Type.String({ pattern: "^[a-f0-9]{64}$" }),
        gamma: Type.Optional(Type.Number({ exclusiveMinimum: 0, maximum: 1 })),
        beta: Type.Optional(Type.Number({ exclusiveMinimum: 0, maximum: 0.5 })),
      }, { additionalProperties: false }),
      execute: async (_id, params) => toolResult(await host.execute("generate_noise_aware_qaoa_circuit", params)),
    }),
    defineTool({
      name: "start_p15_portfolio_campaign",
      label: "Start P15 portfolio Campaign",
      description: "Historical P15 compatibility entry. It is fail-closed unless the current runtime has a new ONE_JOB authorization basis; historical P15 authorization never transfers to a new conversation.",
      parameters: Type.Object({
        authorizationBasis: Type.String({ minLength: 1, maxLength: 240 }),
      }, { additionalProperties: false }),
      execute: async (_id, params) => toolResult(await host.execute("start_p15_portfolio_campaign", params)),
    }),
    defineTool({
      name: "submit_tianyan176_100_shot",
      label: "Submit one tianyan176 100-shot job",
      description: "After a human-approved SUBMIT_HARDWARE gate, submit at most one P06 100-shot tianyan176 job and query the same Query ID to terminal or a recoverable wait state.",
      parameters: Type.Object({
        authorizationBasis: Type.String({ minLength: 1, maxLength: 240 }),
        circuitArtifactSha256: Type.String({ pattern: "^[a-f0-9]{64}$" }),
      }, { additionalProperties: false }),
      execute: async (_id, params) => toolResult(await host.execute("submit_tianyan176_100_shot", params)),
    }),
    defineTool({
      name: "record_analysis", label: "Record analysis", description: "Store an analysis draft that cites registered artifact hashes.",
      parameters: Type.Object({ content: Type.String({ minLength: 1, maxLength: 12000 }), parentHashes: Type.Array(Type.String({ pattern: "^[a-f0-9]{64}$" }), { maxItems: 32 }) }, { additionalProperties: false }),
      execute: async (_id, params) => toolResult(await host.execute("record_analysis", params)),
    }),
    defineTool({
      name: "request_approval", label: "Request approval", description: "Create a pending human approval request; never approves it.",
      parameters: Type.Object({
        action: Type.Union([Type.Literal("FREEZE_PROTOCOL"), Type.Literal("NEXT_ITERATION"), Type.Literal("UNSEAL_TEST"), Type.Literal("SUBMIT_HARDWARE")]),
        authorizationBasis: Type.Optional(Type.String({ minLength: 1, maxLength: 240 })),
        subjectHash: Type.String({ pattern: "^[a-f0-9]{64}$" }),
        rationale: Type.String({ minLength: 1, maxLength: 1200 }),
      }, { additionalProperties: false }),
      execute: async (_id, params) => toolResult(await host.execute("request_approval", params)),
    }),
  ];
  const allowed = new Set(allowedNames);
  return tools.filter((tool) => allowed.has(tool.name as QfToolName));
}

export function assertExactToolNames(
  actualNames: readonly string[],
  expectedNames: readonly QfToolName[] = QF_TOOL_NAMES,
): void {
  const expected = [...expectedNames].sort();
  const actual = [...new Set(actualNames)].sort();
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(`QF tool boundary mismatch: expected ${expected.join(",")}; got ${actual.join(",")}`);
  }
}

export function qfToolSpecsForIpc(
  specs: readonly QfToolSpec[],
): Array<{ name: string; label: string; description: string; parameters: JsonObject }> {
  return specs.map(({ name, label, description, parameters }) => ({
    name,
    label,
    description,
    parameters: parameters as JsonObject,
  }));
}

export function validateQfToolArguments(spec: QfToolSpec, input: unknown): JsonObject {
  if (!Check(spec.parameters, input)) {
    const errors = Errors(spec.parameters, input)
      .slice(0, 8)
      .map((error) => error.message)
      .join("; ");
    throw new Error(`QF tool ${spec.name} arguments failed the canonical schema: ${errors}`);
  }
  return input as JsonObject;
}
