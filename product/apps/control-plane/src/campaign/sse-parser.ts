import { createHash } from "node:crypto";

export interface OpenAiChatSseParseResult {
  content: string;
  done: boolean;
  dataEventCount: number;
  eventCount: number;
  invalidJsonEventCount: number;
  finishReasons: string[];
  providerError: { type: string | null; code: string | null; message: string } | null;
  totalBytes: number;
  transportChunkCount: number;
  transportChunkSizes: number[];
  bodySha256: string;
  bodyFormat: "empty" | "json" | "sse" | "text";
  topLevelJsonKeys: string[];
  lineEndings: { crlf: number; lf: number; cr: number };
}

export interface OpenAiChatSseDiagnosis {
  ok: boolean;
  classification:
    | "READY"
    | "HTTP_ERROR"
    | "PROVIDER_NON_SSE"
    | "PROVIDER_SSE_ERROR"
    | "EMPTY_EVENT_STREAM"
    | "MALFORMED_SSE"
    | "TRUNCATED_SSE"
    | "CONTENT_MISMATCH";
  httpStatus: number;
  contentType: string | null;
  contentEncoding: string | null;
  transferEncoding: string | null;
  cacheControl: string | null;
  contentMatches: boolean;
  contentCharacters: number;
  done: boolean;
  dataEventCount: number;
  eventCount: number;
  invalidJsonEventCount: number;
  finishReasons: string[];
  providerError: OpenAiChatSseParseResult["providerError"];
  totalBytes: number;
  transportChunkCount: number;
  transportChunkSizes: number[];
  bodySha256: string;
  bodyFormat: OpenAiChatSseParseResult["bodyFormat"];
  topLevelJsonKeys: string[];
  lineEndings: OpenAiChatSseParseResult["lineEndings"];
}

function boundedProviderError(value: unknown): OpenAiChatSseParseResult["providerError"] {
  if (!value || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  const nested = record.error && typeof record.error === "object"
    ? record.error as Record<string, unknown>
    : record;
  const message = typeof nested.message === "string" ? nested.message : "provider SSE error";
  return {
    type: typeof nested.type === "string" ? nested.type.slice(0, 120) : null,
    code: typeof nested.code === "string" ? nested.code.slice(0, 120) : null,
    message: message.replace(/Bearer\s+\S+/giu, "Bearer [REDACTED]").slice(0, 500),
  };
}

function bodyShape(text: string): { format: OpenAiChatSseParseResult["bodyFormat"]; keys: string[] } {
  const trimmed = text.trim();
  if (!trimmed) return { format: "empty", keys: [] };
  try {
    const parsed = JSON.parse(trimmed) as unknown;
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return { format: "json", keys: Object.keys(parsed as Record<string, unknown>).sort().slice(0, 20) };
    }
  } catch {
    // Shape detection is deliberately best-effort and never exposes the body.
  }
  return { format: /(^|[\r\n])(?:data|event):/u.test(trimmed) ? "sse" : "text", keys: [] };
}

