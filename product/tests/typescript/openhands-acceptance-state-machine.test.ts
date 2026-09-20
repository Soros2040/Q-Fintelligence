import { PassThrough } from "node:stream";

import { describe, expect, it } from "vitest";

import { waitForOpenHandsAcceptanceRegistrationGate } from "../../apps/control-plane/src/campaign/openhands-acceptance-registration-gate.js";
import {
  laneRegistrationAnchor,
  laneResumeRegistrationAction,
  plannedFaultProcessAction,
  scienceFaultRunAction,
  startupProcessAction,
} from "../../scripts/openhands-acceptance-state-machine.mjs";

const expectation = {
  token: "registration-token",
  processKey: "openhands-acceptance-science:campaign-test:run-test",
  processId: 42,
  campaignId: "campaign-test",
  runId: "run-test",
  lane: "quantum_science" as const,
  recovery: false,
  expectedLeaseGeneration: 0,
};

function gatePayload(overrides: Record<string, unknown> = {}): string {
  return `${JSON.stringify({
    schemaVersion: "qf.openhands-acceptance-registration-gate.v1",
    ...expectation,
    ...overrides,
  })}\n`;
}

describe("OpenHands acceptance startup and fault orchestration state machine", () => {
  it("keeps a Worker behind the pipe gate until its exact central registration is released", async () => {
    const input = new PassThrough();
    let released = false;
    const waiting = waitForOpenHandsAcceptanceRegistrationGate(input, expectation, 1_000)
      .then(() => { released = true; });
    await new Promise<void>((resolve) => setImmediate(resolve));
    expect(released).toBe(false);
    input.end(gatePayload());
    await waiting;
    expect(released).toBe(true);
  });

  it("rejects forged or closed registration gates before lease acquisition", async () => {
    const forged = new PassThrough();
    const rejected = waitForOpenHandsAcceptanceRegistrationGate(forged, expectation, 1_000);
    forged.end(gatePayload({ processId: expectation.processId + 1 }));
    await expect(rejected).rejects.toThrow("registration gate identity mismatch: processId");

    const forgedToken = new PassThrough();
    const rejectedToken = waitForOpenHandsAcceptanceRegistrationGate(forgedToken, expectation, 1_000);
    forgedToken.end(gatePayload({ token: "secret-that-must-not-be-reflected" }));
    await expect(rejectedToken).rejects.toThrow("registration gate identity mismatch: token");
    await expect(rejectedToken).rejects.not.toThrow("secret-that-must-not-be-reflected");

    const closed = new PassThrough();
    const missing = waitForOpenHandsAcceptanceRegistrationGate(closed, expectation, 1_000);
    closed.end();
    await expect(missing).rejects.toThrow("closed before release");
  });

  it("never selects a replacement PID when retrying a kill-before-mark PLANNED fault", () => {
    const fault = {
      state: "PLANNED",
      targetProcessId: 101,
      targetProcessKey: "openhands-acceptance-science:campaign-test:run-test",
    };
    expect(plannedFaultProcessAction(fault, {
      pid: 101,
      processKey: fault.targetProcessKey,
    })).toBe("TERMINATE_PLANNED_TARGET");
    expect(plannedFaultProcessAction(fault, {
      pid: 202,
      processKey: fault.targetProcessKey,
    })).toBe("CONFIRM_PLANNED_TARGET_DEAD");
    expect(plannedFaultProcessAction(fault, null)).toBe("CONFIRM_PLANNED_TARGET_DEAD");
  });

  it("compensates an unmarked RUNNING startup after orchestrator death and seals a fully attached set", () => {
    const campaign = {
      campaignId: "campaign-startup",
      status: "RUNNING",
      runs: [
        { runId: "run-openhands", lane: "openhands_orchestration", status: "RUNNING", workerId: null, processId: null },
        { runId: "run-science", lane: "quantum_science", status: "RUNNING", workerId: null, processId: null },
        { runId: "run-audit", lane: "evidence_audit", status: "RUNNING", workerId: null, processId: null },
      ],
    };
    expect(startupProcessAction(campaign, [], false)).toBe("COMPENSATE_PARTIAL");
    const attached = campaign.runs.map((run, index) => ({
      ...run,
      workerId: `worker-${index}`,
      processId: 5_100 + index,
    }));
    const registered = attached.map((run) => ({
      runId: run.runId,
      lane: run.lane,
      pid: run.processId,
      live: true,
      processKey: run.lane === "quantum_science"
        ? `openhands-acceptance-science:${campaign.campaignId}:${run.runId}`
        : `openhands-acceptance-lane:${campaign.campaignId}:${run.runId}`,
    }));
    expect(startupProcessAction({ ...campaign, runs: attached }, registered, false)).toBe("MARK_COMPLETE");
    expect(startupProcessAction({ ...campaign, runs: attached }, registered, true)).toBe("REUSE_COMPLETE");
  });

  it("marks a dead old science fault even after the replacement has attached", () => {
    const fault = { state: "PLANNED", targetProcessId: 6_100 };
    expect(scienceFaultRunAction(fault, {
      status: "RECOVERING",
      workerId: "old-worker",
      processId: 6_100,
      leaseGeneration: 1,
    })).toBe("MARK_RECOVERING");
    expect(scienceFaultRunAction(fault, {
      status: "RUNNING",
      workerId: "replacement-worker",
      processId: 6_101,
      leaseGeneration: 2,
    })).toBe("MARK_WITH_REPLACEMENT");
  });

  it("replaces a dead gate-window registration even when the DB still retains the older PID", () => {
    const run = {
      runId: "run-science",
      lane: "quantum_science",
      status: "RECOVERING",
      processId: 7_100,
      leaseGeneration: 1,
    };
    const replacement = {
      processKey: "openhands-acceptance-science:campaign-gate:run-science",
      pid: 7_101,
      kind: "OPENHANDS_ACCEPTANCE_SCIENCE_WORKER",
      campaignId: "campaign-gate",
      runId: "run-science",
      lane: "quantum_science",
      expectedLeaseGeneration: 1,
      replacementAnchor: {
        processKey: "openhands-acceptance-science:campaign-gate:run-science",
        pid: 7_100,
        kind: "OPENHANDS_ACCEPTANCE_SCIENCE_WORKER",
        campaignId: "campaign-gate",
        runId: "run-science",
        lane: "quantum_science",
        expectedLeaseGeneration: 0,
      },
    };
    expect(laneResumeRegistrationAction(
      run,
      replacement,
      false,
    )).toBe("REPLACE_DEAD_REGISTRATION");
    expect(laneRegistrationAnchor("campaign-gate", run, replacement)).toEqual(replacement.replacementAnchor);
    expect(() => laneRegistrationAnchor("campaign-gate", { ...run, leaseGeneration: 2 }, replacement))
      .toThrow("does not match the DB lease");
    expect(() => laneResumeRegistrationAction(
      run,
      replacement,
      true,
    )).toThrow("cannot converge");
  });
});
