// P16 change authorship category: supervisor_infrastructure

import { randomUUID } from "node:crypto";

import type { AgentUiEvent, ConversationSummary, JsonObject, RuntimeMode, StructuredError } from "@q-fintelligence/contracts";
import { CONTRACT_SCHEMA_VERSION, toAgentWorkspaceEvent } from "@q-fintelligence/contracts";

import type { RuntimeConfig } from "../config.js";
import { StateConflictError, type WorkspaceRepository } from "../db/repository.js";
import { redactSecrets, safeErrorMessage } from "../security/redaction.js";
import type { P16HardwareRepository } from "../p16/p16-hardware-repository.js";
import { MockRuntime } from "./mock-runtime.js";
import {
  OpenHandsRuntime,
  type OpenHandsExactRecoveryExpectation,
  type OpenHandsRuntimeOptions,
  type OpenHandsSidecarProcessIdentity,
} from "./openhands-runtime.js";
import type { ProviderRegistry } from "./providers.js";
import type { AgentRuntime, RuntimeEmission } from "./runtime.js";
import { RuntimeAbortedError, RuntimeFailureError } from "./runtime.js";
import { QF_TOOL_NAMES, QfToolHost } from "./tools.js";
import { buildFullQfSystemPrompt, systemPromptHash } from "./system-prompt.js";

type EventListener = (event: AgentUiEvent) => void;

function sanitizePayload(input: JsonObject): JsonObject {
  const sensitiveKey = (key: string) => /(?:^|[_-])(api[_-]?key|token|connection[_-]?key|cookie|authorization|auth[_-]?header)(?:$|[_-])/iu.test(key);
  const visit = (value: JsonObject[string]): JsonObject[string] => {
    if (typeof value === "string") return redactSecrets(value);
    if (Array.isArray(value)) return value.map(visit);
    if (value && typeof value === "object") {
      return Object.fromEntries(Object.entries(value).map(([key, entry]) => [key, sensitiveKey(key) ? "[REDACTED]" : visit(entry)]));
    }
    return value;
  };
  return Object.fromEntries(Object.entries(input).map(([key, value]) => [key, sensitiveKey(key) ? "[REDACTED]" : visit(value)]));
}

function failureDetail(error: unknown): StructuredError {
  if (error instanceof RuntimeFailureError) {
    return {
      category: error.category,
      code: error.code,
      message: safeErrorMessage(error),
      retryable: error.retryable,
      recovery: error.recovery,
    };
  }
  return {
    category: "INTERNAL",
    code: "AGENT_RUNTIME_FAILED",
    message: safeErrorMessage(error),
    retryable: true,
    recovery: "Inspect the registered events and retry the same explicitly selected runtime.",
  };
}

export class ConversationEventHub {
  private readonly listeners = new Map<string, Set<EventListener>>();

  publish(event: AgentUiEvent): void {
    for (const listener of this.listeners.get(event.conversationId) ?? []) listener(event);
  }

  subscribe(conversationId: string, listener: EventListener): () => void {
    const listeners = this.listeners.get(conversationId) ?? new Set<EventListener>();
    listeners.add(listener);
    this.listeners.set(conversationId, listeners);
    return () => {
      listeners.delete(listener);
      if (listeners.size === 0) this.listeners.delete(conversationId);
    };
  }
}

interface ActiveRun {
  runtime: AgentRuntime;
  completion: Promise<void>;
  aborted: boolean;
}

export interface RuntimeManagedProcessRegistry {
  register(identity: OpenHandsSidecarProcessIdentity): Promise<void>;
  unregister(processKey: string, expectedPid: number): Promise<void>;
}

export interface OpenHandsRuntimeRecoverySummary extends JsonObject {
  recovered: true;
  restarted: true;
  conversationId: string;
  runtimeSessionId: string;
  runtimeRevision: string;
  configHash: string;
  recoveryCursor: number;
  processKey: string;
  processId: number;
  providerCallsAdded: 0;
  externalActionsReplayed: false;
}

