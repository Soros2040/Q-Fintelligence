import type { JsonObject } from "@q-fintelligence/contracts";

import { buildFullQfSystemPrompt, systemPromptHash } from "../agent/system-prompt.js";
import type { RuntimeConfig } from "../config.js";
import { StateConflictError, type WorkspaceRepository } from "../db/repository.js";
import {
  acceptanceHash,
  type OpenHandsAcceptanceCampaign,
  OpenHandsAcceptanceRepository,
} from "./openhands-acceptance-repository.js";

export interface OpenHandsAcceptanceLaunchInput {
  conversationId: string;
  fixedProviderModel: "deepseek/deepseek-v4-pro";
  minimumWallClockSeconds: number;
  minimumOverlapSeconds: number;
  hardwareMode: "READ_ONLY";
  hardwareTarget: "tianyan176";
  maxNewHardwareJobs: 0;
  shotsPerJob: 0;
  gitAction: "NONE";
}

function nonNegativeInteger(value: unknown): number {
  return Number.isSafeInteger(value) && Number(value) >= 0 ? Number(value) : 0;
}

export class OpenHandsAcceptanceService {
  constructor(
    private readonly repository: WorkspaceRepository,
    private readonly acceptanceRepository: OpenHandsAcceptanceRepository,
    private readonly config: RuntimeConfig,
  ) {}

  create(input: OpenHandsAcceptanceLaunchInput): OpenHandsAcceptanceCampaign {
    if (
      input.fixedProviderModel !== "deepseek/deepseek-v4-pro"
      || input.minimumWallClockSeconds < 7_200
      || input.minimumOverlapSeconds < 6_900
      || input.hardwareMode !== "READ_ONLY"
      || input.hardwareTarget !== "tianyan176"
      || input.maxNewHardwareJobs !== 0
      || input.shotsPerJob !== 0
      || input.gitAction !== "NONE"
    ) {
      throw new StateConflictError("OpenHands acceptance authorization does not match the frozen execution envelope");
    }
    const conversation = this.repository.getConversation(input.conversationId);
    const project = this.repository.getProject(conversation.projectId);
    if (!project.name.includes("OpenHands 重构后两小时量子验收")) {
      throw new StateConflictError("acceptance Project name must include OpenHands 重构后两小时量子验收");
    }
    if (
      conversation.mode !== "OPENHANDS"
      || conversation.provider !== "deepseek"
      || conversation.modelId !== "deepseek-v4-pro"
    ) {
      throw new StateConflictError("acceptance Campaign requires a fresh OpenHands DeepSeek v4 Pro conversation");
    }
    const session = this.repository.getSession(input.conversationId);
    if (!session || session.runtimeKind !== "OPENHANDS" || !/^[a-f0-9]{64}$/u.test(session.configHash)) {
      throw new StateConflictError("OpenHands runtime session preflight must complete before Campaign creation");
    }
    const events = this.repository.listEventsAfter(input.conversationId, 0);
    const snapshot = this.repository.getSnapshot(input.conversationId);
    const usageEvents = events.filter((event) => event.type === "model.usage");
    const toolStarts = events.filter((event) => event.type === "tool.started");
    const toolCompletes = events.filter((event) => event.type === "tool.completed");
    const completedNames = new Set(toolCompletes.flatMap((event) => {
      const name = event.payload.toolName ?? event.payload.name;
      return typeof name === "string" ? [name] : [];
    }));
    if (
      usageEvents.length < 1
      || toolStarts.length < 1
      || toolCompletes.length < 1
      || !completedNames.has("get_task_context")
      || !completedNames.has("list_project_sources")
      || snapshot.approvals.some((approval) => approval.action === "UNSEAL_TEST" && approval.status === "APPROVED")
    ) {
      throw new StateConflictError("real OpenHands Provider usage and the two-tool read-only canary must pass before Campaign creation");
    }
    const authorization = {
      ...input,
      formalTestSealed: true,
      conversationId: conversation.conversationId,
      taskId: conversation.taskId,
      projectId: conversation.projectId,
      runtimeSessionId: session.sessionId,
      runtimeRevision: session.runtimeRevision,
      runtimeConfigHash: session.configHash,
      systemPromptHash: session.promptHash,
    };
    const campaign = this.acceptanceRepository.create({
      projectId: conversation.projectId,
      conversationId: conversation.conversationId,
      taskId: conversation.taskId,
      authorizationHash: acceptanceHash(authorization),
      systemPromptHash: systemPromptHash(buildFullQfSystemPrompt(this.config.hardwarePolicy)),
      runtimeConfigHash: session.configHash,
      minimumWallClockSeconds: input.minimumWallClockSeconds,
      minimumOverlapSeconds: input.minimumOverlapSeconds,
    });
    const providerPromptTokens = usageEvents.reduce(
      (sum, event) => sum + nonNegativeInteger(event.payload.input),
      0,
    );
    const providerCompletionTokens = usageEvents.reduce(
      (sum, event) => sum + nonNegativeInteger(event.payload.output),
      0,
    );
    this.acceptanceRepository.setProviderUsage(
      campaign.campaignId,
      usageEvents.length,
      providerPromptTokens,
      providerCompletionTokens,
    );
    return this.acceptanceRepository.get(campaign.campaignId);
  }

