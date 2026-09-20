import { createHash } from "node:crypto";

import type { P05Repository } from "../p05/p05-repository.js";
import type { ProviderRegistry } from "../agent/providers.js";
import type { RuntimeConfig } from "../config.js";
import type { WorkspaceRepository } from "../db/repository.js";
import { assertHardwareMutationAuthorized, type HardwareExecutionPolicy } from "../hardware-policy.js";
import { P07Orchestrator } from "./p07-orchestrator.js";
import type { P07ModelId, P07Provider, P07Repository } from "./p07-repository.js";

const AUTHORIZATION_PHRASES: Record<P07ModelId, string> = {
  "deepseek-v4-pro": "AUTHORIZE P07 DEEPSEEK AND TIANYAN176",
  "gpt-5.6": "AUTHORIZE P07 GPT-5.6 AND REUSE TIANYAN176",
};

function hash(value: unknown): string {
  return createHash("sha256").update(JSON.stringify(value)).digest("hex");
}

function objectiveIsSafe(objective: string): boolean {
  return !/(ignore|override|replace).{0,40}(system|developer|instruction)|unseal.{0,30}test|reveal.{0,30}(key|token|secret)/isu.test(
    objective.normalize("NFKC"),
  );
}

export class P07Service {
  readonly orchestrator: P07Orchestrator;
  private readonly hardwarePolicy: HardwareExecutionPolicy;

  constructor(
    projectRoot: string,
    artifactRoot: string,
    config: RuntimeConfig,
    private readonly repository: WorkspaceRepository,
    private readonly p05: P05Repository,
    private readonly p07: P07Repository,
    private readonly providers: ProviderRegistry,
  ) {
    this.hardwarePolicy = config.hardwarePolicy;
    this.orchestrator = new P07Orchestrator(projectRoot, artifactRoot, config, repository, p07, providers);
  }

  launch(input: {
    conversationId: string;
    objective: string;
    predecessorCampaignId?: string;
  }): ReturnType<P07Repository["getDetail"]> {
    const conversation = this.repository.assertConversationWritable(input.conversationId);
    assertHardwareMutationAuthorized(this.hardwarePolicy, {
      operation: "P07 Campaign launch",
      jobCount: 50,
      shotsPerJob: 100,
      target: "tianyan176",
    });
    if (conversation.mode !== "OPENHANDS") throw new Error("P07 requires an OpenHands conversation");
    let provider: P07Provider;
    let modelId: P07ModelId;
    if (conversation.provider === "deepseek" && conversation.modelId === "deepseek-v4-pro") {
      provider = "deepseek/getoken";
      modelId = "deepseek-v4-pro";
    } else if (conversation.provider === "openai" && conversation.modelId === "gpt-5.6") {
      provider = "openai/getoken";
      modelId = "gpt-5.6";
    } else {
      throw new Error("P07 supports only an explicitly locked deepseek-v4-pro or gpt-5.6 OpenHands conversation");
    }
    if (modelId === "gpt-5.6") {
      if (!input.predecessorCampaignId) throw new Error("P07 gpt-5.6 continuation requires the immutable predecessor Campaign");
      const predecessor = this.p07.getCampaign(input.predecessorCampaignId);
      const predecessorDetail = this.p07.getDetail(input.predecessorCampaignId);
      const completedHardware = Array.isArray(predecessorDetail.hardwareBatches)
        && predecessorDetail.hardwareBatches.some((item) => {
          if (typeof item !== "object" || item === null || Array.isArray(item)) return false;
          const batch = item as Record<string, unknown>;
          return batch.status === "COMPLETED" && batch.circuitCount === 50
            && Array.isArray(batch.queryIds) && batch.queryIds.length === 50;
        });
      if (predecessor.status !== "BLOCKED" || predecessor.modelId !== "deepseek-v4-pro" || !completedHardware) {
        throw new Error("P07 gpt-5.6 continuation requires the completed-hardware DeepSeek BLOCKED predecessor");
      }
    }
    if (!objectiveIsSafe(input.objective)) throw new Error("P07 objective was quarantined by InputSecurityRouter");
    const uploads = this.p05.listUploads(input.conversationId);
    if (!uploads.some((upload) => !upload.quarantined)) throw new Error("P07 requires at least one safely parsed browser upload");
    if (!uploads.some((upload) => upload.quarantined && upload.riskLevel === "HIGH")) {
      throw new Error("P07 requires a real prompt-injection fixture quarantined by the upload route");
    }
    const campaignId = this.p07.createCampaign({
      conversationId: input.conversationId,
      taskId: conversation.taskId,
      objective: input.objective,
      provider,
      modelId,
      ...(input.predecessorCampaignId ? { predecessorCampaignId: input.predecessorCampaignId } : {}),
    });
    this.p07.appendEvent({
      campaignId,
      lane: "data_quality",
      eventType: "approval.requested",
      idempotencyKey: `${campaignId}:authorization-requested`,
      payload: {
        summary: "P07 Execution Plan is ready for explicit human authorization.",
        provider,
        model: modelId,
        predecessorCampaignId: input.predecessorCampaignId ?? null,
        hardware: "tianyan176",
        hardwareMode: input.predecessorCampaignId ? "read-only predecessor Query ID reuse" : "single idempotent batch",
        minimumRuntimeSeconds: 21_600,
        minimumVerifiedTokens: 20_000_000,
        targetVerifiedTokens: 50_000_000,
        formalTestSealed: true,
      },
    });
    return this.p07.getDetail(campaignId);
  }