export class RuntimeManager {
  readonly events = new ConversationEventHub();
  private readonly runtimes = new Map<string, AgentRuntime>();
  private readonly active = new Map<string, ActiveRun>();
  private readonly starting = new Set<string>();
  private startP15Campaign?: (input: { conversationId: string; authorizationBasis: string }) => JsonObject;

  constructor(
    private readonly repository: WorkspaceRepository,
    private readonly config: RuntimeConfig,
    private readonly providers: ProviderRegistry,
    private readonly projectRoot: string,
    private readonly p16Hardware?: P16HardwareRepository,
    private readonly processRegistry?: RuntimeManagedProcessRegistry,
  ) {}

  private createToolHost(conversationId: string): QfToolHost {
    return new QfToolHost(
      this.repository,
      conversationId,
      this.config.artifactRoot,
      this.projectRoot,
      (event) => this.events.publish(event),
      this.startP15Campaign,
      this.p16Hardware,
      this.config.hardwarePolicy,
    );
  }

  private currentSystemPrompt(): string {
    return buildFullQfSystemPrompt(this.config.hardwarePolicy);
  }

  private openHandsRuntimeOptions(
    conversation: ConversationSummary,
    provider = conversation.provider!,
    modelId = conversation.modelId!,
  ): OpenHandsRuntimeOptions {
    const { baseUrl, apiKey } = this.providers.runtimeConfiguration(provider);
    const systemPrompt = this.currentSystemPrompt();
    return {
      projectRoot: this.projectRoot,
      sessionRoot: this.config.openHandsSessionRoot,
      sidecarPath: this.config.openHandsSidecarPath,
      ...(this.config.openHandsSidecarArguments === undefined
        ? {}
        : { sidecarArguments: this.config.openHandsSidecarArguments }),
      promptVersion: this.config.systemPromptVersion,
      provider,
      modelId,
      baseUrl,
      apiKey,
      systemPrompt,
      systemPromptHash: systemPromptHash(systemPrompt),
      repository: this.repository,
      host: this.createToolHost(conversation.conversationId),
      conversationId: conversation.conversationId,
      ...(this.processRegistry === undefined ? {} : {
        onSidecarStarted: (identity: OpenHandsSidecarProcessIdentity) => this.processRegistry!.register(identity),
        onSidecarExited: (identity: OpenHandsSidecarProcessIdentity) => this.processRegistry!.unregister(
          identity.processKey,
          identity.pid,
        ),
      }),
    };
  }

  private async createOpenHandsRuntime(
    conversation: ConversationSummary,
    provider = conversation.provider!,
    modelId = conversation.modelId!,
  ): Promise<OpenHandsRuntime> {
    return OpenHandsRuntime.create(this.openHandsRuntimeOptions(conversation, provider, modelId));
  }

  private async prepareExactOpenHandsRecovery(
    conversation: ConversationSummary,
    expected: OpenHandsExactRecoveryExpectation,
  ): Promise<OpenHandsRuntime> {
    return OpenHandsRuntime.prepareExactRecovery(
      this.openHandsRuntimeOptions(conversation),
      expected,
    );
  }

  setP15CampaignLauncher(
    launcher: (input: { conversationId: string; authorizationBasis: string }) => JsonObject,
  ): void {
    this.startP15Campaign = launcher;
  }

  private async runtimeFor(conversation: ConversationSummary): Promise<AgentRuntime> {
    const existing = this.runtimes.get(conversation.conversationId);
    if (existing) return existing;
    if (conversation.mode === "PI") {
      throw new StateConflictError("legacy Pi conversations are read-only; create a new OpenHands conversation in the same project");
    }
    const host = this.createToolHost(conversation.conversationId);
    let runtime: AgentRuntime;
    if (conversation.mode === "MOCK") {
      if (process.env.NODE_ENV !== "test") {
        throw new StateConflictError("Mock conversations are historical/test-only; create an OpenHands conversation");
      }
      runtime = new MockRuntime(host);
      this.repository.upsertSession({
        conversationId: conversation.conversationId,
        sessionId: `mock_${conversation.conversationId}`,
        relativeSessionFile: null,
        provider: "mock",
        modelId: "qf-mock-v1",
        promptVersion: this.config.systemPromptVersion,
        promptHash: systemPromptHash(this.currentSystemPrompt()),
        lifecycleState: "READY",
      });
    } else runtime = await this.createOpenHandsRuntime(conversation);
    if (JSON.stringify([...runtime.toolNames].sort()) !== JSON.stringify([...QF_TOOL_NAMES].sort())) {
      runtime.dispose();
      throw new Error("runtime tool allowlist assertion failed");
    }
    this.runtimes.set(conversation.conversationId, runtime);
    return runtime;
  }

