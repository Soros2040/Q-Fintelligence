import { safeErrorMessage } from "../security/redaction.js";
import { diagnoseOpenAiChatSseResponse, type OpenAiChatSseDiagnosis } from "./sse-parser.js";

export interface CapabilityProbeResult {
  schemaVersion: "qf.provider-capability.v1";
  provider: "openai";
  modelId: "gpt-5.6-sol";
  checkedAt: string;
  catalog: ProbeCase;
  text: ProbeCase;
  streaming: ProbeCase;
  tool: ProbeCase;
  structuredOutput: ProbeCase;
  noFallback: true;
  overall: "READY" | "BLOCKED_PROVIDER";
}

export interface ProbeCase {
  ok: boolean;
  httpStatus: number | null;
  evidence: string;
  diagnostics?: OpenAiChatSseDiagnosis;
}

function boundedEvidence(value: unknown): string {
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return text.replace(/Bearer\s+\S+/giu, "Bearer [REDACTED]").slice(0, 800);
}

async function jsonRequest(baseUrl: string, apiKey: string, body: Record<string, unknown>): Promise<{ response: Response; json: unknown }> {
  const response = await fetch(`${baseUrl}/chat/completions`, {
    method: "POST",
    headers: { Authorization: `Bearer ${apiKey}`, "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(60_000),
  });
  let json: unknown = null;
  try {
    json = await response.json();
  } catch {
    json = { parseError: true };
  }
  return { response, json };
}

function errorEvidence(json: unknown): string {
  if (json && typeof json === "object" && "error" in json) {
    const error = (json as { error: unknown }).error;
    if (error && typeof error === "object") {
      const record = error as Record<string, unknown>;
      return boundedEvidence({
        type: record.type ?? null,
        code: record.code ?? null,
        message: record.message ?? "provider error",
      });
    }
  }
  return boundedEvidence(json);
}

export async function probeFixedCampaignModel(input: {
  baseUrl: string;
  apiKey: string;
  onBillableCall?: () => void;
}): Promise<CapabilityProbeResult> {
  const baseUrl = input.baseUrl.replace(/\/$/u, "");
  const checkedAt = new Date().toISOString();
  const fail = (error: unknown): ProbeCase => ({ ok: false, httpStatus: null, evidence: boundedEvidence(safeErrorMessage(error)) });
  let catalog: ProbeCase;
  try {
    const response = await fetch(`${baseUrl}/models`, {
      headers: { Authorization: `Bearer ${input.apiKey}` },
      signal: AbortSignal.timeout(30_000),
    });
    const body = await response.json() as { data?: Array<{ id?: unknown }> };
    const listed = Array.isArray(body.data) && body.data.some((item) => item.id === "gpt-5.6-sol");
    catalog = {
      ok: response.ok && listed,
      httpStatus: response.status,
      evidence: response.ok ? `catalogListed=${listed}` : errorEvidence(body),
    };
  } catch (error) {
    catalog = fail(error);
  }

  let text: ProbeCase;
  try {
    input.onBillableCall?.();
    const { response, json } = await jsonRequest(baseUrl, input.apiKey, {
      model: "gpt-5.6-sol",
      messages: [{ role: "user", content: "Reply with QF_P02_TEXT_READY only." }],
      max_tokens: 24,
      stream: false,
    });
    const content = (json as { choices?: Array<{ message?: { content?: unknown } }> }).choices?.[0]?.message?.content;
    text = {
      ok: response.ok && typeof content === "string" && content.includes("QF_P02_TEXT_READY"),
      httpStatus: response.status,
      evidence: response.ok ? `nonEmptyText=${typeof content === "string" && content.length > 0}` : errorEvidence(json),
    };
  } catch (error) {
    text = fail(error);
  }

  const streaming = await probeFixedCampaignStreaming({
    baseUrl,
    apiKey: input.apiKey,
    ...(input.onBillableCall ? { onBillableCall: input.onBillableCall } : {}),
    marker: "QF_P02_STREAM_READY",
  });

  let tool: ProbeCase;
  try {
    input.onBillableCall?.();
    const { response, json } = await jsonRequest(baseUrl, input.apiKey, {
      model: "gpt-5.6-sol",
      messages: [{ role: "user", content: "Call qf_readiness exactly once with value P02." }],
      tools: [{
        type: "function",
        function: {
          name: "qf_readiness",
          description: "Read-only capability probe.",
          parameters: {
            type: "object",
            additionalProperties: false,
            required: ["value"],
            properties: { value: { type: "string", enum: ["P02"] } },
          },
        },
      }],
      tool_choice: { type: "function", function: { name: "qf_readiness" } },
      max_tokens: 128,
      stream: false,
    });
    const calls = (json as { choices?: Array<{ message?: { tool_calls?: unknown[] } }> }).choices?.[0]?.message?.tool_calls;
    tool = {
      ok: response.ok && Array.isArray(calls) && calls.length === 1,
      httpStatus: response.status,
      evidence: response.ok ? `toolCallCount=${Array.isArray(calls) ? calls.length : 0}` : errorEvidence(json),
    };
  } catch (error) {
    tool = fail(error);
  }

  let structuredOutput: ProbeCase;
  try {
    input.onBillableCall?.();
    const { response, json } = await jsonRequest(baseUrl, input.apiKey, {
      model: "gpt-5.6-sol",
      messages: [{ role: "user", content: "Return the requested readiness object." }],
      response_format: {
        type: "json_schema",
        json_schema: {
          name: "qf_p02_readiness",
          strict: true,
          schema: {
            type: "object",
            additionalProperties: false,
            required: ["ready"],
            properties: { ready: { type: "boolean", const: true } },
          },
        },
      },
      max_tokens: 64,
      stream: false,
    });
    const content = (json as { choices?: Array<{ message?: { content?: unknown } }> }).choices?.[0]?.message?.content;
    let valid = false;
    if (typeof content === "string") {
      try {
        valid = (JSON.parse(content) as { ready?: unknown }).ready === true;
      } catch {
        valid = false;
      }
    }
    structuredOutput = {
      ok: response.ok && valid,
      httpStatus: response.status,
      evidence: response.ok ? `schemaValid=${valid}` : errorEvidence(json),
    };
  } catch (error) {
    structuredOutput = fail(error);
  }

  const ready = [catalog, text, streaming, tool, structuredOutput].every((item) => item.ok);
  return {
    schemaVersion: "qf.provider-capability.v1",
    provider: "openai",
    modelId: "gpt-5.6-sol",
    checkedAt,
    catalog,
    text,
    streaming,
    tool,
    structuredOutput,
    noFallback: true,
    overall: ready ? "READY" : "BLOCKED_PROVIDER",
  };
}

export async function probeFixedCampaignStreaming(input: {
  baseUrl: string;
  apiKey: string;
  onBillableCall?: () => void;
  marker?: string;
}): Promise<ProbeCase> {
  const baseUrl = input.baseUrl.replace(/\/$/u, "");
  const marker = input.marker ?? "QF_P03_STREAM_READY";
  try {
    input.onBillableCall?.();
    const response = await fetch(`${baseUrl}/chat/completions`, {
      method: "POST",
      headers: { Authorization: `Bearer ${input.apiKey}`, "Content-Type": "application/json" },
      body: JSON.stringify({
        model: "gpt-5.6-sol",
        messages: [{ role: "user", content: `Reply with ${marker} only.` }],
        max_tokens: 32,
        stream: true,
      }),
      signal: AbortSignal.timeout(60_000),
    });
    const diagnostics = await diagnoseOpenAiChatSseResponse(response, marker);
    return {
      ok: diagnostics.ok,
      httpStatus: diagnostics.httpStatus,
      evidence: [
        `classification=${diagnostics.classification}`,
        `contentType=${diagnostics.contentType ?? "missing"}`,
        `dataEvents=${diagnostics.dataEventCount}`,
        `done=${diagnostics.done}`,
        `contentMatches=${diagnostics.contentMatches}`,
        `bytes=${diagnostics.totalBytes}`,
        `bodySha256=${diagnostics.bodySha256}`,
      ].join(";"),
      diagnostics,
    };
  } catch (error) {
    return { ok: false, httpStatus: null, evidence: boundedEvidence(safeErrorMessage(error)) };
  }
}