  syncProviderUsage(campaignId: string): OpenHandsAcceptanceCampaign {
    const campaign = this.acceptanceRepository.get(campaignId);
    if (campaign.baselineEventSequence === null) {
      this.acceptanceRepository.setProviderUsage(campaignId, 0, 0, 0);
      return this.acceptanceRepository.get(campaignId);
    }
    const usageEvents = this.repository.listEventsAfter(campaign.conversationId, 0)
      .filter((event) => event.type === "model.usage"
        && event.sequence !== null
        && event.sequence > campaign.baselineEventSequence!);
    const promptTokens = usageEvents.reduce((sum, event) => sum + nonNegativeInteger(event.payload.input), 0);
    const completionTokens = usageEvents.reduce((sum, event) => sum + nonNegativeInteger(event.payload.output), 0);
    this.acceptanceRepository.setProviderUsage(campaignId, usageEvents.length, promptTokens, completionTokens);
    return this.acceptanceRepository.get(campaignId);
  }

  start(campaignId: string): OpenHandsAcceptanceCampaign {
    const campaign = this.acceptanceRepository.get(campaignId);
    if (
      this.config.hardwarePolicy.mode !== "READ_ONLY"
      || this.config.hardwarePolicy.target !== "tianyan176"
      || this.config.hardwarePolicy.maxNewHardwareJobs !== 0
      || this.config.hardwarePolicy.shotsPerJob !== 0
    ) {
      throw new StateConflictError("Campaign runtime hardware policy drifted from READ_ONLY/0 Job/0 shots");
    }
    return this.acceptanceRepository.start(campaignId);
  }

  compensateStart(campaignId: string): OpenHandsAcceptanceCampaign {
    return this.acceptanceRepository.compensateStart(campaignId);
  }

  resumeStart(campaignId: string): OpenHandsAcceptanceCampaign {
    if (
      this.config.hardwarePolicy.mode !== "READ_ONLY"
      || this.config.hardwarePolicy.target !== "tianyan176"
      || this.config.hardwarePolicy.maxNewHardwareJobs !== 0
      || this.config.hardwarePolicy.shotsPerJob !== 0
    ) {
      throw new StateConflictError("Campaign startup recovery hardware policy drifted from READ_ONLY/0 Job/0 shots");
    }
    return this.acceptanceRepository.resumeStart(campaignId);
  }

  startupComplete(campaignId: string): boolean {
    return this.acceptanceRepository.startupComplete(campaignId);
  }

  completeStart(campaignId: string): OpenHandsAcceptanceCampaign {
    return this.acceptanceRepository.completeStart(campaignId);
  }

  get(campaignId: string): OpenHandsAcceptanceCampaign {
    return this.acceptanceRepository.get(campaignId);
  }

  list(): OpenHandsAcceptanceCampaign[] {
    return this.acceptanceRepository.list();
  }

  audit(campaignId: string): JsonObject {
    this.syncProviderUsage(campaignId);
    return this.acceptanceRepository.audit(campaignId);
  }

  finalize(campaignId: string): { campaign: OpenHandsAcceptanceCampaign; audit: JsonObject; passed: boolean } {
    this.syncProviderUsage(campaignId);
    return this.acceptanceRepository.finalize(campaignId);
  }
}