  async recoverOpenHandsRuntime(conversationId: string): Promise<OpenHandsRuntimeRecoverySummary> {
    if (this.active.has(conversationId) || this.starting.has(conversationId)) {
      throw new StateConflictError("cannot recover the OpenHands sidecar while a prompt is active");
    }
    const conversation = this.repository.assertConversationWritable(conversationId);
    if (conversation.mode !== "OPENHANDS") {
      throw new StateConflictError("only OpenHands conversations have a recoverable sidecar");
    }
    const before = this.repository.getSession(conversationId);
    if (!before || before.runtimeKind !== "OPENHANDS") {
      throw new StateConflictError("OpenHands recovery requires an existing durable runtime session");
    }
    const cached = this.runtimes.get(conversationId);
    if (cached !== undefined && !(cached instanceof OpenHandsRuntime)) {
      throw new StateConflictError("the cached runtime kind does not match the durable OpenHands session");
    }
    if (cached instanceof OpenHandsRuntime && cached.sidecarProcess !== null) {
      throw new StateConflictError("OpenHands sidecar is still running; recovery requires a verified stopped process");
    }
    this.starting.add(conversationId);
    let runtime: OpenHandsRuntime | null = cached ?? null;
    let retryStarted = false;
    try {
      runtime ??= await this.prepareExactOpenHandsRecovery(conversation, before);
      const preparedSession = this.repository.getSession(conversationId);
      if (!preparedSession || JSON.stringify(preparedSession) !== JSON.stringify(before)) {
        throw new StateConflictError("OpenHands durable runtime identity changed before exact recovery started");
      }
      const beforeSnapshot = this.repository.getSnapshot(conversationId);
      const beforeToolCalls = this.repository.listOpenHandsToolCallsForRecovery(conversationId, before.sessionId);
      const started = this.repository.appendEvent({
        conversationId,
        type: "retry.started",
        payload: {
          summary: "正在严格恢复同一 OpenHands session、revision、配置与 cursor",
          stage: "sidecar_recovery",
          recoveryMode: "RECOVER_EXACT",
          runtimeSessionId: before.sessionId,
          runtimeRevision: before.runtimeRevision,
          configHash: before.configHash,
          recoveryCursor: before.recoveryCursor,
          providerRequestCreated: false,
          externalActionsReplayed: false,
        },
      });
      retryStarted = true;
      this.events.publish(started);
      const recovered = await runtime.recoverSidecar();
      const after = this.repository.getSession(conversationId);
      if (
        !after
        || after.runtimeKind !== "OPENHANDS"
        || after.sessionId !== before.sessionId
        || after.runtimeRevision !== before.runtimeRevision
        || after.configHash !== before.configHash
        || after.provider !== before.provider
        || after.modelId !== before.modelId
        || after.promptVersion !== before.promptVersion
        || after.promptHash !== before.promptHash
        || after.relativeSessionFile !== before.relativeSessionFile
        || after.lifecycleState !== before.lifecycleState
        || after.recoveryCursor !== before.recoveryCursor
        || recovered.providerPromptRequestsThisProcess !== 0
      ) {
        throw new Error("OpenHands recovery changed the durable runtime identity");
      }
      const afterSnapshot = this.repository.getSnapshot(conversationId);
      const afterToolCalls = this.repository.listOpenHandsToolCallsForRecovery(conversationId, before.sessionId);
      for (const key of ["messages", "steps", "artifacts", "approvals", "session"] as const) {
        if (JSON.stringify(beforeSnapshot[key]) !== JSON.stringify(afterSnapshot[key])) {
          throw new Error(`OpenHands recovery changed durable ${key}`);
        }
      }
      if (JSON.stringify(beforeToolCalls) !== JSON.stringify(afterToolCalls)) {
        throw new Error("OpenHands recovery changed the durable tool-call inbox");
      }
      if (cached === undefined) this.runtimes.set(conversationId, runtime);
      const completed = this.repository.appendEvent({
        conversationId,
        type: "retry.completed",
        payload: {
          summary: "OpenHands sidecar 已复用原 session、revision、配置与恢复游标",
          stage: "sidecar_recovery",
          success: true,
          runtimeSessionId: after.sessionId,
          runtimeRevision: after.runtimeRevision,
          configHash: after.configHash,
          recoveryCursor: after.recoveryCursor,
          processKey: recovered.process.processKey,
          processId: recovered.process.pid,
          executionStatus: recovered.executionStatus,
          sdkEventCount: recovered.sdkEventCount,
          providerCallsAdded: recovered.providerPromptRequestsThisProcess,
          providerRequestCreated: false,
          externalActionsReplayed: false,
        },
      });
      this.events.publish(completed);
      return {
        recovered: true,
        restarted: true,
        conversationId,
        runtimeSessionId: after.sessionId,
        runtimeRevision: after.runtimeRevision,
        configHash: after.configHash,
        recoveryCursor: after.recoveryCursor,
        processKey: recovered.process.processKey,
        processId: recovered.process.pid,
        providerCallsAdded: recovered.providerPromptRequestsThisProcess,
        externalActionsReplayed: false,
      };
    } catch (error) {
      if (runtime !== null && cached === undefined) {
        runtime.dispose();
        if (this.runtimes.get(conversationId) === runtime) this.runtimes.delete(conversationId);
      }
      if (retryStarted) {
        const failed = this.repository.appendEvent({
          conversationId,
          type: "retry.completed",
          payload: {
            summary: "OpenHands 严格恢复失败，未创建 Provider 请求或重放外部动作",
            stage: "sidecar_recovery",
            recoveryMode: "RECOVER_EXACT",
            success: false,
            code: "OPENHANDS_RECOVER_EXACT_FAILED",
            message: safeErrorMessage(error),
            providerCallsAdded: 0,
            providerRequestCreated: false,
            externalActionsReplayed: false,
          },
        });
        this.events.publish(failed);
      }
      throw error;
    } finally {
      this.starting.delete(conversationId);
    }
  }

