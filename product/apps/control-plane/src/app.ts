// P16 change authorship category: supervisor_infrastructure

import path from "node:path";

import Fastify, { type FastifyInstance, type FastifyReply } from "fastify";

import type {
  AgentUiEvent,
  ApprovalDecisionCommand,
  CreateConversationCommand,
  CreateProjectCommand,
  P05LaunchCommand,
  P05UploadCommand,
  ProjectSourceCommand,
  RenameApplyCommand,
  RenameEvent,
  SendMessageCommand,
  SwitchConversationModelCommand,
  UpdateProjectCommand,
} from "@q-fintelligence/contracts";
import { CONVERSATION_MODEL_OPTIONS, CONTRACT_SCHEMA_VERSION } from "@q-fintelligence/contracts";

import { ProviderReadinessError, ProviderRegistry } from "./agent/providers.js";
import { RuntimeManager } from "./agent/runtime-manager.js";
import { RuntimeControlError, RuntimeFailureError } from "./agent/runtime.js";
import { readArtifact } from "./artifact-store.js";
import { loadRuntimeConfig, type RuntimeConfig } from "./config.js";
import { openDatabase } from "./db/migrations.js";
import { NotFoundError, StateConflictError, WorkspaceRepository } from "./db/repository.js";
import { assertHardwareMutationAuthorized, HardwarePolicyError } from "./hardware-policy.js";
import { RuntimeProcessRegistry } from "./runtime-process-registry.js";
import { safeErrorMessage } from "./security/redaction.js";
import { CampaignRepository } from "./campaign/repository.js";
import { OpenHandsAcceptanceRepository } from "./campaign/openhands-acceptance-repository.js";
import {
  OpenHandsAcceptanceService,
  type OpenHandsAcceptanceLaunchInput,
} from "./campaign/openhands-acceptance-service.js";
import { ensureP04Campaign } from "./campaign/p04-bootstrap.js";
import { P04Repository } from "./campaign/p04-repository.js";
import { P05Repository } from "./p05/p05-repository.js";
import { P05Service } from "./p05/p05-service.js";
import { P07Repository } from "./p07/p07-repository.js";
import { P07Service } from "./p07/p07-service.js";
import { P15Repository } from "./p15/p15-repository.js";
import { P15Service } from "./p15/p15-service.js";
import { P16HardwareRepository } from "./p16/p16-hardware-repository.js";

const idParameters = {
  type: "object",
  additionalProperties: false,
  properties: {
    projectId: { type: "string", minLength: 1 },
    conversationId: { type: "string", minLength: 1 },
    taskId: { type: "string", minLength: 1 },
    sha256: { type: "string", pattern: "^[a-f0-9]{64}$" },
    approvalId: { type: "string", minLength: 1 },
    campaignId: { type: "string", minLength: 1 },
    p05TaskId: { type: "string", minLength: 1 },
    p07CampaignId: { type: "string", minLength: 1 },
    p15CampaignId: { type: "string", minLength: 1 },
    openHandsAcceptanceCampaignId: { type: "string", minLength: 1 },
    sourceId: { type: "string", minLength: 1 },
  },
} as const;

const sendMessageSchema = {
  type: "object",
  additionalProperties: false,
  required: ["content"],
  properties: {
    content: { type: "string", minLength: 1, maxLength: 12_000 },
    intent: { type: "string", enum: ["AUTO", "CHAT", "WORK"] },
  },
} as const;

function isConversationModelOption(provider: string, modelId: string): boolean {
  return CONVERSATION_MODEL_OPTIONS.some((option) => option.provider === provider && option.modelId === modelId);
}

function writeSse(reply: FastifyReply, eventName: string, data: unknown, id?: number): boolean {
  if (reply.raw.destroyed || reply.raw.writableEnded) return false;
  if (id !== undefined) reply.raw.write(`id: ${id}\n`);
  reply.raw.write(`event: ${eventName}\ndata: ${JSON.stringify(data)}\n\n`);
  return !reply.raw.destroyed && !reply.raw.writableEnded;
}

export interface ApplicationOptions {
  projectRoot?: string;
  config?: RuntimeConfig;
  databasePath?: string;
  logger?: boolean;
}

export interface QfApplication {
  app: FastifyInstance;
  repository: WorkspaceRepository;
  runtimeManager: RuntimeManager;
  providers: ProviderRegistry;
  campaignRepository: CampaignRepository;
  openHandsAcceptanceRepository: OpenHandsAcceptanceRepository;
  openHandsAcceptanceService: OpenHandsAcceptanceService;
  p04Repository: P04Repository;
  p05Repository: P05Repository;
  p05Service: P05Service;
  p07Repository: P07Repository;
  p07Service: P07Service;
  p15Repository: P15Repository;
  p15Service: P15Service;
  p16HardwareRepository: P16HardwareRepository;
  config: RuntimeConfig;
}

