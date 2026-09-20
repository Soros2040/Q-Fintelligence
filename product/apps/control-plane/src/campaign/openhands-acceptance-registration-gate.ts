import type { Readable } from "node:stream";
import { createInterface } from "node:readline";

import type { OpenHandsAcceptanceLane } from "./openhands-acceptance-repository.js";

export interface OpenHandsAcceptanceRegistrationGateExpectation {
  token: string;
  processKey: string;
  processId: number;
  campaignId: string;
  runId: string;
  lane: OpenHandsAcceptanceLane;
  recovery: boolean;
  expectedLeaseGeneration: number;
}

function parseGate(line: string): Record<string, unknown> {
  const parsed = JSON.parse(line) as unknown;
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error("OpenHands acceptance registration gate must be a JSON object");
  }
  return parsed as Record<string, unknown>;
}

function registrationGateIdentityMismatches(
  gate: Record<string, unknown>,
  expected: OpenHandsAcceptanceRegistrationGateExpectation,
): string[] {
  const expectedIdentity: Record<string, unknown> = {
    schemaVersion: "qf.openhands-acceptance-registration-gate.v1",
    token: expected.token,
    processKey: expected.processKey,
    processId: expected.processId,
    campaignId: expected.campaignId,
    runId: expected.runId,
    lane: expected.lane,
    recovery: expected.recovery,
    expectedLeaseGeneration: expected.expectedLeaseGeneration,
  };
  return Object.entries(expectedIdentity)
    .filter(([field, expectedValue]) => gate[field] !== expectedValue)
    .map(([field]) => field);
}

export async function waitForOpenHandsAcceptanceRegistrationGate(
  input: Readable,
  expected: OpenHandsAcceptanceRegistrationGateExpectation,
  timeoutMilliseconds = 30_000,
): Promise<void> {
  if (!expected.token || !expected.processKey || !Number.isSafeInteger(expected.processId) || expected.processId <= 1) {
    throw new Error("OpenHands acceptance registration-gate expectation is invalid");
  }
  const line = await new Promise<string>((resolve, reject) => {
    const lines = createInterface({ input, crlfDelay: Infinity });
    let settled = false;
    const finish = (operation: () => void): void => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      lines.close();
      operation();
    };
    const timer = setTimeout(
      () => finish(() => reject(new Error("OpenHands acceptance registration gate timed out"))),
      timeoutMilliseconds,
    );
    timer.unref();
    lines.once("line", (value) => finish(() => resolve(value)));
    lines.once("close", () => finish(() => reject(new Error("OpenHands acceptance registration gate closed before release"))));
    lines.once("error", (error) => finish(() => reject(error)));
  });
  const gate = parseGate(line);
  const mismatches = registrationGateIdentityMismatches(gate, expected);
  if (mismatches.length > 0) {
    throw new Error(`OpenHands acceptance registration gate identity mismatch: ${mismatches.join(", ")}`);
  }
}
