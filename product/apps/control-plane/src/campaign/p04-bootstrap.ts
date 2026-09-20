import type { CampaignSummary } from "@q-fintelligence/contracts";

import type { WorkspaceRepository } from "../db/repository.js";
import type { CampaignRepository } from "./repository.js";
import { assertP04Authorization, p04AuthorizationHash, P04_AUTHORIZATION } from "./p04-authorization.js";
import { assertCampaignRoleToolNames, campaignRoleDefinitions } from "./roles.js";

export function ensureP04Campaign(
  workspaceRepository: WorkspaceRepository,
  campaignRepository: CampaignRepository,
): CampaignSummary {
  assertP04Authorization();
  const definitions = campaignRoleDefinitions();
  for (const definition of definitions) assertCampaignRoleToolNames(definition.role, definition.toolNames);
  const authorizationHash = p04AuthorizationHash();
  const existing = campaignRepository.listCampaigns().find((campaign) => campaign.authorizationHash === authorizationHash);
  if (existing) {
    workspaceRepository.assertConversationWritable(existing.conversationId);
    return existing;
  }
  const project = workspaceRepository.listProjects(true).find((item) => item.name === "P04 六小时自治并发验收")
    ?? workspaceRepository.createProject("P04 六小时自治并发验收", "三 Run lane、工具工厂、真实金融与 tianyan176 的 Durable Campaign");
  const conversation = workspaceRepository.createConversation({
    projectId: project.projectId,
    title: "P04 六小时真实金融自治并发 Campaign",
    mode: "OPENHANDS",
    provider: "openai",
    modelId: "gpt-5.6-sol",
  });
  return campaignRepository.ensureCampaign({
    taskId: conversation.taskId,
    conversationId: conversation.conversationId,
    authorizationHash,
    authorization: P04_AUTHORIZATION,
    roles: definitions,
  });
}
