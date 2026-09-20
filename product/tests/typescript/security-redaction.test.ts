import { afterEach, describe, expect, it } from "vitest";

import { redactSecrets, safeErrorMessage } from "../../apps/control-plane/src/security/redaction.js";

const previousOpenAiKey = process.env.OPENAI_API_KEY;

afterEach(() => {
  if (previousOpenAiKey === undefined) delete process.env.OPENAI_API_KEY;
  else process.env.OPENAI_API_KEY = previousOpenAiKey;
});

describe("secret redaction", () => {
  it("redacts bearer headers and configured secret values", () => {
    process.env.OPENAI_API_KEY = "qf-test-secret-value";
    const redacted = redactSecrets(
      "Authorization: Bearer qf-bearer-token; upstream echoed qf-test-secret-value",
    );
    expect(redacted).not.toContain("qf-bearer-token");
    expect(redacted).not.toContain("qf-test-secret-value");
    expect(redacted.match(/\[REDACTED\]/gu)).toHaveLength(2);
  });

  it("applies the same redaction to structured error messages", () => {
    process.env.OPENAI_API_KEY = "qf-test-secret-value";
    expect(safeErrorMessage(new Error("provider returned qf-test-secret-value")))
      .toBe("provider returned [REDACTED]");
  });

  it("keeps only stable code and message from IPC error objects", () => {
    process.env.OPENAI_API_KEY = "qf-test-secret-value";
    expect(safeErrorMessage({
      code: "OPENHANDS_SANDBOX_FAILED",
      message: "provider returned qf-test-secret-value",
      apiKey: "must-not-be-rendered",
    })).toBe("OPENHANDS_SANDBOX_FAILED: provider returned [REDACTED]");
  });

  it("redacts cookies and generic key fields before either UI column can render them", () => {
    const redacted = redactSecrets(
      'Cookie: session=abc123\n{"connection_key":"private-value","access-token":"token-value"}',
    );
    expect(redacted).toContain("Cookie: [REDACTED]");
    expect(redacted).not.toContain("abc123");
    expect(redacted).not.toContain("private-value");
    expect(redacted).not.toContain("token-value");
  });
});
