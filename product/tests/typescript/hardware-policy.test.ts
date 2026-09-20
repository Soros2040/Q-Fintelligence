import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { QfToolHost } from "../../apps/control-plane/src/agent/tools.js";
import { runTianyanJob } from "../../apps/control-plane/src/campaign/python-runner.js";
import { openDatabase } from "../../apps/control-plane/src/db/migrations.js";
import { WorkspaceRepository } from "../../apps/control-plane/src/db/repository.js";
import {
  assertHardwareMutationAuthorized,
  loadHardwareExecutionPolicy,
  READ_ONLY_HARDWARE_POLICY,
} from "../../apps/control-plane/src/hardware-policy.js";

const temporaryRoots: string[] = [];

afterEach(async () => {
  await Promise.all(temporaryRoots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
});

describe("current execution hardware policy", () => {
  it("defaults fail-closed and rejects inconsistent or historical authorization", () => {
    expect(loadHardwareExecutionPolicy({})).toEqual(READ_ONLY_HARDWARE_POLICY);
    expect(() => loadHardwareExecutionPolicy({
      HARDWARE_MODE: "READ_ONLY",
      MAX_NEW_HARDWARE_JOBS: "1",
      SHOTS_PER_JOB: "100",
    })).toThrow(/READ_ONLY requires/);
    expect(() => loadHardwareExecutionPolicy({
      HARDWARE_MODE: "ONE_JOB",
      HARDWARE_TARGET: "tianyan176",
      MAX_NEW_HARDWARE_JOBS: "1",
      SHOTS_PER_JOB: "100",
      HARDWARE_AUTHORIZATION_BASIS: "P15_USER_REQUEST_20260725",
    })).toThrow(/historical/);
  });

  it("blocks every mutating QF hardware tool before approvals, Campaigns, or external calls", async () => {
    const directory = await mkdtemp(path.join(os.tmpdir(), "qf-read-only-policy-"));
    temporaryRoots.push(directory);
    const opened = openDatabase(path.join(directory, "state.sqlite3"), path.resolve("infra/sqlite"));
    const repository = new WorkspaceRepository(opened.database);
    const project = repository.createProject("read-only hardware boundary");
    const conversation = repository.createConversation({
      projectId: project.projectId,
      title: "new OpenHands acceptance conversation",
      mode: "OPENHANDS",
      provider: "deepseek",
      modelId: "deepseek-v4-pro",
    });
    let p15Launches = 0;
    const host = new QfToolHost(
      repository,
      conversation.conversationId,
      path.join(directory, "artifacts"),
      process.cwd(),
      undefined,
      () => {
        p15Launches += 1;
        return {};
      },
      undefined,
      READ_ONLY_HARDWARE_POLICY,
    );

    await expect(host.execute("request_approval", {
      action: "SUBMIT_HARDWARE",
      authorizationBasis: "P15_USER_REQUEST_20260725",
      subjectHash: "a".repeat(64),
      rationale: "historical approval must not transfer",
    })).rejects.toThrow(/NOT_AUTHORIZED/);
    await expect(host.execute("start_p15_portfolio_campaign", {
      authorizationBasis: "P15_USER_REQUEST_20260725",
    })).rejects.toThrow(/NOT_AUTHORIZED/);
    await expect(host.execute("prepare_p16_hardware_batch", {
      authorizationBasis: "P16_USER_REQUEST_20260725",
    })).rejects.toThrow(/NOT_AUTHORIZED/);

    expect(repository.listApprovals(conversation.conversationId)).toEqual([]);
    expect(p15Launches).toBe(0);
    repository.close();
  });

  it("blocks the TianYan adapter before credentials or a child process are consulted", async () => {
    const previousMode = process.env.HARDWARE_MODE;
    const previousJobs = process.env.MAX_NEW_HARDWARE_JOBS;
    const previousShots = process.env.SHOTS_PER_JOB;
    const previousKey = process.env.TIANYAN_CONNECTION_KEY;
    process.env.HARDWARE_MODE = "READ_ONLY";
    process.env.MAX_NEW_HARDWARE_JOBS = "0";
    process.env.SHOTS_PER_JOB = "0";
    delete process.env.TIANYAN_CONNECTION_KEY;
    try {
      await expect(runTianyanJob({
        projectRoot: process.cwd(),
        request: {
          action: "submit",
          authorization_basis: "P04_USER_AUTHORIZATION_20260722",
          machine_name: "tianyan176",
          shots: 100,
        },
      })).rejects.toThrow(/NOT_AUTHORIZED/);
    } finally {
      if (previousMode === undefined) delete process.env.HARDWARE_MODE;
      else process.env.HARDWARE_MODE = previousMode;
      if (previousJobs === undefined) delete process.env.MAX_NEW_HARDWARE_JOBS;
      else process.env.MAX_NEW_HARDWARE_JOBS = previousJobs;
      if (previousShots === undefined) delete process.env.SHOTS_PER_JOB;
      else process.env.SHOTS_PER_JOB = previousShots;
      if (previousKey === undefined) delete process.env.TIANYAN_CONNECTION_KEY;
      else process.env.TIANYAN_CONNECTION_KEY = previousKey;
    }
  });

  it("permits only a fresh exact one-Job policy", () => {
    const policy = loadHardwareExecutionPolicy({
      HARDWARE_MODE: "ONE_JOB",
      HARDWARE_TARGET: "tianyan176",
      MAX_NEW_HARDWARE_JOBS: "1",
      SHOTS_PER_JOB: "100",
      HARDWARE_AUTHORIZATION_BASIS: "QF_CURRENT_TEST_AUTHORIZATION",
    });
    expect(() => assertHardwareMutationAuthorized(policy, {
      operation: "test submission",
      authorizationBasis: "QF_CURRENT_TEST_AUTHORIZATION",
      jobCount: 1,
      shotsPerJob: 100,
      target: "tianyan176",
    })).not.toThrow();
    expect(() => assertHardwareMutationAuthorized(policy, {
      operation: "test submission",
      authorizationBasis: "QF_CURRENT_TEST_AUTHORIZATION",
      jobCount: 2,
      shotsPerJob: 100,
      target: "tianyan176",
    })).toThrow(/exceeds/);
  });
});
