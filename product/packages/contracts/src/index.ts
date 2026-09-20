export const CONTRACT_SCHEMA_VERSION = "qf.v1" as const;

export const TASK_STATES = [
  "DRAFT",
  "SPECIFIED",
  "APPROVED",
  "RUNNING",
  "PAUSED",
  "BLOCKED",
  "FAILED",
  "VALIDATED",
  "DELIVERED",
] as const;

export type TaskState = (typeof TASK_STATES)[number];

export interface ModelSelection {
  provider: string;
  modelId: string;
  selectedAt: string;
  capabilityCheckId: string;
}

export interface TaskSpec {
  schemaVersion: typeof CONTRACT_SCHEMA_VERSION;
  taskId: string;
  scientificQuestion: string;
  asOfDate: string;
  state: TaskState;
  model: ModelSelection;
  protocolHash?: string;
}

export interface ProtocolSpec {
  schemaVersion: typeof CONTRACT_SCHEMA_VERSION;
  protocolId: string;
  taskId: string;
  asOfDate: string;
  universe: {
    assetCount: 6;
    selectionPeriod: "2019";
    selectionRule: "historical-hs300-cross-industry-liquidity";
  };
  split: {
    strategy: "time";
    train: { start: "2020-01-01"; end: "2022-12-31" };
    validation: { start: "2023-01-01"; end: "2023-12-31" };
    test: { start: "2024-01-01"; end: string };
  };
  label: {
    name: "future_5d_downside_realized_risk";
    horizonTradingDays: 5;
    nonNegative: true;
  };
  riskGraph: {
    method: "generalized-fevd";
    varOrder: 1;
    windowTradingDays: 120;
    regularization: "ridge";
    directed: true;
  };
  portfolio: {
    assetCount: 6;
    selectedCount: 3;
    weighting: "equal";
  };
  researchIterationLimit: 5;
  testPolicy: "sealed-single-execution";
}

export interface ArtifactManifest {
  schemaVersion: typeof CONTRACT_SCHEMA_VERSION;
  sha256: string;
  bytes: number;
  mediaType: string;
  relativePath: string;
  createdAt: string;
  producer: string;
  parentHashes: string[];
}

export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonObject | JsonValue[];
export interface JsonObject {
  [key: string]: JsonValue;
}

export const RUNTIME_MODES = ["MOCK", "OPENHANDS", "PI"] as const;
export type RuntimeMode = (typeof RUNTIME_MODES)[number];
export type CreateConversationMode = Exclude<RuntimeMode, "PI">;

export const CONVERSATION_STATES = [
  "DRAFT",
  "RUNNING",
  "PAUSED",
  "BLOCKED",
  "FAILED",
  "COMPLETED",
  "ABORTED",
] as const;
export type ConversationState = (typeof CONVERSATION_STATES)[number];

export const STEP_STATES = ["PENDING", "RUNNING", "COMPLETED", "FAILED", "BLOCKED", "ABORTED"] as const;
export type StepState = (typeof STEP_STATES)[number];

export const AGENT_UI_EVENT_TYPES = [
  "agent.started",
  "agent.completed",
  "agent.aborted",
  "turn.started",
  "turn.completed",
  "assistant.delta",
  "tool.started",
  "tool.progress",
  "tool.completed",
  "step.changed",
  "artifact.created",
  "approval.requested",
  "approval.resolved",
  "run.blocked",
  "run.failed",
  "run.started",
  "run.heartbeat",
  "run.completed",
  "security.quarantined",
  "model.changed",
  "rename.completed",
  "model.request",
  "model.usage",
  "hardware.batch",
  "message.completed",
  "queue.changed",
  "retry.started",
  "retry.completed",
  "compaction.started",
  "compaction.completed",
] as const;
export type AgentUiEventType = (typeof AGENT_UI_EVENT_TYPES)[number];

export interface StructuredError {
  category: "PROVIDER" | "MODEL" | "TOOL" | "WORKER" | "SCHEMA" | "SQLITE" | "SECURITY" | "CONFLICT" | "ABORTED" | "INTERNAL";
  code: string;
  message: string;
  retryable: boolean;
  recovery?: string;
  artifactHashes?: string[];
}

