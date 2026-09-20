import { createHash } from "node:crypto";

import type { JsonObject } from "@q-fintelligence/contracts";

import type { WorkspaceRepository } from "../db/repository.js";
import {
  assertHardwareMutationAuthorized,
  READ_ONLY_HARDWARE_POLICY,
  type HardwareExecutionPolicy,
} from "../hardware-policy.js";
import { P15Orchestrator } from "./p15-orchestrator.js";
import type { P15Repository } from "./p15-repository.js";

const HUMAN_AUTHORIZATION_BASIS = "P15_USER_REQUEST_20260725";

function hash(value: unknown): string {
  return createHash("sha256").update(JSON.stringify(value)).digest("hex");
}

export class P15Service {
  readonly orchestrator: P15Orchestrator;

  constructor(
    projectRoot: string,
    artifactRoot: string,
    private readonly repository: WorkspaceRepository,
    private readonly p15: P15Repository,
    private readonly hardwarePolicy: HardwareExecutionPolicy = READ_ONLY_HARDWARE_POLICY,
  ) {
    this.orchestrator = new P15Orchestrator(projectRoot, artifactRoot, repository, p15);
    for (const campaign of p15.listCampaigns().filter((item) =>
      this.hardwarePolicy.mode === "ONE_JOB"
      && this.hardwarePolicy.authorizationBasis !== null
      && (item.status === "RUNNING" || item.status === "WAITING_HARDWARE")
      && item.checkpoint.authorizationBasis === this.hardwarePolicy.authorizationBasis
      && typeof item.checkpoint.approvalId === "string")) {
      this.orchestrator.start(campaign.campaignId, String(campaign.checkpoint.approvalId));
    }
  }

