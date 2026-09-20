import { describe, expect, it } from "vitest";

import {
  diagnoseOpenAiChatSseResponse,
  parseOpenAiChatSse,
} from "../../apps/control-plane/src/campaign/sse-parser.js";

function byteStream(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
}

describe("P03 OpenAI-compatible SSE parsing", () => {
  it("handles CRLF, chunk splits, multiline data and DONE", async () => {
    const parsed = await parseOpenAiChatSse(byteStream([
      ": keepalive\r",
      "\nevent: message\r\ndata: {\"choices\":[{\"delta\":{\"content\":\"QF_P03_\"}}]}\r\n\r",
      "\ndata: {\"choices\":[{\"delta\":\r\n",
      "data: {\"content\":\"STREAM_READY\"}}]}\r\n\r\n",
      "data: [DO",
      "NE]\r\n\r\n",
    ]));

    expect(parsed.content).toBe("QF_P03_STREAM_READY");
    expect(parsed.done).toBe(true);
    expect(parsed.dataEventCount).toBe(3);
    expect(parsed.invalidJsonEventCount).toBe(0);
    expect(parsed.transportChunkCount).toBe(6);
  });

  it("recognizes SSE error events without exposing arbitrary fields", async () => {
    const parsed = await parseOpenAiChatSse(byteStream([
      "event: error\n",
      "data: {\"error\":{\"type\":\"upstream_error\",\"code\":\"rate_limited\",\"message\":\"bounded failure\",\"secret\":\"must-not-leak\"}}\n\n",
    ]));

    expect(parsed.providerError).toEqual({
      type: "upstream_error",
      code: "rate_limited",
      message: "bounded failure",
    });
    expect(JSON.stringify(parsed)).not.toContain("must-not-leak");
  });

  it("classifies a non-SSE JSON response as a provider protocol mismatch", async () => {
    const response = new Response(JSON.stringify({ choices: [{ message: { content: "QF_P03_STREAM_READY" } }] }), {
      status: 200,
      headers: { "content-type": "application/json; charset=utf-8" },
    });
    const diagnosis = await diagnoseOpenAiChatSseResponse(response, "QF_P03_STREAM_READY");

    expect(diagnosis.ok).toBe(false);
    expect(diagnosis.classification).toBe("PROVIDER_NON_SSE");
    expect(diagnosis.bodyFormat).toBe("json");
    expect(diagnosis.bodySha256).toMatch(/^[a-f0-9]{64}$/u);
    expect(JSON.stringify(diagnosis)).not.toContain("message.content");
  });
});
