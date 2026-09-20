import { Type } from "typebox";

import type { JsonObject, JsonValue } from "@q-fintelligence/contracts";

import type { AgentRuntime } from "../agent/runtime.js";
import type { QfToolExecutionContext, QfToolSpec } from "../agent/tools.js";
import type { CampaignRoleDefinition } from "./roles.js";
import { assertCampaignRoleToolNames } from "./roles.js";

export interface RoleSessionProbeResult {
  role: CampaignRoleDefinition["role"];
  sessionId: string;
  sessionFile: string | null;
  toolNames: string[];
  stopReason: string;
  toolCallCount: number;
  textReceived: boolean;
}

function roleProbeResult(definition: CampaignRoleDefinition, toolName: string): JsonObject {
  return { status: "READ_ONLY_PROBE", role: definition.role, tool: toolName };
}

export function campaignRoleToolSpecs(definition: CampaignRoleDefinition): QfToolSpec[] {
  return definition.toolNames.map((name) => ({
    name,
    label: name,
    description: `Constrained ${definition.role} tool. This capability probe returns no scientific facts or approval.`,
    parameters: Type.Object({}, { additionalProperties: false }),
    execute: async () => roleProbeResult(definition, name),
  }));
}

export function campaignRoleToolExecutor(definition: CampaignRoleDefinition): (
  name: string,
  input: JsonObject,
  context: QfToolExecutionContext,
) => Promise<JsonValue> {
  const allowed = new Set(definition.toolNames);
  return async (name, input, context) => {
    context.signal.throwIfAborted();
    if (!allowed.has(name)) throw new Error(`P02 role ${definition.role} rejected an out-of-scope tool`);
    if (Object.keys(input).length !== 0) throw new Error(`P02 role ${definition.role} probe tools require an empty object`);
    return roleProbeResult(definition, name);
  };
}

/**
 * Exercise one already-created role runtime through the framework-neutral QF
 * runtime contract. The caller supplies the persisted runtime identity so this
 * helper does not know about a framework SDK, provider transport, or storage
 * layout. Ownership of the runtime transfers to this function and it is always
 * disposed before returning.
 */
export async function probeCampaignRoleSession(input: {
  runtime: AgentRuntime;
  sessionId: string;
  sessionFile: string | null;
  definition: CampaignRoleDefinition;
}): Promise<RoleSessionProbeResult> {
  let toolCallCount = 0;
  let textReceived = false;
  try {
    const actualTools = [...input.runtime.toolNames];
    assertCampaignRoleToolNames(input.definition.role, actualTools);
    await input.runtime.prompt(
      `Call ${input.definition.toolNames[0]} exactly once with an empty object. Then return one strict JSON object with role and status READY.`,
      async (event) => {
        if (event.type === "tool.started") toolCallCount += 1;
        if (event.type === "assistant.delta" && typeof event.payload.text === "string"
          && event.payload.text.trim()) textReceived = true;
      },
      { toolNames: input.definition.toolNames },
    );
    return {
      role: input.definition.role,
      sessionId: input.sessionId,
      sessionFile: input.sessionFile,
      toolNames: actualTools,
      stopReason: "stop",
      toolCallCount,
      textReceived,
    };
  } finally {
    input.runtime.dispose();
  }
}