  private emit(conversation: ConversationSummary, emission: RuntimeEmission): AgentUiEvent {
    const payload = sanitizePayload(emission.payload);
    const event = emission.persistent
      ? this.repository.appendEvent({ conversationId: conversation.conversationId, type: emission.type, payload })
      : (() => {
        const transient: AgentUiEvent = {
            schemaVersion: CONTRACT_SCHEMA_VERSION,
            eventId: `transient_${randomUUID()}`,
            conversationId: conversation.conversationId,
            taskId: conversation.taskId,
            runId: this.repository.getRunId(conversation.conversationId),
            sequence: null,
            persistent: false,
            type: emission.type,
            payload,
            createdAt: new Date().toISOString(),
          };
          return { ...transient, workspace: toAgentWorkspaceEvent(transient) };
        })();
    this.events.publish(event);
    return event;
  }

  async switchModel(conversationId: string, provider: string, modelId: string): Promise<ConversationSummary> {
    const conversation = this.repository.assertConversationWritable(conversationId);
    if (this.active.has(conversationId) || this.starting.has(conversationId)) {
      throw new StateConflictError("cannot switch models while a prompt is active");
    }
    if (conversation.mode !== "OPENHANDS") throw new StateConflictError("only OpenHands conversations can switch models");
    if (conversation.provider === provider && conversation.modelId === modelId) return conversation;

    this.starting.add(conversationId);
    try {
      const previousRuntime = this.runtimes.get(conversationId);
      const replacement = await this.createOpenHandsRuntime(conversation, provider, modelId);
      previousRuntime?.dispose();
      this.runtimes.set(conversationId, replacement);
      const updated = this.repository.updateConversationModel(conversationId, provider, modelId);
      const event = this.repository.appendEvent({
        conversationId,
        type: "model.changed",
        payload: {
          summary: `会话模型已切换为 ${provider}/${modelId}`,
          previousProvider: conversation.provider,
          previousModelId: conversation.modelId,
          provider,
          modelId,
          actor: "HUMAN",
          conversationId,
          sessionId: replacement.runtimeSessionId,
          runtimeRevision: replacement.runtimeRevision,
          configHash: replacement.configHash,
        },
      });
      this.events.publish(event);
      return updated;
    } finally {
      this.starting.delete(conversationId);
    }
  }