export interface ProjectSummary {
  schemaVersion: typeof CONTRACT_SCHEMA_VERSION;
  projectId: string;
  name: string;
  description: string;
  archived: boolean;
  createdAt: string;
  updatedAt: string;
}

export interface ConversationSummary {
  schemaVersion: typeof CONTRACT_SCHEMA_VERSION;
  conversationId: string;
  projectId: string;
  taskId: string;
  title: string;
  mode: RuntimeMode;
  state: ConversationState;
  provider: string | null;
  modelId: string | null;
  archived: boolean;
  lastActivityAt: string;
  createdAt: string;
}

export interface ArchiveHistoryResult {
  projectsArchived: number;
  conversationsArchived: number;
  archivedAt: string;
}

export type RenameScope = "PROJECT" | "CONVERSATION";

export interface RenameEvent {
  schemaVersion: typeof CONTRACT_SCHEMA_VERSION;
  renameEventId: string;
  scope: RenameScope;
  projectId: string;
  conversationId: string | null;
  oldName: string;
  newName: string;
  actor: "HUMAN" | "AGENT";
  provider: string | null;
  modelId: string | null;
  contextSha256: string | null;
  promptTokens: number;
  completionTokens: number;
  status: "SUGGESTED" | "APPLIED" | "SUPERSEDED";
  undoOfEventId: string | null;
  createdAt: string;
  appliedAt: string | null;
}

export interface RenameSuggestion {
  suggestion: string;
  renameEvent: RenameEvent;
}

export interface RenameApplyCommand {
  name: string;
  actor: "HUMAN" | "AGENT";
  suggestionEventId?: string;
}

export interface MessageRecord {
  schemaVersion: typeof CONTRACT_SCHEMA_VERSION;
  messageId: string;
  conversationId: string;
  role: "USER" | "ASSISTANT" | "SYSTEM";
  content: string;
  provider: string | null;
  modelId: string | null;
  piMessageId: string | null;
  createdAt: string;
}

export interface AgentUiEvent {
  schemaVersion: typeof CONTRACT_SCHEMA_VERSION;
  eventId: string;
  conversationId: string;
  taskId: string;
  runId: string | null;
  sequence: number | null;
  persistent: boolean;
  type: AgentUiEventType;
  payload: JsonObject;
  createdAt: string;
  workspace?: AgentWorkspaceEvent;
}

export const AGENT_WORKSPACE_EVENT_KINDS = [
  "task_started",
  "stage_changed",
  "agent_activity",
  "stream_delta",
  "tool_started",
  "tool_progress",
  "tool_completed",
  "tool_failed",
  "waiting",
  "heartbeat",
  "retrying",
  "recovered",
  "artifact_created",
  "result_ready",
  "blocked",
  "stopped",
  "completed",
] as const;
export type AgentWorkspaceEventKind = (typeof AGENT_WORKSPACE_EVENT_KINDS)[number];

export type AgentWorkspaceSensitivity = "PUBLIC" | "INTERNAL" | "SENSITIVE_REDACTED";

export interface AgentWorkspaceEvent {
  schemaVersion: "qf.workspace-event.v1";
  sourceEventId: string;
  kind: AgentWorkspaceEventKind;
  taskId: string;
  conversationId: string;
  runId: string | null;
  agentId: string | null;
  sequence: number | null;
  timestamp: string;
  summary: string;
  stage: string | null;
  detailRefs: {
    eventId: string;
    runId: string | null;
    artifactHashes: string[];
    queryId: string | null;
  };
  sensitivity: AgentWorkspaceSensitivity;
}

function payloadText(payload: JsonObject, key: string): string | null {
  const value = payload[key];
  return typeof value === "string" && value.trim() !== "" ? value : null;
}

