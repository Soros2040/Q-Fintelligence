import { createHash, randomUUID } from "node:crypto";

import type { JsonObject } from "@q-fintelligence/contracts";

import type { QfToolHost, ToolExecutionResult } from "./tools.js";
import { QF_TOOL_NAMES } from "./tools.js";
import type { AgentRuntime, RuntimeEmission, RuntimeEventSink, RuntimePromptOptions, RuntimeResult } from "./runtime.js";
import { RuntimeAbortedError, RuntimeFailureError } from "./runtime.js";

const MOCK_DELAY_MS = process.env.NODE_ENV === "test" ? 5 : 90;

export class MockRuntime implements AgentRuntime {
  readonly kind = "MOCK" as const;
  readonly toolNames = QF_TOOL_NAMES;
  private aborted = false;
  private currentSink: RuntimeEventSink | null = null;
  private readonly steering: string[] = [];
  private readonly followUps: string[] = [];

  constructor(private readonly host: QfToolHost) {}

  private async emit(event: RuntimeEmission): Promise<void> {
    await this.currentSink?.(event);
  }

  private async wait(): Promise<void> {
    await new Promise((resolve) => setTimeout(resolve, MOCK_DELAY_MS));
    if (this.aborted) throw new RuntimeAbortedError();
  }

  private async runTool(name: Parameters<QfToolHost["execute"]>[0], input: JsonObject): Promise<ToolExecutionResult> {
    const toolCallId = `mock_tool_${randomUUID()}`;
    await this.emit({ type: "tool.started", payload: { toolCallId, toolName: name }, persistent: true });
    const started = Date.now();
    const result = await this.host.execute(name, input);
    await this.emit({
      type: "tool.completed",
      payload: { toolCallId, toolName: name, durationMs: Date.now() - started, summary: result.summary },
      persistent: true,
    });
    if (result.step) {
      await this.emit({
        type: "step.changed",
        payload: { stepId: result.step.stepId, name: result.step.name, status: result.step.status },
        persistent: true,
      });
    }
    if (result.artifact) {
      await this.emit({
        type: "artifact.created",
        payload: { sha256: result.artifact.sha256, mediaType: result.artifact.mediaType, bytes: result.artifact.bytes },
        persistent: true,
      });
    }
    if (result.approval) {
      await this.emit({
        type: "approval.requested",
        payload: { approvalId: result.approval.approvalId, action: result.approval.action, status: result.approval.status },
        persistent: true,
      });
    }
    return result;
  }

  async prompt(content: string, sink: RuntimeEventSink, options?: RuntimePromptOptions): Promise<RuntimeResult> {
    this.aborted = false;
    this.currentSink = sink;
    await this.emit({ type: "agent.started", payload: { runtime: "MOCK", fixture: "p0.v1" }, persistent: true });
    await this.emit({ type: "turn.started", payload: { source: "prompt" }, persistent: true });
    await this.wait();

    if (options?.toolNames?.length === 0) {
      const assistantContent = `【MOCK CHAT】${content.slice(0, 1000)}`;
      await this.emit({ type: "assistant.delta", payload: { text: assistantContent }, persistent: false });
      await this.emit({ type: "turn.completed", payload: { runtime: "MOCK", toolResultCount: 0 }, persistent: true });
      await this.emit({ type: "agent.completed", payload: { runtime: "MOCK" }, persistent: true });
      this.currentSink = null;
      return { assistantContent, provider: "mock", modelId: "qf-mock-v1", runtimeMessageId: null };
    }

    if (/可控失败|触发失败|controlled failure|\bfail\b/iu.test(content)) {
      throw new RuntimeFailureError(
        "Mock fixture triggered the requested controlled failure.",
        "MOCK_CONTROLLED_FAILURE",
        true,
      );
    }

    await this.emit({
      type: "assistant.delta",
      payload: { text: "【MOCK】正在读取已登记任务上下文，并执行确定性白名单步骤。" },
      persistent: false,
    });
    const context = await this.runTool("get_task_context", {});
    await this.wait();
    const step = await this.runTool("request_step_execution", { stepName: "draft_task_spec" });
    await this.wait();
    const artifactResult = await this.runTool("record_analysis", {
      content: `【MOCK】TaskSpec 草案演示；用户请求：${content.slice(0, 1000)}。此工件只证明产品合同，不包含真实市场或量子结果。`,
      parentHashes: [],
    });
    await this.wait();
    const subjectHash = createHash("sha256").update(`mock-protocol:${content}`).digest("hex");
    const approvalResult = await this.runTool("request_approval", {
      action: "FREEZE_PROTOCOL",
      subjectHash,
      rationale: "Mock fixture requests a human decision; the Agent cannot approve its own protocol.",
    });

    const assistantContent = [
      "【MOCK】已完成 P0 技术原型演示。本结果不是实际模型、市场数据或量子计算结论。",
      context.summary,
      step.summary,
      artifactResult.artifact ? `JSON 工件 SHA-256：${artifactResult.artifact.sha256}` : "未生成工件。",
      approvalResult.approval ? `审批 ${approvalResult.approval.approvalId} 保持 PENDING，需人工决策。` : "未创建审批。",
    ].join("\n");
    await this.emit({ type: "assistant.delta", payload: { text: assistantContent }, persistent: false });
    await this.emit({ type: "turn.completed", payload: { runtime: "MOCK" }, persistent: true });
    await this.emit({ type: "agent.completed", payload: { runtime: "MOCK" }, persistent: true });
    this.currentSink = null;
    return { assistantContent, provider: "mock", modelId: "qf-mock-v1", runtimeMessageId: null };
  }

  async steer(content: string): Promise<void> {
    if (!this.currentSink) throw new Error("no Mock run is active");
    this.steering.push(content);
    await this.emit({ type: "queue.changed", payload: { steering: this.steering.length, followUp: this.followUps.length }, persistent: true });
  }

  async followUp(content: string): Promise<void> {
    if (!this.currentSink) throw new Error("no Mock run is active");
    this.followUps.push(content);
    await this.emit({ type: "queue.changed", payload: { steering: this.steering.length, followUp: this.followUps.length }, persistent: true });
  }

  async abort(): Promise<void> {
    this.aborted = true;
    await this.emit({ type: "agent.aborted", payload: { runtime: "MOCK" }, persistent: true });
  }

  dispose(): void {
    this.aborted = true;
    this.currentSink = null;
  }
}