export function createApplication(options: ApplicationOptions = {}): QfApplication {
  const projectRoot = options.projectRoot ?? process.cwd();
  const config = options.config ?? loadRuntimeConfig();
  const databasePath = options.databasePath ?? path.resolve(projectRoot, config.sqlitePath);
  const migrationResult = openDatabase(databasePath, path.join(projectRoot, "infra", "sqlite"));
  const repository = new WorkspaceRepository(migrationResult.database);
  const campaignRepository = new CampaignRepository(migrationResult.database);
  const openHandsAcceptanceRepository = new OpenHandsAcceptanceRepository(migrationResult.database);
  const p04Repository = new P04Repository(migrationResult.database);
  const p05Repository = new P05Repository(migrationResult.database);
  const p07Repository = new P07Repository(migrationResult.database);
  const p15Repository = new P15Repository(migrationResult.database);
  const p16HardwareRepository = new P16HardwareRepository(migrationResult.database);
  const providers = new ProviderRegistry();
  const runtimeProcessRegistry = process.env.QF_RUNTIME_PROCESS_REGISTRY_REQUIRED === "1"
    ? new RuntimeProcessRegistry(projectRoot)
    : undefined;
  const runtimeManager = new RuntimeManager(repository, {
    ...config,
    artifactRoot: path.resolve(projectRoot, config.artifactRoot),
    openHandsSessionRoot: path.resolve(projectRoot, config.openHandsSessionRoot),
    openHandsSidecarPath: path.resolve(projectRoot, config.openHandsSidecarPath),
  }, providers, projectRoot, p16HardwareRepository, runtimeProcessRegistry);
  const p05Service = new P05Service(
    projectRoot,
    path.resolve(projectRoot, config.artifactRoot),
    repository,
    p05Repository,
    (event) => runtimeManager.events.publish(event),
    config.hardwarePolicy,
  );
  const p07Service = new P07Service(
    projectRoot,
    path.resolve(projectRoot, config.artifactRoot),
    config,
    repository,
    p05Repository,
    p07Repository,
    providers,
  );
  const p15Service = new P15Service(
    projectRoot,
    path.resolve(projectRoot, config.artifactRoot),
    repository,
    p15Repository,
    config.hardwarePolicy,
  );
  const openHandsAcceptanceService = new OpenHandsAcceptanceService(
    repository,
    openHandsAcceptanceRepository,
    config,
  );
  runtimeManager.setP15CampaignLauncher((input) => p15Service.launch(input));
  const app = Fastify({
    bodyLimit: 24 * 1024 * 1024,
    logger: options.logger === false ? false : {
      level: process.env.LOG_LEVEL ?? "info",
      redact: { paths: ["req.headers.authorization", "request.headers.authorization"], censor: "[REDACTED]" },
    },
  });

  app.setErrorHandler((error, _request, reply) => {
    if (error instanceof NotFoundError) {
      return reply.code(404).send({ error: { category: "CONTRACT", code: "NOT_FOUND", message: error.message, retryable: false } });
    }
    if (error instanceof StateConflictError) {
      return reply.code(409).send({ error: { category: "CONFLICT", code: "STATE_CONFLICT", message: error.message, retryable: true } });
    }
    if (error instanceof HardwarePolicyError) {
      return reply.code(409).send({
        error: {
          category: "CONFLICT",
          code: error.code,
          message: error.message,
          retryable: false,
          recovery: "Keep hardware read-only unless the current user starts a separately authorized execution.",
        },
      });
    }
    if (error instanceof RuntimeControlError) {
      return reply.code(409).send({
        error: {
          category: "CONFLICT",
          code: error.code,
          message: error.message,
          retryable: error.retryable,
          recovery: error.recovery,
        },
      });
    }
    if (error instanceof RuntimeFailureError) {
      return reply.code(error.category === "SECURITY" ? 409 : 500).send({
        error: {
          category: error.category,
          code: error.code,
          message: safeErrorMessage(error),
          retryable: error.retryable,
          recovery: error.recovery,
        },
      });
    }
    if (error instanceof ProviderReadinessError) {
      return reply.code(503).send({ error: error.detail });
    }
    const errorRecord = typeof error === "object" && error !== null
      ? error as { validation?: unknown; code?: unknown; name?: unknown }
      : {};
    if (errorRecord.validation) {
      return reply.code(400).send({ error: { category: "SCHEMA", code: "REQUEST_INVALID", message: "Request did not match the qf.v1 API contract.", retryable: false } });
    }
    app.log.error({ code: errorRecord.code, name: errorRecord.name }, "request failed");
    return reply.code(500).send({
      error: {
        category: String(errorRecord.code).includes("SQLITE") ? "SQLITE" : "INTERNAL",
        code: String(errorRecord.code).includes("BUSY") ? "SQLITE_BUSY" : "INTERNAL_ERROR",
        message: safeErrorMessage(error),
        retryable: true,
      },
    });
  });

  app.get("/api/health", async () => ({
    project: config.project,
    status: "ok",
    schemaVersion: CONTRACT_SCHEMA_VERSION,
    processNamespace: config.processNamespace,
    apiPort: config.apiPort,
    webPort: config.webPort,
    timestamp: new Date().toISOString(),
  }));

  app.get("/api/foundation", async () => ({
    phase: "p02-durable-campaign",
    authority: "Fastify + SQLite",
    runtimes: ["Mock", "OpenHands SDK", "Pi historical read-only"],
    deterministicWorkers: ["finance", "quantum", "durable-campaign"],
    persistence: ["SQLite WAL", "SHA-256 artifact store", "OpenHands conversation state"],
    hardBoundaries: [
      "zero OpenHands default file or shell tools",
      "Agent cannot approve its own requests",
      "provider/model selection is explicit, auditable, and never falls back silently",
      "q-fintelligence ports 43187-43192 are forbidden",
      "formal 2024-2026 test interval remains sealed",
      "hardware limits are phase-scoped; P15 permits at most 50 unique circuits per generation with persisted Query IDs",
    ],
  }));

  app.get("/api/p15/campaigns", async () => p15Service.list());

  app.get<{ Params: { p15CampaignId: string } }>("/api/p15/campaigns/:p15CampaignId", {
    schema: { params: { ...idParameters, required: ["p15CampaignId"] } },
  }, async (request) => p15Service.get(request.params.p15CampaignId));

  app.get("/api/projects", async () => ({ projects: repository.listProjects() }));
  app.post<{ Body: CreateProjectCommand }>("/api/projects", {
    schema: {
      body: {
        type: "object",
        additionalProperties: false,
        required: ["name"],
        properties: {
          name: { type: "string", minLength: 1, maxLength: 120 },
          description: { type: "string", maxLength: 2000 },
        },
      },
    },
  }, async (request, reply) => reply.code(201).send(repository.createProject(request.body.name, request.body.description ?? "")));

  app.post("/api/projects/archive-all", async () => repository.archiveAllProjects());

  const publishRenameEvent = (rename: RenameEvent): void => {
    const conversationId = rename.conversationId
      ?? repository.listConversations(rename.projectId).find((conversation) => (
        conversation.mode !== "PI"
        && repository.getSession(conversation.conversationId)?.runtimeKind !== "PI"
      ))?.conversationId
      ?? null;
    if (!conversationId) return;
    const event = repository.appendEvent({
      conversationId,
      type: "rename.completed",
      payload: {
        summary: `${rename.scope === "PROJECT" ? "项目" : "对话"}已从“${rename.oldName}”重命名为“${rename.newName}”`,
        renameEventId: rename.renameEventId,
        scope: rename.scope,
        projectId: rename.projectId,
        conversationId: rename.conversationId,
        oldName: rename.oldName,
        newName: rename.newName,
        actor: rename.actor,
        provider: rename.provider,
        modelId: rename.modelId,
        undoOfEventId: rename.undoOfEventId,
      },
    });
    runtimeManager.events.publish(event);
  };

  const renameApplySchema = {
    type: "object",
    additionalProperties: false,
    required: ["name", "actor"],
    properties: {
      name: { type: "string", minLength: 1, maxLength: 160 },
      actor: { enum: ["HUMAN", "AGENT"] },
      suggestionEventId: { type: "string", minLength: 1 },
    },
  } as const;

  app.get<{ Params: { projectId: string } }>("/api/projects/:projectId/renames", {
    schema: { params: { ...idParameters, required: ["projectId"] } },
  }, async (request) => ({ events: repository.listRenameEvents("PROJECT", request.params.projectId) }));

  app.post<{ Params: { projectId: string } }>("/api/projects/:projectId/rename/suggest", {
    schema: { params: { ...idParameters, required: ["projectId"] } },
  }, async (request) => {
    const context = repository.getRenameContext("PROJECT", request.params.projectId);
    const generated = await providers.generateRenameSuggestion(context);
    const renameEvent = repository.createRenameSuggestion({
      scope: "PROJECT",
      projectId: request.params.projectId,
      newName: generated.suggestion,
      provider: generated.provider,
      modelId: generated.modelId,
      contextSha256: context.contextSha256,
      promptTokens: generated.promptTokens,
      completionTokens: generated.completionTokens,
    });
    return { suggestion: renameEvent.newName, renameEvent };
  });

  app.patch<{ Params: { projectId: string }; Body: RenameApplyCommand }>("/api/projects/:projectId/rename", {
    schema: {
      params: { ...idParameters, required: ["projectId"] },
      body: renameApplySchema,
    },
  }, async (request) => {
    const rename = repository.applyRename({
      scope: "PROJECT",
      subjectId: request.params.projectId,
      ...request.body,
    });
    publishRenameEvent(rename);
    return rename;
  });

  app.post<{ Params: { projectId: string } }>("/api/projects/:projectId/rename/undo", {
    schema: { params: { ...idParameters, required: ["projectId"] } },
  }, async (request) => {
    const rename = repository.undoRename("PROJECT", request.params.projectId);
    publishRenameEvent(rename);
    return rename;
  });

  app.patch<{ Params: { projectId: string }; Body: UpdateProjectCommand }>("/api/projects/:projectId", {
    schema: {
      params: { ...idParameters, required: ["projectId"] },
      body: {
        type: "object",
        additionalProperties: false,
        minProperties: 1,
        properties: {
          name: { type: "string", minLength: 1, maxLength: 120 },
          description: { type: "string", maxLength: 2000 },
          archived: { type: "boolean" },
        },
      },
    },
  }, async (request) => repository.updateProject(request.params.projectId, request.body));

  app.get<{ Params: { projectId: string } }>("/api/projects/:projectId/conversations", {
    schema: { params: { ...idParameters, required: ["projectId"] } },
  }, async (request) => ({ conversations: repository.listConversations(request.params.projectId) }));

  app.post<{ Params: { projectId: string }; Body: CreateConversationCommand }>("/api/projects/:projectId/conversations", {
    schema: {
      params: { ...idParameters, required: ["projectId"] },
      body: {
        type: "object",
        additionalProperties: false,
        required: ["title", "mode"],
        properties: {
          title: { type: "string", minLength: 1, maxLength: 160 },
          mode: { enum: process.env.NODE_ENV === "test" ? ["MOCK", "OPENHANDS"] : ["OPENHANDS"] },
          provider: { type: "string", minLength: 1 },
          modelId: { type: "string", minLength: 1 },
        },
        allOf: [{ if: { properties: { mode: { const: "OPENHANDS" } } }, then: { required: ["provider", "modelId"] } }],
      },
    },
  }, async (request, reply) => {
    if (request.body.mode === "MOCK" && config.agentMode === "openhands") throw new StateConflictError("server is configured for real-agent-only mode");
    if (request.body.mode === "OPENHANDS") {
      if (config.agentMode === "mock") throw new StateConflictError("server is configured for Mock-only mode");
      if (!isConversationModelOption(request.body.provider!, request.body.modelId!)) {
        throw new StateConflictError("the selected model is not exposed for conversations");
      }
    }
    return reply.code(201).send(repository.createConversation({ projectId: request.params.projectId, ...request.body }));
  });

  app.get<{ Params: { conversationId: string } }>("/api/conversations/:conversationId/snapshot", {
    schema: { params: { ...idParameters, required: ["conversationId"] } },
  }, async (request) => repository.getSnapshot(request.params.conversationId));

  app.get<{ Params: { conversationId: string } }>("/api/conversations/:conversationId/renames", {
    schema: { params: { ...idParameters, required: ["conversationId"] } },
  }, async (request) => ({ events: repository.listRenameEvents("CONVERSATION", request.params.conversationId) }));

  app.post<{ Params: { conversationId: string } }>("/api/conversations/:conversationId/rename/suggest", {
    schema: { params: { ...idParameters, required: ["conversationId"] } },
  }, async (request) => {
    const conversation = repository.assertConversationWritable(request.params.conversationId);
    const context = repository.getRenameContext("CONVERSATION", request.params.conversationId);
    const generated = await providers.generateRenameSuggestion(context);
    const renameEvent = repository.createRenameSuggestion({
      scope: "CONVERSATION",
      projectId: conversation.projectId,
      conversationId: conversation.conversationId,
      newName: generated.suggestion,
      provider: generated.provider,
      modelId: generated.modelId,
      contextSha256: context.contextSha256,
      promptTokens: generated.promptTokens,
      completionTokens: generated.completionTokens,
    });
    return { suggestion: renameEvent.newName, renameEvent };
  });

  app.patch<{ Params: { conversationId: string }; Body: RenameApplyCommand }>("/api/conversations/:conversationId/rename", {
    schema: {
      params: { ...idParameters, required: ["conversationId"] },
      body: renameApplySchema,
    },
  }, async (request) => {
    const rename = repository.applyRename({
      scope: "CONVERSATION",
      subjectId: request.params.conversationId,
      ...request.body,
    });
    publishRenameEvent(rename);
    return rename;
  });

  app.post<{ Params: { conversationId: string } }>("/api/conversations/:conversationId/rename/undo", {
    schema: { params: { ...idParameters, required: ["conversationId"] } },
  }, async (request) => {
    const rename = repository.undoRename("CONVERSATION", request.params.conversationId);
    publishRenameEvent(rename);
    return rename;
  });

  app.patch<{ Params: { conversationId: string }; Body: SwitchConversationModelCommand }>(
    "/api/conversations/:conversationId/model",
    {
      schema: {
        params: { ...idParameters, required: ["conversationId"] },
        body: {
          type: "object",
          additionalProperties: false,
          required: ["provider", "modelId", "actor"],
          properties: {
            provider: { enum: ["openai", "deepseek"] },
            modelId: { enum: ["gpt-5.6", "deepseek-v4-pro"] },
            actor: { const: "HUMAN" },
          },
        },
      },
    },
    async (request) => {
      const conversation = repository.assertConversationWritable(request.params.conversationId);
      if (conversation.mode !== "OPENHANDS") {
        throw new StateConflictError("only OpenHands conversations can switch models");
      }
      if (!isConversationModelOption(request.body.provider, request.body.modelId)) {
        throw new StateConflictError("the selected provider/model pair is not exposed for conversations");
      }
      return await runtimeManager.switchModel(
        request.params.conversationId,
        request.body.provider,
        request.body.modelId,
      );
    },
  );

  app.get("/api/campaigns", async () => ({ campaigns: campaignRepository.listCampaigns() }));

  app.get("/api/openhands-acceptance/campaigns", async () => ({ campaigns: openHandsAcceptanceService.list() }));

  app.post<{ Body: OpenHandsAcceptanceLaunchInput }>("/api/openhands-acceptance/campaigns", {
    schema: {
      body: {
        type: "object",
        additionalProperties: false,
        required: [
          "conversationId",
          "fixedProviderModel",
          "minimumWallClockSeconds",
          "minimumOverlapSeconds",
          "hardwareMode",
          "hardwareTarget",
          "maxNewHardwareJobs",
          "shotsPerJob",
          "gitAction",
        ],
        properties: {
          conversationId: { type: "string", minLength: 1 },
          fixedProviderModel: { const: "deepseek/deepseek-v4-pro" },
          minimumWallClockSeconds: { type: "integer", minimum: 7_200 },
          minimumOverlapSeconds: { type: "integer", minimum: 6_900 },
          hardwareMode: { const: "READ_ONLY" },
          hardwareTarget: { const: "tianyan176" },
          maxNewHardwareJobs: { const: 0 },
          shotsPerJob: { const: 0 },
          gitAction: { const: "NONE" },
        },
      },
    },
  }, async (request, reply) => reply.code(201).send(openHandsAcceptanceService.create(request.body)));

  app.get<{ Params: { openHandsAcceptanceCampaignId: string } }>(
    "/api/openhands-acceptance/campaigns/:openHandsAcceptanceCampaignId",
    { schema: { params: { ...idParameters, required: ["openHandsAcceptanceCampaignId"] } } },
    async (request) => openHandsAcceptanceService.get(request.params.openHandsAcceptanceCampaignId),
  );

  app.get<{ Params: { openHandsAcceptanceCampaignId: string } }>(
    "/api/openhands-acceptance/campaigns/:openHandsAcceptanceCampaignId/audit",
    { schema: { params: { ...idParameters, required: ["openHandsAcceptanceCampaignId"] } } },
    async (request) => openHandsAcceptanceService.audit(request.params.openHandsAcceptanceCampaignId),
  );

  app.post("/api/campaigns/p04", async (_request, reply) => {
    const campaign = ensureP04Campaign(repository, campaignRepository);
    repository.assertConversationWritable(campaign.conversationId);
    p04Repository.ensureRuns(campaign.campaignId);
    return reply.code(201).send(p04Repository.getDetail(campaign));
  });

  app.get<{ Params: { campaignId: string } }>("/api/campaigns/:campaignId", {
    schema: { params: { ...idParameters, required: ["campaignId"] } },
  }, async (request) => campaignRepository.getDetail(request.params.campaignId));

  app.get<{ Params: { campaignId: string } }>("/api/campaigns/:campaignId/p04", {
    schema: { params: { ...idParameters, required: ["campaignId"] } },
  }, async (request) => p04Repository.getDetail(campaignRepository.getCampaign(request.params.campaignId)));

  app.get<{ Params: { conversationId: string } }>("/api/conversations/:conversationId/campaigns", {
    schema: { params: { ...idParameters, required: ["conversationId"] } },
  }, async (request) => ({ campaigns: campaignRepository.listCampaignsForConversation(request.params.conversationId) }));

  app.get("/api/p05/tasks", async () => ({ tasks: p05Service.listTasks() }));

  app.get<{ Querystring: { conversationId: string } }>("/api/p05/uploads", {
    schema: {
      querystring: {
        type: "object",
        additionalProperties: false,
        required: ["conversationId"],
        properties: { conversationId: { type: "string", minLength: 1 } },
      },
    },
  }, async (request) => ({ uploads: p05Service.listUploads(request.query.conversationId) }));

  app.post<{ Body: P05UploadCommand }>("/api/p05/uploads", {
    schema: {
      body: {
        type: "object",
        additionalProperties: false,
        required: ["conversationId", "fileName", "bytesBase64"],
        properties: {
          conversationId: { type: "string", minLength: 1 },
          fileName: { type: "string", minLength: 1, maxLength: 240 },
          declaredMediaType: { type: "string", maxLength: 240 },
          bytesBase64: { type: "string", minLength: 1, maxLength: 23_000_000 },
        },
      },
    },
  }, async (request, reply) => reply.code(201).send(await p05Service.upload(request.body)));

  app.get<{ Params: { projectId: string } }>("/api/projects/:projectId/sources", {
    schema: { params: { ...idParameters, required: ["projectId"] } },
  }, async (request) => ({ sources: p05Service.listProjectSources(request.params.projectId) }));

  app.post<{
    Params: { projectId: string };
    Body: ProjectSourceCommand;
  }>("/api/projects/:projectId/sources", {
    schema: {
      params: { ...idParameters, required: ["projectId"] },
      body: {
        type: "object",
        additionalProperties: false,
        required: ["fileName", "bytesBase64"],
        properties: {
          conversationId: { type: "string", minLength: 1 },
          fileName: { type: "string", minLength: 1, maxLength: 240 },
          declaredMediaType: { type: "string", maxLength: 240 },
          bytesBase64: { type: "string", minLength: 1, maxLength: 23_000_000 },
        },
      },
    },
  }, async (request, reply) => reply.code(201).send(
    await p05Service.uploadProjectSource(request.params.projectId, request.body),
  ));

  app.delete<{
    Params: { projectId: string; sourceId: string };
  }>("/api/projects/:projectId/sources/:sourceId", {
    schema: { params: { ...idParameters, required: ["projectId", "sourceId"] } },
  }, async (request, reply) => {
    p05Service.archiveProjectSource(request.params.projectId, request.params.sourceId);
    return reply.code(204).send();
  });

  app.post<{ Body: P05LaunchCommand }>("/api/p05/tasks", {
    schema: {
      body: {
        type: "object",
        additionalProperties: false,
        required: ["conversationId", "objective"],
        properties: {
          conversationId: { type: "string", minLength: 1 },
          objective: { type: "string", minLength: 1, maxLength: 12_000 },
        },
      },
    },
  }, async (request, reply) => reply.code(202).send(await p05Service.launch(request.body)));

  app.get<{ Params: { p05TaskId: string } }>("/api/p05/tasks/:p05TaskId", {
    schema: { params: { ...idParameters, required: ["p05TaskId"] } },
  }, async (request) => p05Service.getTask(request.params.p05TaskId));

  app.post<{ Params: { p05TaskId: string } }>("/api/p05/tasks/:p05TaskId/resume", {
    schema: { params: { ...idParameters, required: ["p05TaskId"] } },
  }, async (request, reply) => reply.code(202).send(p05Service.resume(request.params.p05TaskId)));

  app.get("/api/p07/readiness", async () => ({
    fixedLocks: [
      { provider: "deepseek/getoken", model: "deepseek-v4-pro", credential: process.env.DEEPSEEK_API_KEY && process.env.DEEPSEEK_BASE_URL ? "configured" : "missing" },
      { provider: "openai/getoken", model: "gpt-5.6", credential: process.env.OPENAI_API_KEY && process.env.OPENAI_BASE_URL ? "configured" : "missing" },
    ],
    tushareCredential: process.env.TUSHARE_TOKEN ? "configured" : "missing",
    tianyanCredential: process.env.TIANYAN_CONNECTION_KEY ? "configured" : "missing",
    formalTestSealed: true,
  }));

  app.get("/api/p07/campaigns", async () => ({ campaigns: p07Service.list() }));

  app.post<{ Body: { conversationId: string; objective: string; predecessorCampaignId?: string } }>("/api/p07/campaigns", {
    schema: {
      body: {
        type: "object",
        additionalProperties: false,
        required: ["conversationId", "objective"],
        properties: {
          conversationId: { type: "string", minLength: 1 },
          objective: { type: "string", minLength: 1, maxLength: 12_000 },
          predecessorCampaignId: { type: "string", minLength: 1 },
        },
      },
    },
  }, async (request, reply) => reply.code(201).send(p07Service.launch(request.body)));

  app.get<{ Params: { p07CampaignId: string } }>("/api/p07/campaigns/:p07CampaignId", {
    schema: { params: { ...idParameters, required: ["p07CampaignId"] } },
  }, async (request) => p07Service.get(request.params.p07CampaignId));

  app.post<{ Params: { p07CampaignId: string }; Body: { phrase: string } }>("/api/p07/campaigns/:p07CampaignId/authorize", {
    schema: {
      params: { ...idParameters, required: ["p07CampaignId"] },
      body: {
        type: "object", additionalProperties: false, required: ["phrase"],
        properties: { phrase: { enum: ["AUTHORIZE P07 DEEPSEEK AND TIANYAN176", "AUTHORIZE P07 GPT-5.6 AND REUSE TIANYAN176"] } },
      },
    },
  }, async (request, reply) => reply.code(202).send(p07Service.authorize(request.params.p07CampaignId, request.body.phrase)));

  app.post<{ Params: { p07CampaignId: string } }>("/api/p07/campaigns/:p07CampaignId/resume", {
    schema: { params: { ...idParameters, required: ["p07CampaignId"] } },
  }, async (request, reply) => reply.code(202).send(p07Service.resume(request.params.p07CampaignId)));

  app.post<{ Params: { p07CampaignId: string } }>("/api/p07/campaigns/:p07CampaignId/finalize-blocked", {
    schema: { params: { ...idParameters, required: ["p07CampaignId"] } },
  }, async (request, reply) => reply.code(202).send(await p07Service.finalizeBlocked(request.params.p07CampaignId)));

  app.post<{ Params: { p07CampaignId: string }; Body: { gate: "browser" | "engineering"; status: "PASS" | "FAIL" } }>(
    "/api/p07/campaigns/:p07CampaignId/gates",
    {
      schema: {
        params: { ...idParameters, required: ["p07CampaignId"] },
        body: {
          type: "object", additionalProperties: false, required: ["gate", "status"],
          properties: { gate: { enum: ["browser", "engineering"] }, status: { enum: ["PASS", "FAIL"] } },
        },
      },
    },
    async (request) => p07Service.setGate(request.params.p07CampaignId, request.body),
  );

  app.get<{ Params: { p07CampaignId: string }; Querystring: { after?: string; once?: string } }>(
    "/api/p07/campaigns/:p07CampaignId/events",
    { schema: { params: { ...idParameters, required: ["p07CampaignId"] } } },
    async (request, reply) => {
      const queryAfter = Number(request.query.after ?? 0);
      const headerAfter = Number(request.headers["last-event-id"] ?? 0);
      let lastSent = Math.max(Number.isSafeInteger(queryAfter) && queryAfter >= 0 ? queryAfter : 0,
        Number.isSafeInteger(headerAfter) && headerAfter >= 0 ? headerAfter : 0);
      p07Service.get(request.params.p07CampaignId);
      reply.hijack();
      reply.raw.statusCode = 200;
      reply.raw.setHeader("Content-Type", "text/event-stream; charset=utf-8");
      reply.raw.setHeader("Cache-Control", "no-cache, no-transform");
      reply.raw.setHeader("Connection", "keep-alive");
      const flush = () => {
        for (const event of p07Repository.listEvents(request.params.p07CampaignId, lastSent, 500)) {
          if (!writeSse(reply, String(event.eventType), event, event.sequence)) return false;
          lastSent = event.sequence;
        }
        return true;
      };
      writeSse(reply, "snapshot", p07Service.get(request.params.p07CampaignId));
      flush();
      if (request.query.once === "1") return reply.raw.end();
      const timer = setInterval(() => {
        if (!flush() || !reply.raw.write(`: heartbeat ${Date.now()}\n\n`)) {
          clearInterval(timer);
          if (!reply.raw.writableEnded) reply.raw.end();
        }
      }, 1_000);
      timer.unref();
      request.raw.once("close", () => clearInterval(timer));
    },
  );

  app.get<{ Params: { sha256: string } }>("/api/p07/artifacts/:sha256/download", {
    schema: { params: { ...idParameters, required: ["sha256"] } },
  }, async (request, reply) => {
    const manifest = repository.getArtifactManifest(request.params.sha256);
    const content = await readArtifact({
      root: path.resolve(projectRoot, config.artifactRoot),
      manifest,
      maxBytes: manifest.bytes,
      allowedMediaTypes: [manifest.mediaType],
    });
    return reply.type(manifest.mediaType)
      .header("Content-Disposition", `attachment; filename="${manifest.sha256}"`)
      .header("X-Content-Type-Options", "nosniff")
      .send(Buffer.from(content));
  });

  app.post<{ Params: { conversationId: string }; Body: SendMessageCommand }>("/api/conversations/:conversationId/messages", {
    schema: { params: { ...idParameters, required: ["conversationId"] }, body: sendMessageSchema },
  }, async (request, reply) => {
    repository.assertConversationWritable(request.params.conversationId);
    return reply.code(202).send(await runtimeManager.startPrompt(
      request.params.conversationId,
      request.body.content,
      request.body.intent ?? "AUTO",
    ));
  });

  for (const mode of ["steer", "follow-up"] as const) {
    app.post<{ Params: { conversationId: string }; Body: SendMessageCommand }>(`/api/conversations/:conversationId/${mode}`, {
      schema: { params: { ...idParameters, required: ["conversationId"] }, body: sendMessageSchema },
    }, async (request, reply) => {
      repository.assertConversationWritable(request.params.conversationId);
      await runtimeManager.queue(request.params.conversationId, request.body.content, mode);
      return reply.code(202).send({ accepted: true, mode });
    });
  }

  app.post<{ Params: { conversationId: string } }>("/api/conversations/:conversationId/abort", {
    schema: { params: { ...idParameters, required: ["conversationId"] } },
  }, async (request, reply) => {
    repository.assertConversationWritable(request.params.conversationId);
    await runtimeManager.abort(request.params.conversationId);
    return reply.code(202).send({ accepted: true });
  });

  app.post<{ Params: { conversationId: string } }>("/api/conversations/:conversationId/runtime/recover", {
    schema: { params: { ...idParameters, required: ["conversationId"] } },
  }, async (request) => runtimeManager.recoverOpenHandsRuntime(request.params.conversationId));

  app.get<{ Params: { conversationId: string }; Querystring: { after?: string; once?: string } }>(
    "/api/conversations/:conversationId/events",
    { schema: { params: { ...idParameters, required: ["conversationId"] } } },
    async (request, reply) => {
      const queryAfter = Number(request.query.after ?? 0);
      const headerAfter = Number(request.headers["last-event-id"] ?? 0);
      const after = Math.max(Number.isSafeInteger(queryAfter) && queryAfter >= 0 ? queryAfter : 0, Number.isSafeInteger(headerAfter) && headerAfter >= 0 ? headerAfter : 0);
      repository.getConversation(request.params.conversationId);
      reply.hijack();
      reply.raw.statusCode = 200;
      reply.raw.setHeader("Content-Type", "text/event-stream; charset=utf-8");
      reply.raw.setHeader("Cache-Control", "no-cache, no-transform");
      reply.raw.setHeader("Connection", "keep-alive");
      let lastSent = after;
      let closed = false;
      let replaying = true;
      const pendingLive: AgentUiEvent[] = [];
      let unsubscribe = () => {};
      const close = () => {
        if (closed) return;
        closed = true;
        unsubscribe();
        if (!reply.raw.writableEnded) reply.raw.end();
      };
      if (request.query.once !== "1") {
        unsubscribe = runtimeManager.events.subscribe(request.params.conversationId, (event) => {
          if (closed) return;
          if (replaying) {
            pendingLive.push(event);
            return;
          }
          if (event.sequence !== null && event.sequence <= lastSent) return;
          if (event.sequence !== null) lastSent = event.sequence;
          if (!writeSse(reply, event.type, event, event.sequence ?? undefined)) close();
        });
      }
      writeSse(reply, "snapshot", repository.getSnapshot(request.params.conversationId));
      for (const event of repository.listEventsAfter(request.params.conversationId, after)) {
        if (event.sequence === null || event.sequence <= lastSent) continue;
        lastSent = event.sequence;
        if (!writeSse(reply, event.type, event, event.sequence)) break;
      }
      replaying = false;
      for (const event of pendingLive.sort((left, right) => (left.sequence ?? Number.MAX_SAFE_INTEGER) - (right.sequence ?? Number.MAX_SAFE_INTEGER))) {
        if (event.sequence !== null && event.sequence <= lastSent) continue;
        if (event.sequence !== null) lastSent = event.sequence;
        if (!writeSse(reply, event.type, event, event.sequence ?? undefined)) break;
      }
      if (request.query.once === "1") return close();
      const heartbeat = setInterval(() => {
        if (!closed && !reply.raw.write(`: heartbeat ${Date.now()}\n\n`)) close();
      }, 15_000);
      heartbeat.unref();
      request.raw.once("close", () => {
        clearInterval(heartbeat);
        close();
      });
    },
  );

  app.get<{ Querystring: { refresh?: string } }>("/api/models", async (request) => ({
    models: await providers.listModels(request.query.refresh === "1"),
    note: "Catalog presence, readiness, and explicit task selection are separate states.",
  }));

  app.get<{ Params: { taskId: string } }>("/api/tasks/:taskId/steps", {
    schema: { params: { ...idParameters, required: ["taskId"] } },
  }, async (request) => ({ steps: repository.listSteps(repository.getConversationByTaskId(request.params.taskId).conversationId) }));

  app.get<{ Params: { taskId: string } }>("/api/tasks/:taskId/artifacts", {
    schema: { params: { ...idParameters, required: ["taskId"] } },
  }, async (request) => ({ artifacts: repository.listArtifacts(repository.getConversationByTaskId(request.params.taskId).conversationId) }));

  app.get<{ Params: { sha256: string } }>("/api/artifacts/:sha256/metadata", {
    schema: { params: { ...idParameters, required: ["sha256"] } },
  }, async (request) => repository.getArtifact(request.params.sha256));

  app.get<{ Params: { sha256: string } }>("/api/artifacts/:sha256/content", {
    schema: { params: { ...idParameters, required: ["sha256"] } },
  }, async (request, reply) => {
    const manifest = repository.getArtifactManifest(request.params.sha256);
    if (!repository.getArtifact(request.params.sha256).previewable) {
      throw new StateConflictError("artifact is not eligible for bounded inline preview");
    }
    const content = await readArtifact({
      root: path.resolve(projectRoot, config.artifactRoot),
      manifest,
      maxBytes: 2_000_000,
      allowedMediaTypes: [
        "application/json",
        "text/plain",
        "text/markdown",
        "text/csv",
        "text/vnd.qf.qcis",
        "text/vnd.qcis",
        "image/svg+xml",
        manifest.mediaType.endsWith("+json") ? manifest.mediaType : "application/json",
      ],
    });
    return reply.type(manifest.mediaType).header("X-Content-Type-Options", "nosniff").send(Buffer.from(content));
  });

  app.get<{ Params: { sha256: string } }>("/api/artifacts/:sha256/download", {
    schema: { params: { ...idParameters, required: ["sha256"] } },
  }, async (request, reply) => {
    const manifest = repository.getArtifactManifest(request.params.sha256);
    const content = await readArtifact({
      root: path.resolve(projectRoot, config.artifactRoot),
      manifest,
      maxBytes: manifest.bytes,
      allowedMediaTypes: [manifest.mediaType],
    });
    return reply.type(manifest.mediaType)
      .header("Content-Disposition", `attachment; filename="${manifest.sha256}"`)
      .header("X-Content-Type-Options", "nosniff")
      .send(Buffer.from(content));
  });

  app.post<{ Params: { taskId: string; approvalId: string }; Body: ApprovalDecisionCommand }>(
    "/api/tasks/:taskId/approvals/:approvalId/decision",
    {
      schema: {
        params: { ...idParameters, required: ["taskId", "approvalId"] },
        body: {
          type: "object",
          additionalProperties: false,
          required: ["decision", "actor"],
          properties: { decision: { enum: ["APPROVE", "REJECT"] }, actor: { const: "HUMAN" } },
        },
      },
    },
    async (request) => {
      const approval = repository.getApproval(request.params.approvalId);
      if (approval.taskId !== request.params.taskId) throw new NotFoundError("approval does not belong to this task");
      repository.assertConversationWritable(approval.conversationId);
      if (approval.action === "SUBMIT_HARDWARE" && request.body.decision === "APPROVE") {
        assertHardwareMutationAuthorized(config.hardwarePolicy, {
          operation: "front-end hardware approval",
          authorizationBasis: config.hardwarePolicy.authorizationBasis,
          jobCount: 1,
          shotsPerJob: config.hardwarePolicy.shotsPerJob,
          target: "tianyan176",
        });
      }
      const resolved = repository.decideApproval(request.params.approvalId, request.body.decision, request.body.actor);
      const conversation = repository.getConversationByTaskId(request.params.taskId);
      const event = repository.appendEvent({
        conversationId: conversation.conversationId,
        type: "approval.resolved",
        payload: { approvalId: resolved.approvalId, status: resolved.status },
      });
      runtimeManager.events.publish(event);
      return resolved;
    },
  );

  app.addHook("onClose", async () => {
    await runtimeManager.dispose();
    repository.close();
  });

  if (runtimeProcessRegistry) {
    app.addHook("onReady", async () => {
      await runtimeProcessRegistry.reconcileDeadRegistrations();
    });
  }

  return {
    app,
    repository,
    campaignRepository,
    openHandsAcceptanceRepository,
    openHandsAcceptanceService,
    p04Repository,
    p05Repository,
    p05Service,
    p07Repository,
    p07Service,
    p15Repository,
    p15Service,
    p16HardwareRepository,
    runtimeManager,
    providers,
    config,
  };
}