function payloadStrings(payload: JsonObject, key: string): string[] {
  const value = payload[key];
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

function workspaceKind(event: Pick<AgentUiEvent, "type" | "payload">): AgentWorkspaceEventKind {
  if (event.type === "run.started" || event.type === "agent.started") return "task_started";
  if (event.type === "step.changed") return "stage_changed";
  if (event.type === "assistant.delta") return "stream_delta";
  if (event.type === "tool.started") return "tool_started";
  if (event.type === "tool.progress") return "tool_progress";
  if (event.type === "tool.completed") return event.payload.isError === true ? "tool_failed" : "tool_completed";
  if (event.type === "run.heartbeat") return "heartbeat";
  if (event.type === "queue.changed") return "waiting";
  if (event.type === "retry.started") return "retrying";
  if (event.type === "retry.completed") return event.payload.success === false ? "retrying" : "recovered";
  if (event.type === "artifact.created") return "artifact_created";
  if (event.type === "message.completed") return "result_ready";
  if (event.type === "run.blocked" || event.type === "approval.requested") return "blocked";
  if (event.type === "run.failed") return "blocked";
  if (event.type === "agent.aborted") return "stopped";
  if (event.type === "run.completed" || event.type === "agent.completed") return "completed";
  return "agent_activity";
}

function workspaceSummary(event: Pick<AgentUiEvent, "type" | "payload">): string {
  const explicit = payloadText(event.payload, "summary") ?? payloadText(event.payload, "message");
  if (explicit !== null) return explicit;
  const toolName = payloadText(event.payload, "toolName");
  const labels: Partial<Record<AgentUiEventType, string>> = {
    "agent.started": "Agent 已开始处理当前任务",
    "agent.completed": "Agent 已完成本轮工作",
    "agent.aborted": "Agent 运行已停止，现有事件和工件已保留",
    "turn.started": "模型回合已开始",
    "turn.completed": "模型回合已完成",
    "assistant.delta": "正在接收模型增量输出",
    "tool.started": toolName ? `正在调用 ${toolName}` : "科学工具已开始",
    "tool.progress": toolName ? `${toolName} 正在执行` : "科学工具正在执行",
    "tool.completed": toolName
      ? `${toolName}${event.payload.isError === true ? " 执行失败" : " 已完成"}`
      : "科学工具已完成",
    "step.changed": payloadText(event.payload, "name") ?? "任务阶段已更新",
    "artifact.created": "新工件已登记",
    "approval.requested": "需要人工审批后才能继续",
    "approval.resolved": "人工审批已记录",
    "run.blocked": "运行遇到外部阻塞",
    "run.failed": "运行失败，恢复信息已保留",
    "run.started": "任务运行已建立",
    "run.heartbeat": "等待中的运行状态已更新",
    "run.completed": "任务运行已完成",
    "security.quarantined": "不可信输入已隔离",
    "model.changed": "当前会话模型已切换",
    "model.request": "固定 Provider 模型请求已发出",
    "model.usage": "Provider 用量已核验",
    "message.completed": "最终结果已写入对话",
    "queue.changed": "等待队列状态已更新",
    "retry.started": "正在按原模型与原任务重试",
    "retry.completed": "重试已结束",
    "compaction.started": "上下文压缩已开始",
    "compaction.completed": "上下文压缩已完成",
    "hardware.batch": "量子硬件状态已更新",
  };
  return labels[event.type] ?? event.type;
}

export function toAgentWorkspaceEvent(event: AgentUiEvent): AgentWorkspaceEvent {
  const artifactHashes = [
    ...payloadStrings(event.payload, "artifactHashes"),
    ...["sha256", "artifactSha256", "resultArtifactSha256"]
      .map((key) => payloadText(event.payload, key))
      .filter((value): value is string => value !== null && /^[a-f0-9]{64}$/u.test(value)),
  ];
  const sensitivity: AgentWorkspaceSensitivity = event.type === "model.request"
    || event.payload.sensitive === true
    ? "SENSITIVE_REDACTED"
    : event.type.startsWith("tool.") || event.type === "hardware.batch"
      ? "INTERNAL"
      : "PUBLIC";
  return {
    schemaVersion: "qf.workspace-event.v1",
    sourceEventId: event.eventId,
    kind: workspaceKind(event),
    taskId: event.taskId,
    conversationId: event.conversationId,
    runId: event.runId,
    agentId: payloadText(event.payload, "agentId") ?? payloadText(event.payload, "toolName"),
    sequence: event.sequence,
    timestamp: event.createdAt,
    summary: workspaceSummary(event),
    stage: payloadText(event.payload, "stage") ?? payloadText(event.payload, "name"),
    detailRefs: {
      eventId: event.eventId,
      runId: event.runId,
      artifactHashes: [...new Set(artifactHashes)],
      queryId: payloadText(event.payload, "queryId") ?? payloadText(event.payload, "query_id"),
    },
    sensitivity,
  };
}

export interface StepStatus {
  schemaVersion: typeof CONTRACT_SCHEMA_VERSION;
  stepId: string;
  runId: string;
  name: string;
  status: StepState;
  errorCategory: string | null;
  startedAt: string | null;
  finishedAt: string | null;
}

export interface ArtifactSummary {
  schemaVersion: typeof CONTRACT_SCHEMA_VERSION;
  sha256: string;
  mediaType: string;
  bytes: number;
  producer: string;
  createdAt: string;
  parentHashes: string[];
  previewable: boolean;
}

export interface ApprovalRequest {
  schemaVersion: typeof CONTRACT_SCHEMA_VERSION;
  approvalId: string;
  taskId: string;
  conversationId: string;
  action: "FREEZE_PROTOCOL" | "NEXT_ITERATION" | "UNSEAL_TEST" | "SUBMIT_HARDWARE";
  subjectHash: string;
  rationale: string;
  status: "PENDING" | "APPROVED" | "REJECTED";
  requestedBy: "AGENT" | "USER";
  requestedAt: string;
  decidedAt: string | null;
}

export interface AgentSessionSummary {
  schemaVersion: typeof CONTRACT_SCHEMA_VERSION;
  sessionId: string;
  runtimeKind: RuntimeMode;
  runtimeRevision: string;
  configHash: string;
  recoveryCursor: number;
  provider: string;
  modelId: string;
  promptVersion: string;
  promptHash: string;
  lifecycleState: string;
  relativeSessionFile: string | null;
}

export interface OpenHandsWorkspaceSummary {
  schemaVersion: typeof CONTRACT_SCHEMA_VERSION;
  conversationId: string;
  runtimeSessionId: string;
  manifestHash: string;
  backend: "hardened-docker-agent-server";
  hostLocalWorkspace: false;
  workspaceId: string;
  snapshot: JsonObject;
  imageReference: string;
  imageDigest: string;
  initialContainerId: string;
  serverReusedAtCreation: boolean;
  secretEnvironmentNames: string[];
  createdAt: string;
  updatedAt: string;
}

export const TOOL_CALL_INBOX_STATES = ["RUNNING", "UNKNOWN", "COMPLETED", "FAILED"] as const;
export type ToolCallInboxState = (typeof TOOL_CALL_INBOX_STATES)[number];

export interface ToolCallInboxEntry {
  schemaVersion: typeof CONTRACT_SCHEMA_VERSION;
  conversationId: string;
  runtimeSessionId: string;
  toolCallId: string;
  toolName: string;
  requestHash: string;
  status: ToolCallInboxState;
  result: JsonValue | null;
  error: JsonObject | null;
  claimedAt: string;
  finishedAt: string | null;
  updatedAt: string;
}

export interface ToolCallClaim {
  disposition: "CLAIMED" | "IN_FLIGHT" | "REPLAY";
  entry: ToolCallInboxEntry;
}

export interface ConversationSnapshot {
  schemaVersion: typeof CONTRACT_SCHEMA_VERSION;
  conversation: ConversationSummary;
  messages: MessageRecord[];
  steps: StepStatus[];
  artifacts: ArtifactSummary[];
  approvals: ApprovalRequest[];
  session: AgentSessionSummary | null;
  workspace?: OpenHandsWorkspaceSummary | null;
  recentEvents: AgentUiEvent[];
  latestSequence: number;
  capturedAt: string;
}

export interface ModelAvailability {
  schemaVersion: typeof CONTRACT_SCHEMA_VERSION;
  provider: string;
  modelId: string;
  label: string;
  catalogListed: boolean;
  available: boolean;
  supportsStreaming: boolean;
  supportsTools: boolean;
  protocol: "chat-completions";
  readiness: "UNKNOWN" | "READY" | "UNAVAILABLE" | "UNCONFIGURED";
  checkedAt: string;
  failure?: StructuredError;
}

export const CONVERSATION_MODEL_OPTIONS = [
  { provider: "openai", modelId: "gpt-5.6", label: "GPT-5.6 Sol" },
  { provider: "deepseek", modelId: "deepseek-v4-pro", label: "DeepSeek v4 Pro" },
] as const;

export type ConversationModelOption = (typeof CONVERSATION_MODEL_OPTIONS)[number];

export interface CreateProjectCommand {
  name: string;
  description?: string;
}

export interface UpdateProjectCommand {
  name?: string;
  description?: string;
  archived?: boolean;
}

export interface CreateConversationCommand {
  title: string;
  mode: CreateConversationMode;
  provider?: string;
  modelId?: string;
}

export interface SwitchConversationModelCommand {
  provider: ConversationModelOption["provider"];
  modelId: ConversationModelOption["modelId"];
  actor: "HUMAN";
}

export interface SendMessageCommand {
  content: string;
  intent?: "AUTO" | "CHAT" | "WORK";
}

export interface ApprovalDecisionCommand {
  decision: "APPROVE" | "REJECT";
  actor: "HUMAN";
}

export const CAMPAIGN_STATES = [
  "CREATED",
  "PREFLIGHT",
  "RUNNING",
  "PAUSED",
  "BLOCKED",
  "FAILED",
  "COMPLETED",
  "CANCELLED",
] as const;
export type CampaignState = (typeof CAMPAIGN_STATES)[number];

export const CAMPAIGN_ROLES = [
  "intake_guard",
  "supervisor_router",
  "data_steward",
  "scientific_builder",
  "quantum_executor",
  "evidence_verifier",
] as const;
export type CampaignRole = (typeof CAMPAIGN_ROLES)[number];

export const CHAIN_LEVELS = ["L0", "L1", "L2", "L3", "L4"] as const;
export type ChainLevel = (typeof CHAIN_LEVELS)[number];

export interface P02LaunchAuthorization {
  schemaVersion: "qf.p02.authorization.v1";
  taskTitle: string;
  providerAlias: "openai";
  baseUrlAlias: "getoken";
  modelId: "gpt-5.6-sol";
  minimumRuntimeMinutes: 150;
  maximumRuntimeMinutes: 360;
  formalTestSealed: true;
  tushareReadonly: true;
  localMarketDataCache: true;
  tianyanCloudSimulator: true;
  tianyanHardware: true;
  maxSimulatorJobs: 4;
  simulatorShotsPerJob: 5000;
  maxHardwareJobs: 2;
  hardwareMaxTotalShots: 10000;
  hardwareMaxCumulativeExecutionSeconds: 600;
  allowModelFallback: false;
  authorizeGitCommit: false;
  authorizeGitPush: false;
  authorizationSource: string;
}

export interface P03LaunchAuthorization {
  schemaVersion: "qf.p03.authorization.v1";
  stageId: "P03";
  providerAlias: "openai";
  baseUrlAlias: "getoken";
  modelId: "gpt-5.6-sol";
  allowModelFallback: false;
  resumeCampaignId: string;
  formalTestSealed: true;
  backend: "tianyan176";
  maxNewHardwareJobs: 2;
  shotsPerJob: 100;
  maxNewHardwareShots: 200;
  conservativeSecondsPerJob: 240;
  maxNewHardwareExecutionSeconds: 600;
  queueCheckpointMinutes: 30;
  queueForegroundMinutes: 180;
  authorizeCloudResubmit: false;
  authorizeGitCommit: false;
  authorizeGitPush: false;
  authorizationSource: string;
}

export interface P04LaunchAuthorization {
  schemaVersion: "qf.p04.authorization.v1";
  stageId: "P04";
  providerAlias: "openai";
  baseUrlAlias: "getoken";
  modelId: "gpt-5.6-sol";
  allowModelFallback: false;
  minimumRuntimeMinutes: 360;
  maximumRuntimeMinutes: 480;
  formalTestSealed: true;
  runLanes: readonly ["finance", "tool_factory", "evidence_audit"];
  backend: "tianyan176";
  remoteSimulatorEnabled: false;
  maxActiveHardwareLeases: 1;
  requireLocalCqlibPreflight: true;
  authorizeFaultInjection: true;
  authorizeGitCommit: false;
  authorizeGitPush: false;
  authorizationSource: string;
}

export const P04_RUN_LANES = ["finance", "tool_factory", "evidence_audit"] as const;
export type P04RunLane = (typeof P04_RUN_LANES)[number];

export const P04_RUN_STATES = [
  "CREATED",
  "RUNNING",
  "RECOVERING",
  "COMPLETED",
  "BLOCKED",
  "FAILED",
] as const;
export type P04RunState = (typeof P04_RUN_STATES)[number];

export interface P04RunSummary {
  schemaVersion: typeof CONTRACT_SCHEMA_VERSION;
  runId: string;
  campaignId: string;
  lane: P04RunLane;
  status: P04RunState;
  relativeWorkspace: string;
  workerId: string | null;
  processId: number | null;
  heartbeatAt: string | null;
  eventCount: number;
  artifactCount: number;
  recoveryCount: number;
  startedAt: string | null;
  completedAt: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface P04GeneratedToolSummary {
  schemaVersion: typeof CONTRACT_SCHEMA_VERSION;
  toolId: string;
  campaignId: string;
  runId: string;
  name: "financial_result_diagnostics" | "quantum_result_diagnostics";
  status: "GENERATED" | "VALIDATED" | "REGISTERED" | "REJECTED";
  specHash: string;
  codeHash: string;
  testHash: string;
  sourceArtifactSha256: string | null;
  testArtifactSha256: string | null;
  invocationCount: number;
  createdAt: string;
  updatedAt: string;
}

export interface P04FaultInjectionSummary {
  schemaVersion: typeof CONTRACT_SCHEMA_VERSION;
  faultId: string;
  campaignId: string;
  runId: string;
  kind: "SCIENCE_PROCESS_TERMINATION" | "EXTERNAL_QUERY_PROCESS_TERMINATION";
  targetProcessId: number;
  state: "PLANNED" | "INJECTED" | "RECOVERED" | "FAILED";
  checkpointArtifactSha256: string | null;
  recoveryArtifactSha256: string | null;
  injectedAt: string | null;
  recoveredAt: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface P04CampaignDetail {
  schemaVersion: typeof CONTRACT_SCHEMA_VERSION;
  campaign: CampaignSummary;
  runs: P04RunSummary[];
  tools: P04GeneratedToolSummary[];
  faults: P04FaultInjectionSummary[];
  hardwareLease: {
    backend: "tianyan176";
    campaignId: string;
    runId: string;
    queryId: string | null;
    state: string;
    heartbeatAt: string;
  } | null;
}

export const P03_QUEUE_LIFECYCLE_STATES = [
  "DISCOVERING",
  "BACKEND_UNAVAILABLE",
  "READY_TO_SUBMIT",
  "SUBMITTING",
  "QUEUED",
  "RUNNING",
  "COMPLETED",
  "FAILED",
  "CANCELLED",
  "UNKNOWN",
] as const;
export type P03QueueLifecycleState = (typeof P03_QUEUE_LIFECYCLE_STATES)[number];

export interface P03HardwareBudgetSummary {
  p02HistoricalJobs: number;
  p02HistoricalShots: number;
  p02HistoricalExecutionSeconds: number;
  p03Jobs: number;
  p03Shots: number;
  p03ReservedExecutionSeconds: number;
  p03MaxJobs: 2;
  p03MaxShots: 200;
  p03MaxExecutionSeconds: 600;
  lifecycleJobs: number;
  lifecycleShots: number;
  lifecycleConservativeExecutionSeconds: number;
}

export interface P03QueueRunSummary {
  schemaVersion: typeof CONTRACT_SCHEMA_VERSION;
  queueRunId: string;
  campaignId: string;
  ordinal: 1 | 2;
  stageId: "P03";
  backend: "tianyan176";
  shots: 100;
  purpose: "queue_state_machine_validation" | "queue_state_machine_regression";
  lifecycleState: P03QueueLifecycleState;
  circuitHash: string | null;
  quantumJobId: string | null;
  reservedExecutionSeconds: number;
  queryId: string | null;
  providerStatus: string | null;
  machineStatus: string | null;
  queuePosition: number | null;
  estimatedStartAt: string | null;
  lastQueriedAt: string | null;
  nextQueryAt: string | null;
  pollAttempts: number;
  queueEnteredAt: string | null;
  terminalAt: string | null;
  checkpoint30mAt: string | null;
  unknownSubmission: boolean;
  discoveryArtifactSha256: string | null;
  stateArtifactSha256: string | null;
  rawResultArtifactSha256: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface CampaignSummary {
  schemaVersion: typeof CONTRACT_SCHEMA_VERSION;
  campaignId: string;
  taskId: string;
  conversationId: string;
  authorizationHash: string;
  status: CampaignState;
  currentStage: string;
  minimumRuntimeSeconds: number;
  maximumRuntimeSeconds: number;
  wallClockSeconds: number;
  activeComputeSeconds: number;
  externalWaitSeconds: number;
  humanWaitSeconds: number;
  llmCalls: number;
  hardwareJobs: number;
  hardwareExecutionSeconds: number;
  p03HardwareBudget: P03HardwareBudgetSummary;
  latestP03QueueRun: P03QueueRunSummary | null;
  highestChainLevel: ChainLevel;
  blockerCategory: string | null;
  blockerArtifactSha256: string | null;
  lastHeartbeatAt: string | null;
  startedAt: string | null;
  completedAt: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface CampaignCheckpoint {
  schemaVersion: typeof CONTRACT_SCHEMA_VERSION;
  checkpointId: string;
  campaignId: string;
  stage: string;
  stateHash: string;
  payload: JsonObject;
  createdAt: string;
}

export interface CampaignActionSummary {
  schemaVersion: typeof CONTRACT_SCHEMA_VERSION;
  actionId: string;
  campaignId: string;
  stage: string;
  actionType: string;
  idempotencyKey: string;
  inputHash: string;
  status: "PENDING" | "RUNNING" | "COMPLETED" | "BLOCKED" | "FAILED";
  expectedEvidence: string;
  outputArtifactSha256: string | null;
  startedAt: string;
  completedAt: string | null;
}

export interface FailureCardSummary {
  schemaVersion: typeof CONTRACT_SCHEMA_VERSION;
  failureCardId: string;
  campaignId: string;
  stage: string;
  category: string;
  summary: string;
  evidenceArtifactSha256: string;
  attempts: number;
  recoveryCommand: string;
  createdAt: string;
}

export interface QuantumJobSummary {
  schemaVersion: typeof CONTRACT_SCHEMA_VERSION;
  quantumJobId: string;
  campaignId: string;
  purpose: "cloud_simulator" | "representative_qgnn_subcircuit" | "representative_financial_qaoa";
  backend: string;
  targetType: "SIMULATOR" | "HARDWARE";
  circuitHash: string;
  shots: number;
  estimatedExecutionSeconds: number | null;
  actualExecutionSeconds: number | null;
  queryId: string | null;
  terminalStatus: string | null;
  rawResultArtifactSha256: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface CampaignDetail {
  campaign: CampaignSummary;
  checkpoints: CampaignCheckpoint[];
  actions: CampaignActionSummary[];
  failures: FailureCardSummary[];
  quantumJobs: QuantumJobSummary[];
  p03QueueRuns: P03QueueRunSummary[];
}

export type P05TaskState =
  | "CREATED"
  | "RUNNING"
  | "WAITING_PROVIDER"
  | "WAITING_HARDWARE"
  | "COMPLETED"
  | "BLOCKED"
  | "FAILED"
  | "TIME_BUDGET_EXHAUSTED";

export interface P05UploadSummary {
  uploadId: string;
  conversationId: string;
  projectId: string;
  sourceScope: "CONVERSATION" | "PROJECT";
  taskId: string | null;
  fileName: string;
  extension: string;
  declaredMediaType: string;
  detectedMediaType: string;
  byteSize: number;
  sha256: string;
  parser: string;
  riskLevel: "LOW" | "MEDIUM" | "HIGH";
  quarantined: boolean;
  parseResult: JsonObject;
  createdAt: string;
}

export interface ProjectSourceSummary {
  sourceId: string;
  projectId: string;
  addedFromConversationId: string | null;
  fileName: string;
  extension: string;
  declaredMediaType: string;
  detectedMediaType: string;
  byteSize: number;
  sha256: string;
  parser: string;
  riskLevel: "LOW" | "MEDIUM" | "HIGH";
  quarantined: boolean;
  artifactSha256?: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface P05EventSummary {
  eventId: string;
  sequence: number;
  eventType: string;
  runId: string | null;
  agentId: string | null;
  strategy: string | null;
  summary: string;
  detail: JsonObject;
  createdAt: string;
}

export interface P05ModelCallSummary {
  callId: string;
  runId: string;
  agentId: string;
  strategy: string;
  purpose: string;
  promptSha256: string;
  responseSha256: string | null;
  status: "STARTED" | "COMPLETED" | "FAILED";
  httpStatus: number | null;
  promptTokens: number;
  completionTokens: number;
  cachedTokens: number;
  totalTokens: number;
  latencyMs: number | null;
  artifactPath: string | null;
  errorCode: string | null;
  createdAt: string;
  completedAt: string | null;
}

export interface P05ToolSummary {
  toolId: string;
  name: string;
  status: string;
  codeSha256: string;
  testSha256: string;
  validation: JsonObject;
  invocation: JsonObject;
  relativeWorkspace: string;
  createdAt: string;
}

export interface P05CircuitSummary {
  circuitId: string;
  circuitIndex: number;
  family: string;
  manifest: JsonObject;
  qcis: string;
  qcisSha256: string;
  idempotencyKey: string;
  queryId: string | null;
  state: "REGISTERED" | "SUBMITTED" | "COMPLETED" | "UNKNOWN" | "FAILED";
  receipt: JsonObject | null;
  rawResult: JsonObject | null;
  createdAt: string;
  updatedAt: string;
}

export interface P05BatchSummary {
  batchId: string;
  batchIndex: number;
  batchKind: "BASELINE_EXPLORATION" | "OPTIMIZATION" | "INDEPENDENT_CONFIRMATION";
  status: "REGISTERED" | "SUBMITTING" | "SUBMITTED" | "QUERYING" | "COMPLETED" | "PARTIAL" | "FAILED";
  shots: number;
  backend: "tianyan176";
  leaseKey: string;
  receipt: JsonObject | null;
  metrics: JsonObject | null;
  circuits: P05CircuitSummary[];
  createdAt: string;
  submittedAt: string | null;
  completedAt: string | null;
}

export interface P05VendorCandidateSummary {
  name: string;
  repositoryUrl: string;
  commitSha: string;
  licenseSpdx: string;
  decision: "ACCEPT" | "REJECT" | "AUDIT_ONLY";
  evidence: JsonObject;
  createdAt: string;
}

export interface P05TaskDetail {
  schemaVersion: typeof CONTRACT_SCHEMA_VERSION;
  p05TaskId: string;
  conversationId: string;
  taskId: string;
  provider: "openai/getoken";
  modelId: "gpt-5.6-sol";
  status: P05TaskState;
  stage: string;
  objective: string;
  taskStartedAt: string;
  hardDeadlineAt: string;
  remainingSeconds: number;
  formalTestSealed: true;
  verifiedTotalTokens: number;
  promptTokens: number;
  completionTokens: number;
  cachedTokens: number;
  remainingTokens: number;
  requiredTokensPerSecond: number;
  stableConcurrency: number;
  lastArtifactPath: string | null;
  checkpoint: JsonObject;
  error: JsonObject | null;
  uploads: P05UploadSummary[];
  events: P05EventSummary[];
  modelCalls: P05ModelCallSummary[];
  tools: P05ToolSummary[];
  vendorCandidates: P05VendorCandidateSummary[];
  batches: P05BatchSummary[];
  createdAt: string;
  updatedAt: string;
}

export interface P05UploadCommand {
  conversationId: string;
  fileName: string;
  declaredMediaType: string;
  bytesBase64: string;
}

export interface ProjectSourceCommand {
  conversationId?: string;
  fileName: string;
  declaredMediaType: string;
  bytesBase64: string;
}

export interface P05LaunchCommand {
  conversationId: string;
  objective: string;
}
