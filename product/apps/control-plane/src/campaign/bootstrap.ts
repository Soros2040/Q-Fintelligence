import type { CampaignSummary } from "@q-fintelligence/contracts";

import type { WorkspaceRepository } from "../db/repository.js";
import { P02_AUTHORIZATION, assertStandingAuthorization, authorizationHash } from "./authorization.js";
import type { CampaignRepository } from "./repository.js";
import { assertCampaignRoleToolNames, campaignRoleDefinitions } from "./roles.js";

export function ensureP02Campaign(
  workspaceRepository: WorkspaceRepository,
  campaignRepository: CampaignRepository,
): CampaignSummary {
  assertStandingAuthorization(P02_AUTHORIZATION);
  const definitions = campaignRoleDefinitions();
  for (const definition of definitions) assertCampaignRoleToolNames(definition.role, definition.toolNames);
  const existing = campaignRepository.listCampaigns().find((campaign) => campaign.authorizationHash === authorizationHash());
  if (existing) return existing;
  const project = workspaceRepository.listProjects(true).find((item) => item.name === "P02 长程真实链路")
    ?? workspaceRepository.createProject("P02 长程真实链路", "六股票风险图到天衍云模拟与受限真机的可恢复 Campaign");
  const conversation = workspaceRepository.createConversation({
    projectId: project.projectId,
    title: "P02 已授权正式 Campaign",
    mode: "OPENHANDS",
    provider: "openai",
    modelId: "gpt-5.6-sol",
  });
  return campaignRepository.ensureCampaign({
    taskId: conversation.taskId,
    conversationId: conversation.conversationId,
    authorizationHash: authorizationHash(),
    authorization: P02_AUTHORIZATION,
    roles: definitions,
  });
}