export async function parseOpenAiChatSse(stream: ReadableStream<Uint8Array>): Promise<OpenAiChatSseParseResult> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  const digest = createHash("sha256");
  const chunkSizes: number[] = [];
  const finishReasons = new Set<string>();
  const lineEndings = { crlf: 0, lf: 0, cr: 0 };
  let textBuffer = "";
  let shapeText = "";
  let eventName = "message";
  let dataLines: string[] = [];
  let content = "";
  let done = false;
  let dataEventCount = 0;
  let eventCount = 0;
  let invalidJsonEventCount = 0;
  let providerError: OpenAiChatSseParseResult["providerError"] = null;
  let totalBytes = 0;
  let transportChunkCount = 0;

  const dispatch = () => {
    if (dataLines.length === 0 && eventName === "message") return;
    eventCount += 1;
    if (dataLines.length === 0) {
      eventName = "message";
      return;
    }
    dataEventCount += 1;
    const data = dataLines.join("\n");
    dataLines = [];
    if (data.trim() === "[DONE]") {
      done = true;
      eventName = "message";
      return;
    }
    try {
      const parsed = JSON.parse(data) as Record<string, unknown>;
      if (eventName === "error" || parsed.error) providerError ??= boundedProviderError(parsed);
      const choices = Array.isArray(parsed.choices) ? parsed.choices as Array<Record<string, unknown>> : [];
      for (const choice of choices) {
        const delta = choice.delta && typeof choice.delta === "object" ? choice.delta as Record<string, unknown> : null;
        if (delta && typeof delta.content === "string") content += delta.content;
        if (typeof choice.finish_reason === "string") finishReasons.add(choice.finish_reason);
      }
    } catch {
      invalidJsonEventCount += 1;
      if (eventName === "error") providerError ??= {
        type: "invalid_error_event",
        code: null,
        message: "provider returned a non-JSON SSE error event",
      };
    }
    eventName = "message";
  };

  const processLine = (line: string) => {
    if (line === "") {
      dispatch();
      return;
    }
    if (line.startsWith(":")) return;
    const separator = line.indexOf(":");
    const field = separator === -1 ? line : line.slice(0, separator);
    let value = separator === -1 ? "" : line.slice(separator + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "data") dataLines.push(value);
    if (field === "event") eventName = value || "message";
  };

  const consumeLines = (final: boolean) => {
    let start = 0;
    for (let index = 0; index < textBuffer.length; index += 1) {
      const character = textBuffer[index];
      if (character !== "\r" && character !== "\n") continue;
      if (character === "\r" && index === textBuffer.length - 1 && !final) break;
      processLine(textBuffer.slice(start, index));
      if (character === "\r" && textBuffer[index + 1] === "\n") {
        lineEndings.crlf += 1;
        index += 1;
      } else if (character === "\r") {
        lineEndings.cr += 1;
      } else {
        lineEndings.lf += 1;
      }
      start = index + 1;
    }
    textBuffer = textBuffer.slice(start);
    if (final && textBuffer.length > 0) {
      processLine(textBuffer);
      textBuffer = "";
    }
  };

  while (true) {
    const { done: streamDone, value } = await reader.read();
    if (streamDone) break;
    if (!value) continue;
    digest.update(value);
    totalBytes += value.byteLength;
    transportChunkCount += 1;
    if (chunkSizes.length < 64) chunkSizes.push(value.byteLength);
    const decoded = decoder.decode(value, { stream: true });
    if (shapeText.length < 65_536) shapeText += decoded.slice(0, 65_536 - shapeText.length);
    textBuffer += decoded;
    consumeLines(false);
  }
  const tail = decoder.decode();
  if (shapeText.length < 65_536) shapeText += tail.slice(0, 65_536 - shapeText.length);
  textBuffer += tail;
  consumeLines(true);
  dispatch();
  const shape = bodyShape(shapeText);
  return {
    content,
    done,
    dataEventCount,
    eventCount,
    invalidJsonEventCount,
    finishReasons: [...finishReasons].sort(),
    providerError,
    totalBytes,
    transportChunkCount,
    transportChunkSizes: chunkSizes,
    bodySha256: digest.digest("hex"),
    bodyFormat: shape.format,
    topLevelJsonKeys: shape.keys,
    lineEndings,
  };
}

export async function diagnoseOpenAiChatSseResponse(
  response: Response,
  expectedContent: string,
): Promise<OpenAiChatSseDiagnosis> {
  const empty = new ReadableStream<Uint8Array>({ start: (controller) => controller.close() });
  const parsed = await parseOpenAiChatSse(response.body ?? empty);
  const contentType = response.headers.get("content-type");
  const isEventStream = /^text\/event-stream(?:;|$)/iu.test(contentType ?? "");
  const contentMatches = parsed.content.includes(expectedContent);
  let classification: OpenAiChatSseDiagnosis["classification"] = "READY";
  if (!response.ok) classification = "HTTP_ERROR";
  else if (!isEventStream) classification = "PROVIDER_NON_SSE";
  else if (parsed.providerError) classification = "PROVIDER_SSE_ERROR";
  else if (parsed.dataEventCount === 0) classification = "EMPTY_EVENT_STREAM";
  else if (parsed.invalidJsonEventCount > 0) classification = "MALFORMED_SSE";
  else if (!parsed.done) classification = "TRUNCATED_SSE";
  else if (!contentMatches) classification = "CONTENT_MISMATCH";
  return {
    ok: classification === "READY",
    classification,
    httpStatus: response.status,
    contentType,
    contentEncoding: response.headers.get("content-encoding"),
    transferEncoding: response.headers.get("transfer-encoding"),
    cacheControl: response.headers.get("cache-control"),
    contentMatches,
    contentCharacters: parsed.content.length,
    done: parsed.done,
    dataEventCount: parsed.dataEventCount,
    eventCount: parsed.eventCount,
    invalidJsonEventCount: parsed.invalidJsonEventCount,
    finishReasons: parsed.finishReasons,
    providerError: parsed.providerError,
    totalBytes: parsed.totalBytes,
    transportChunkCount: parsed.transportChunkCount,
    transportChunkSizes: parsed.transportChunkSizes,
    bodySha256: parsed.bodySha256,
    bodyFormat: parsed.bodyFormat,
    topLevelJsonKeys: parsed.topLevelJsonKeys,
    lineEndings: parsed.lineEndings,
  };
}
