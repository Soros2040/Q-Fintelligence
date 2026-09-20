import type { AgentUiEventType, JsonObject, StructuredError } from "@q-fintelligence/contracts";

export interface RuntimeEmission {
  type: AgentUiEventType;
  payload: JsonObject;
  persistent: boolean;
}

export type RuntimeEventSink = (event: RuntimeEmission) => Promise<void>;

export interface RuntimePromptOptions {
  toolNames?: readonly string[];
}

export interface RuntimeResult {
  assistantContent: string;
  provider: string;
  modelId: string;
  runtimeMessageId: string | null;
  usage?: {
    input: number;
    output: number;
    cacheRead: number;
    cacheWrite: number;
    reasoning: number | null;
    totalTokens: number;
  };
}

export interface AgentRuntime {
  readonly kind: "MOCK" | "OPENHANDS";
  readonly toolNames: readonly string[];
  prompt(content: string, sink: RuntimeEventSink, options?: RuntimePromptOptions): Promise<RuntimeResult>;
  steer(content: string): Promise<void>;
  followUp(content: string): Promise<void>;
  abort(): Promise<void>;
  shutdown?(): Promise<void>;
  dispose(): void;
}

export class RuntimeAbortedError extends Error {
  constructor() {
    super("agent run was aborted");
  }
}

export class RuntimeFailureError extends Error {
  constructor(
    message: string,
    readonly code: string,
    readonly retryable: boolean,
    readonly category: StructuredError["category"] = "TOOL",
    readonly recovery = "Review the structured error and submit a corrected prompt.",
  ) {
    super(message);
  }
}

export class RuntimeControlError extends Error {
  constructor(
    message: string,
    readonly code: string,
    readonly retryable: boolean,
    readonly recovery = "Use an explicitly supported runtime control operation.",
  ) {
    super(message);
  }
}