  authorize(campaignId: string, phrase: string): ReturnType<P07Repository["getDetail"]> {
    const campaign = this.p07.getCampaign(campaignId);
    this.repository.assertConversationWritable(campaign.conversationId);
    if (phrase !== AUTHORIZATION_PHRASES[campaign.modelId]) throw new Error("P07 authorization phrase is invalid");
    assertHardwareMutationAuthorized(this.hardwarePolicy, {
      operation: "P07 Campaign authorization",
      authorizationBasis: phrase,
      jobCount: 50,
      shotsPerJob: 100,
      target: "tianyan176",
    });
    const authorizationHash = hash({
      campaignId,
      taskId: campaign.taskId,
      provider: campaign.provider,
      model: campaign.modelId,
      predecessorCampaignId: campaign.predecessorCampaignId,
      backend: "tianyan176",
      hardwareMode: campaign.predecessorCampaignId ? "REUSE_ONLY" : "SUBMIT_ONCE",
      maxBatch: 50,
      targetTokens: 50_000_000,
      minimumRuntimeSeconds: 21_600,
      formalTestSealed: true,
      phrase,
    });
    this.p07.authorize(campaignId, authorizationHash);
    this.p07.appendEvent({
      campaignId,
      lane: "deepseek_agents",
      eventType: "approval.resolved",
      idempotencyKey: `${campaignId}:authorized`,
      payload: {
        summary: `Human authorization recorded; P07 ${campaign.modelId} background Campaign started.`,
        actor: "HUMAN",
        authorizationHash,
        provider: campaign.provider,
        model: campaign.modelId,
      },
    });
    this.orchestrator.start(campaignId);
    return this.p07.getDetail(campaignId);
  }

  resume(campaignId: string): ReturnType<P07Repository["getDetail"]> {
    const campaign = this.p07.getCampaign(campaignId);
    this.repository.assertConversationWritable(campaign.conversationId);
    assertHardwareMutationAuthorized(this.hardwarePolicy, {
      operation: "P07 Campaign resume",
      jobCount: 50,
      shotsPerJob: 100,
      target: "tianyan176",
    });
    if (campaign.status === "COMPLETED") return this.p07.getDetail(campaignId);
    this.providers.runtimeConfiguration(campaign.provider.split("/")[0]!);
    this.p07.setCampaign({ campaignId, status: "RUNNING", stage: "resuming_from_checkpoint", allowExternalCalls: campaign.verifiedTotalTokens < 50_000_000, error: null });
    this.orchestrator.start(campaignId);
    return this.p07.getDetail(campaignId);
  }

  setGate(campaignId: string, input: { gate: "browser" | "engineering"; status: "PASS" | "FAIL" }): ReturnType<P07Repository["getDetail"]> {
    const campaign = this.p07.getCampaign(campaignId);
    this.repository.assertConversationWritable(campaign.conversationId);
    this.p07.setCampaign({ campaignId, ...(input.gate === "browser" ? { browserGate: input.status } : { engineeringGate: input.status }) });
    this.p07.appendEvent({
      campaignId,
      lane: "visualization_archive",
      eventType: "gate.updated",
      idempotencyKey: `${campaignId}:gate:${input.gate}:${input.status}`,
      payload: { summary: `${input.gate} gate set to ${input.status}.`, gate: input.gate, status: input.status },
    });
    return this.p07.getDetail(campaignId);
  }

  async finalizeBlocked(campaignId: string): Promise<ReturnType<P07Repository["getDetail"]>> {
    const campaign = this.p07.getCampaign(campaignId);
    this.repository.assertConversationWritable(campaign.conversationId);
    return this.orchestrator.finalizeBlocked(campaignId) as Promise<ReturnType<P07Repository["getDetail"]>>;
  }

  get(campaignId: string): ReturnType<P07Repository["getDetail"]> {
    return this.p07.getDetail(campaignId);
  }

  list(): ReturnType<P07Repository["listCampaigns"]> {
    return this.p07.listCampaigns();
  }
}