  async startPrompt(
    conversationId: string,
    content: string,
    intent: "AUTO" | "CHAT" | "WORK" = "AUTO",
  ): Promise<{ accepted: true; messageId: string }> {
    const conversation = this.repository.assertConversationWritable(conversationId);
    if (this.active.has(conversationId) || this.starting.has(conversationId)) {
      throw new StateConflictError("conversation already has an active prompt");
    }
    this.starting.add(conversationId);
    let runtime: AgentRuntime;
    try {
      runtime = await this.runtimeFor(conversation);
    } catch (error) {
      this.starting.delete(conversationId);
      throw error;
    }
    const userMessage = this.repository.addMessage({ conversationId, role: "USER", content: redactSecrets(content) });
    const routedContent = intent === "CHAT"
      ? `输入路由：CHAT。仅回答用户问题，不创建新的执行 Run，不调用工具，不申请审批。\n\n用户原始请求：\n${content}`
      : intent === "WORK"
        ? `输入路由：WORK。把用户请求作为可检验工作任务执行；遵守系统提示中的工具、证据、审批和外部提交边界。\n\n用户原始请求：\n${content}`
        : content;
    const selectedToolNames = intent === "CHAT" ? [] : [...QF_TOOL_NAMES];
    this.repository.setConversationState(conversationId, "RUNNING");
    this.emit(conversation, {
      type: "run.started",
      payload: {
        summary: intent === "CHAT"
          ? "OpenHands 对话已建立，不创建新的工具执行节点"
          : "OpenHands 真实 Agent 运行已建立，事件将持续增量写入",
        stage: "provider_request",
        provider: conversation.provider,
        modelId: conversation.modelId,
        promptIntent: intent,
      },
      persistent: true,
    });
    this.emit(conversation, {
      type: "model.request",
      payload: {
        summary: `调用固定模型 ${conversation.provider}/getoken/${conversation.modelId}`,
        provider: conversation.provider,
        modelId: conversation.modelId,
        systemPromptVersion: this.config.systemPromptVersion,
        systemPromptHash: systemPromptHash(this.currentSystemPrompt()),
        messages: [{ role: "user", content: redactSecrets(content) }],
        tools: selectedToolNames,
        streaming: true,
        sensitive: true,
        promptIntent: intent,
      },
      persistent: true,
    });
    let activeRun: ActiveRun;
    const runStartedAt = Date.now();
    let lastRealEventAt = new Date(runStartedAt).toISOString();
    let runtimeFailurePersisted = false;
    let heartbeat: ReturnType<typeof setInterval> | null = null;
    const completion = runtime.prompt(routedContent, async (emission) => {
      if (emission.type === "run.failed") {
        if (runtimeFailurePersisted) return;
        runtimeFailurePersisted = true;
      }
      if (emission.type !== "run.heartbeat") lastRealEventAt = new Date().toISOString();
      this.emit(conversation, emission);
    }, { toolNames: selectedToolNames }).then((result) => {
      if (activeRun.aborted) return;
      if (result.usage !== undefined) {
        this.emit(conversation, {
          type: "model.usage",
          payload: {
            summary: `Provider 核验本轮 ${result.usage.totalTokens.toLocaleString()} Tokens`,
            provider: result.provider,
            modelId: result.modelId,
            ...result.usage,
          },
          persistent: true,
        });
      }
      const message = this.repository.addMessage({
        conversationId,
        role: "ASSISTANT",
        content: redactSecrets(result.assistantContent),
        provider: result.provider,
        modelId: result.modelId,
        piMessageId: result.runtimeMessageId,
      });
      this.emit(conversation, {
        type: "message.completed",
        payload: { messageId: message.messageId, role: message.role },
        persistent: true,
      });
      this.repository.setConversationState(conversationId, "COMPLETED");
      this.emit(conversation, {
        type: "run.completed",
        payload: { summary: "本轮 Agent 运行与工件登记已完成", stage: "result_ready" },
        persistent: true,
      });
    }).catch((error: unknown) => {
      if (activeRun.aborted || error instanceof RuntimeAbortedError) {
        this.repository.setConversationState(conversationId, "ABORTED");
        return;
      }
      const detail = failureDetail(error);
      if (!runtimeFailurePersisted) {
        runtimeFailurePersisted = true;
        this.emit(conversation, {
          type: "run.failed",
          payload: {
            category: detail.category,
            code: detail.code,
            message: detail.message,
            retryable: detail.retryable,
            recovery: detail.recovery ?? "Inspect the registered failure event.",
            artifactHashes: detail.artifactHashes ?? [],
          },
          persistent: true,
        });
      }
      this.repository.setConversationState(conversationId, "FAILED");
    }).finally(() => {
      if (heartbeat !== null) clearInterval(heartbeat);
      this.active.delete(conversationId);
    });
    heartbeat = setInterval(() => {
      this.emit(conversation, {
        type: "run.heartbeat",
        payload: {
          summary: "等待固定 Provider 或当前科学工具返回",
          stage: "agent_work",
          waitingFor: `${conversation.provider}/getoken/${conversation.modelId}`,
          elapsedMs: Date.now() - runStartedAt,
          lastRealEventAt,
          nextPollAt: new Date(Date.now() + 5_000).toISOString(),
          connectionStatus: "active",
        },
        persistent: true,
      });
    }, 5_000);
    heartbeat.unref();
    activeRun = { runtime, completion, aborted: false };
    this.active.set(conversationId, activeRun);
    this.starting.delete(conversationId);
    return { accepted: true, messageId: userMessage.messageId };
  }

