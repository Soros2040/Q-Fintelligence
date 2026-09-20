import type {
  AgentUiEvent,
  AgentWorkspaceEvent,
  ConversationSnapshot,
} from "@q-fintelligence/contracts";
import { toAgentWorkspaceEvent } from "@q-fintelligence/contracts";

export type WorkspaceResultKind =
  | "markdown"
  | "code"
  | "diff"
  | "test"
  | "data_table"
  | "chart"
  | "quantum_circuit"
  | "artifact"
  | "warning"
  | "error"
  | "hardware"
  | "result";

export function workspaceEvent(event: AgentUiEvent): AgentWorkspaceEvent {
  return event.workspace ?? toAgentWorkspaceEvent(event);
}

export function mergeWorkspaceEvent(
  current: AgentUiEvent[],
  incoming: AgentUiEvent,
  maximum = 240,
): AgentUiEvent[] {
  if (current.some((event) => event.eventId === incoming.eventId)) return current;
  const withoutSupersededHeartbeat = incoming.type === "run.heartbeat"
    ? current.filter((event) => event.type !== "run.heartbeat")
    : current;
  const next = [...withoutSupersededHeartbeat, incoming].sort((left, right) => {
    if (left.sequence !== null && right.sequence !== null) return left.sequence - right.sequence;
    return Date.parse(left.createdAt) - Date.parse(right.createdAt);
  });
  if (next.length <= maximum) return next;
  const heartbeat = next.findLast((event) => event.type === "run.heartbeat");
  const durable = next.filter((event) => event.type !== "run.heartbeat").slice(-(maximum - 1));
  return heartbeat === undefined ? next.slice(-maximum) : [...durable, heartbeat];
}

export function compactWorkspaceEvents(events: AgentUiEvent[], maximum = 240): AgentUiEvent[] {
  return events.reduce<AgentUiEvent[]>((current, event) => mergeWorkspaceEvent(current, event, maximum), []);
}

export function visibleWorkspaceEvents(events: AgentUiEvent[]): AgentUiEvent[] {
  return events.filter((event) => event.type !== "run.heartbeat" && event.type !== "assistant.delta");
}

export function latestHeartbeat(events: AgentUiEvent[]): AgentUiEvent | null {
  return events.findLast((event) => event.type === "run.heartbeat") ?? null;
}

const CONVERSATION_SUMMARY_REFRESH_EVENTS = new Set<AgentUiEvent["type"]>([
  "run.started",
  "message.completed",
  "agent.completed",
  "agent.aborted",
  "run.failed",
  "run.completed",
  "model.changed",
  "rename.completed",
]);

export function shouldRefreshConversationSummary(event: Pick<AgentUiEvent, "type">): boolean {
  return CONVERSATION_SUMMARY_REFRESH_EVENTS.has(event.type);
}

export function runSurfaceLabel(events: AgentUiEvent[]): "CHAT TURN" | "WORK RUN" {
  const latestRun = events.findLast((event) => event.type === "run.started");
  return latestRun?.payload.promptIntent === "CHAT" ? "CHAT TURN" : "WORK RUN";
}

export function resultKind(event: AgentUiEvent): WorkspaceResultKind {
  const explicit = event.payload.resultKind;
  if (typeof explicit === "string" && new Set<WorkspaceResultKind>([
    "markdown", "code", "diff", "test", "data_table", "chart", "quantum_circuit",
    "artifact", "warning", "error", "hardware", "result",
  ]).has(explicit as WorkspaceResultKind)) return explicit as WorkspaceResultKind;
  if (event.type === "security.quarantined") return "warning";
  if (event.type === "run.failed" || (event.type === "tool.completed" && event.payload.isError === true)) return "error";
  if (event.type === "hardware.batch") return "hardware";
  if (event.type === "artifact.created") return "artifact";
  if (event.type === "message.completed" || event.type === "run.completed") return "result";
  return "markdown";
}

export function currentStage(snapshot: ConversationSnapshot, events: AgentUiEvent[]): string {
  const eventStage = [...events].reverse()
    .map((event) => workspaceEvent(event).stage)
    .find((value): value is string => value !== null);
  if (eventStage !== undefined) return eventStage;
  const runningStep = snapshot.steps.find((step) => step.status === "RUNNING");
  return runningStep?.name ?? snapshot.conversation.state;
}

export function taskProgress(snapshot: ConversationSnapshot): number {
  if (snapshot.conversation.state === "COMPLETED") return 100;
  if (snapshot.steps.length === 0) return snapshot.conversation.state === "RUNNING" ? 12 : 0;
  const completed = snapshot.steps.filter((step) => step.status === "COMPLETED").length;
  return Math.min(95, Math.max(5, Math.round((completed / snapshot.steps.length) * 100)));
}

export function runPresentation(
  snapshot: ConversationSnapshot,
  events: AgentUiEvent[],
): {
  hasStarted: boolean;
  startedAt: string | null;
  label: "等待任务" | "正在处理" | "已暂停" | "已阻塞" | "已处理";
  ticking: boolean;
} {
  const runStartedAt = events.find((event) => event.type === "run.started")?.createdAt;
  const firstUserMessageAt = snapshot.messages.find((message) => message.role === "USER")?.createdAt;
  const startedAt = runStartedAt ?? firstUserMessageAt ?? null;
  if (startedAt === null) {
    return { hasStarted: false, startedAt: null, label: "等待任务", ticking: false };
  }
  if (snapshot.conversation.state === "RUNNING") {
    return { hasStarted: true, startedAt, label: "正在处理", ticking: true };
  }
  if (snapshot.conversation.state === "PAUSED") {
    return { hasStarted: true, startedAt, label: "已暂停", ticking: false };
  }
  if (snapshot.conversation.state === "BLOCKED") {
    return { hasStarted: true, startedAt, label: "已阻塞", ticking: false };
  }
  return { hasStarted: true, startedAt, label: "已处理", ticking: false };
}

export function elapsedLabel(startedAt: string, now: number): string {
  const totalSeconds = Math.max(0, Math.floor((now - Date.parse(startedAt)) / 1_000));
  const hours = Math.floor(totalSeconds / 3_600);
  const minutes = Math.floor((totalSeconds % 3_600) / 60);
  const seconds = totalSeconds % 60;
  return hours > 0
    ? `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`
    : `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}