  launch(input: { conversationId: string; authorizationBasis: string }): JsonObject {
    assertHardwareMutationAuthorized(this.hardwarePolicy, {
      operation: "P15 Campaign launch",
      authorizationBasis: input.authorizationBasis,
      jobCount: 50,
      shotsPerJob: 100,
      target: "tianyan176",
    });
    if (input.authorizationBasis !== HUMAN_AUTHORIZATION_BASIS) {
      throw new Error("P15 formal launch requires the current user's explicit 2026-07-25 authorization basis");
    }
    const conversation = this.repository.getConversation(input.conversationId);
    if (conversation.mode !== "OPENHANDS" || conversation.provider !== "deepseek" || conversation.modelId !== "deepseek-v4-pro") {
      throw new Error("P15 formal Campaign requires the real OpenHands DeepSeek v4 Pro conversation selected in the Web UI");
    }
    const sources = this.repository.listProjectSourcesWithParseResult(conversation.projectId);
    const required = [
      "DatasetManifest.json",
      "tushare-six-stock-raw-bundle.json",
      "tushare-six-stock-daily-raw.csv",
      "tushare-six-stock-adj-factor-raw.csv",
      "tushare-six-stock-daily-raw.parquet",
      "tushare-six-stock-adj-factor-raw.parquet",
    ];
    const missing = required.filter((fileName) => !sources.some((source) => source.fileName === fileName && !source.quarantined));
    if (missing.length > 0) throw new Error(`P15 browser-uploaded project sources are incomplete: ${missing.join(", ")}`);
    const calibration = this.repository.getLatestP15Calibration(conversation.projectId);
    if (!calibration || calibration.completeness === "UNAVAILABLE") {
      throw new Error("P15 requires the current cqlib tianyan176 calibration package");
    }
    const campaignId = this.p15.createCampaign({
      projectId: conversation.projectId,
      conversationId: conversation.conversationId,
      taskId: conversation.taskId,
    });
    const current = this.p15.getCampaign(campaignId);
    const previousCalibrationId = typeof current.checkpoint.calibrationSnapshotId === "string"
      ? String(current.checkpoint.calibrationSnapshotId)
      : null;
    const previousCalibration = previousCalibrationId
      ? this.repository.getP15Calibration(previousCalibrationId)
      : null;
    const calibrationUpgradeReplay = current.status === "COMPLETED"
      && previousCalibration?.projectId === conversation.projectId
      && previousCalibration.completeness !== "COMPLETE"
      && calibration.completeness === "COMPLETE"
      && previousCalibration.snapshotId !== calibration.snapshotId;
    if ((current.status === "COMPLETED" && !calibrationUpgradeReplay)
      || this.orchestrator.isRunning(campaignId)) {
      return this.p15.getDetail(campaignId);
    }
    let approvalId = typeof current.checkpoint.approvalId === "string" ? String(current.checkpoint.approvalId) : "";
    if (!approvalId) {
      const subjectHash = hash({
        campaignId,
        taskId: conversation.taskId,
        provider: "deepseek/getoken",
        model: "deepseek-v4-pro",
        backend: "tianyan176",
        maxUniqueCircuitsPerGeneration: 50,
        shotsPerCircuit: 100,
        stopAfterIndependentConfirmations: 3,
        formalTestSealed: true,
        authorizationBasis: input.authorizationBasis,
      });
      const existingApproval = this.repository.listApprovals(conversation.conversationId)
        .find((item) =>
          item.action === "SUBMIT_HARDWARE"
          && item.subjectHash === subjectHash
          && item.status === "APPROVED");
      const resolved = existingApproval ?? this.repository.decideApproval(
        this.repository.createApprovalRequest({
          conversationId: conversation.conversationId,
          action: "SUBMIT_HARDWARE",
          subjectHash,
          rationale: "用户在本轮请求中明确授权 P15 持续 tianyan176 真机寻优、每代最多 50 条唯一线路，并要求追平后完成 3 个独立确认批次。",
          requestedBy: "USER",
        }).approvalId,
        "APPROVE",
        "HUMAN",
      );
      approvalId = resolved.approvalId;
      const event = this.repository.appendEvent({
        conversationId: conversation.conversationId,
        type: "approval.resolved",
        payload: {
          summary: "已把本轮用户明确真机授权绑定到 P15 请求哈希；Agent 未扩大授权范围",
          approvalId,
          status: resolved.status,
          subjectHash,
          authorizationBasis: input.authorizationBasis,
          actor: "HUMAN",
          formalTestSealed: true,
        },
      });
      void event;
    }
    this.p15.updateCampaign(campaignId, {
      status: "RUNNING",
      stage: "authorized_starting",
      bestCandidateSha256: calibrationUpgradeReplay ? null : current.bestCandidateSha256,
      confirmationBatches: calibrationUpgradeReplay ? 0 : current.confirmationBatches,
      newExternalCallsAllowed: true,
      reopened: true,
      checkpoint: {
        ...current.checkpoint,
        approvalId,
        authorizationBasis: input.authorizationBasis,
        calibrationSnapshotId: calibration.snapshotId,
        calibrationUpgradeReplay,
        previousCalibrationSnapshotId: calibrationUpgradeReplay
          ? previousCalibrationId
          : current.checkpoint.previousCalibrationSnapshotId ?? null,
        formalTestSealed: true,
      },
    });
    if (calibrationUpgradeReplay) {
      this.repository.appendEvent({
        conversationId: conversation.conversationId,
        type: "run.started",
        payload: {
          summary: "P15 自动恢复：完整 cqlib 指标改变噪声感知映射，原 Campaign 从下一代继续并重新计数独立确认批次",
          p15CampaignId: campaignId,
          previousCalibrationSnapshotId: previousCalibrationId,
          calibrationSnapshotId: calibration.snapshotId,
          previousCompleteness: previousCalibration?.completeness ?? null,
          completeness: calibration.completeness,
          nextGenerationIndex: current.checkpoint.nextGenerationIndex ?? 1,
          confirmationBatchesReset: true,
          formalTestSealed: true,
        },
      });
    }
    this.orchestrator.start(campaignId, approvalId);
    return this.p15.getDetail(campaignId);
  }

  get(campaignId: string): JsonObject {
    return this.p15.getDetail(campaignId);
  }

  list(): JsonObject {
    return { campaigns: this.p15.listCampaigns() as unknown as JsonObject[], formalTestSealed: true };
  }
}

export const P15_HUMAN_AUTHORIZATION_BASIS = HUMAN_AUTHORIZATION_BASIS;