  async queue(conversationId: string, content: string, mode: "steer" | "follow-up"): Promise<void> {
    this.repository.assertConversationWritable(conversationId);
    const active = this.active.get(conversationId);
    if (!active) throw new StateConflictError("conversation has no active prompt");
    if (mode === "steer") await active.runtime.steer(content);
    else await active.runtime.followUp(content);
    this.repository.addMessage({ conversationId, role: "USER", content: redactSecrets(content) });
  }

  async abort(conversationId: string): Promise<void> {
    this.repository.assertConversationWritable(conversationId);
    if (this.starting.has(conversationId)) {
      throw new StateConflictError("cannot abort while the runtime is starting or recovering");
    }
    const active = this.active.get(conversationId);
    if (!active) {
      const conversation = this.repository.getConversation(conversationId);
      if (conversation.state !== "RUNNING") {
        throw new StateConflictError("conversation has no active prompt");
      }
      this.repository.setConversationState(conversationId, "ABORTED");
      this.emit(conversation, {
        type: "agent.aborted",
        payload: {
          summary: "运行时已重启且没有可恢复的活动 Provider 回合；已仅收敛会话状态，可在同一会话继续。",
          stage: "stale_runtime_reconciled",
          code: "STALE_RUNTIME_RECONCILED",
          externalRequestsResubmitted: false,
        },
        persistent: true,
      });
      return;
    }
    active.aborted = true;
    await active.runtime.abort();
    this.repository.setConversationState(conversationId, "ABORTED");
  }

  isActive(conversationId: string): boolean {
    return this.active.has(conversationId) || this.starting.has(conversationId);
  }

  async waitForIdle(conversationId: string): Promise<void> {
    await this.active.get(conversationId)?.completion;
  }

  async dispose(): Promise<void> {
    await Promise.allSettled([...this.active.values()].map((run) => run.runtime.abort()));
    await Promise.all([...this.runtimes.values()].map(async (runtime) => {
      if (runtime.shutdown) await runtime.shutdown();
      else runtime.dispose();
    }));
    this.runtimes.clear();
  }
}
