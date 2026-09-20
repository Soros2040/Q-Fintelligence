import { spawn, type ChildProcess } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { createApplication, type QfApplication } from "../../apps/control-plane/src/app.js";
import type { RuntimeConfig } from "../../apps/control-plane/src/config.js";
import { openDatabase } from "../../apps/control-plane/src/db/migrations.js";
import { READ_ONLY_HARDWARE_POLICY } from "../../apps/control-plane/src/hardware-policy.js";

const temporaryRoots: string[] = [];
const applications: QfApplication[] = [];
const children: ChildProcess[] = [];

async function waitForExit(child: ChildProcess): Promise<void> {
  if (child.exitCode !== null || child.signalCode !== null) return;
  await new Promise<void>((resolve) => child.once("exit", () => resolve()));
}

afterEach(async () => {
  for (const child of children.splice(0)) {
    if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
    await waitForExit(child);
  }
  await Promise.all(applications.splice(0).map(({ app }) => app.close()));
  await Promise.all(temporaryRoots.splice(0).map((directory) => rm(directory, { recursive: true, force: true })));
});

async function setup(): Promise<QfApplication> {
  const directory = await mkdtemp(path.join(os.tmpdir(), "qf-openhands-acceptance-"));
  temporaryRoots.push(directory);
  const config: RuntimeConfig = {
    project: "q-fintelligence",
    processNamespace: "qfintelligence",
    apiHost: "127.0.0.1",
    apiPort: 27_872,
    webPort: 27_871,
    artifactRoot: path.join(directory, "artifacts"),
    sqlitePath: path.join(directory, "state.sqlite3"),
    stateRoot: directory,
    openHandsSessionRoot: path.join(directory, "openhands-sessions"),
    openHandsSidecarPath: process.execPath,
    openHandsSidecarArguments: [path.resolve("tests/fixtures/openhands-sidecar-fake.mjs")],
    systemPromptVersion: "qf-agent-p0.v1",
    agentMode: "hybrid",
    hardwarePolicy: READ_ONLY_HARDWARE_POLICY,
  };
  const application = createApplication({ projectRoot: process.cwd(), config, databasePath: config.sqlitePath, logger: false });
  applications.push(application);
  return application;
}

function seedPassedCanary(application: QfApplication): string {
  const project = application.repository.createProject(
    "OpenHands 重构后两小时量子验收 · API fixture",
    "read-only acceptance fixture",
  );
  const conversation = application.repository.createConversation({
    projectId: project.projectId,
    title: "OpenHands acceptance canary",
    mode: "OPENHANDS",
    provider: "deepseek",
    modelId: "deepseek-v4-pro",
  });
  application.repository.upsertOpenHandsSession({
    conversationId: conversation.conversationId,
    sessionId: "11111111-1111-4111-8111-111111111111",
    relativeSessionFile: "fixture/session",
    provider: "deepseek",
    modelId: "deepseek-v4-pro",
    promptVersion: "qf-agent-p0.v1",
    promptHash: "a".repeat(64),
    lifecycleState: "READY",
    runtimeRevision: "qf-openhands-adapter.v2/1.39.0+qf.noobservability.1/r1",
    configHash: "b".repeat(64),
    recoveryCursor: 4,
  });
  application.repository.appendEvent({
    conversationId: conversation.conversationId,
    type: "model.usage",
    payload: { provider: "deepseek", modelId: "deepseek-v4-pro", input: 101, output: 29, totalTokens: 130 },
  });
  for (const [sequence, toolName] of ["get_task_context", "list_project_sources"].entries()) {
    application.repository.appendEvent({
      conversationId: conversation.conversationId,
      type: "tool.started",
      payload: { toolName, toolCallId: `tool-${sequence}` },
    });
    application.repository.appendEvent({
      conversationId: conversation.conversationId,
      type: "tool.completed",
      payload: { toolName, toolCallId: `tool-${sequence}`, isError: false },
    });
  }
  return conversation.conversationId;
}

describe("OpenHands two-hour acceptance Campaign", () => {
  it("creates an isolated three-lane Campaign only after a real-usage two-tool canary", async () => {
    const application = await setup();
    const conversationId = seedPassedCanary(application);
    const response = await application.app.inject({
      method: "POST",
      url: "/api/openhands-acceptance/campaigns",
      payload: {
        conversationId,
        fixedProviderModel: "deepseek/deepseek-v4-pro",
        minimumWallClockSeconds: 7_200,
        minimumOverlapSeconds: 6_900,
        hardwareMode: "READ_ONLY",
        hardwareTarget: "tianyan176",
        maxNewHardwareJobs: 0,
        shotsPerJob: 0,
        gitAction: "NONE",
      },
    });
    expect(response.statusCode, response.body).toBe(201);
    const campaign = response.json();
    expect(campaign).toMatchObject({
      status: "CREATED",
      fixedProviderModel: "deepseek/deepseek-v4-pro",
      minimumWallClockSeconds: 7_200,
      minimumOverlapSeconds: 6_900,
      hardwareMode: "READ_ONLY",
      hardwareTarget: "tianyan176",
      maxNewHardwareJobs: 0,
      shotsPerJob: 0,
      formalTestSealed: true,
      providerCalls: 1,
      providerPromptTokens: 101,
      providerCompletionTokens: 29,
      hardwareJobsCreated: 0,
    });
    expect(campaign.runs.map((run: { lane: string }) => run.lane).sort()).toEqual([
      "evidence_audit",
      "openhands_orchestration",
      "quantum_science",
    ]);
    expect(new Set(campaign.runs.map((run: { relativeWorkspace: string }) => run.relativeWorkspace)).size).toBe(3);
  });

  it("fails closed on duration or hardware authorization drift", async () => {
    const application = await setup();
    const conversationId = seedPassedCanary(application);
    const response = await application.app.inject({
      method: "POST",
      url: "/api/openhands-acceptance/campaigns",
      payload: {
        conversationId,
        fixedProviderModel: "deepseek/deepseek-v4-pro",
        minimumWallClockSeconds: 7_199,
        minimumOverlapSeconds: 6_899,
        hardwareMode: "ONE_JOB",
        hardwareTarget: "tianyan176",
        maxNewHardwareJobs: 1,
        shotsPerJob: 100,
        gitAction: "NONE",
      },
    });
    expect(response.statusCode).toBe(400);
    expect(application.openHandsAcceptanceService.list()).toHaveLength(0);
  });

  it("refuses Campaign creation before the mandatory OpenHands canary", async () => {
    const application = await setup();
    const project = application.repository.createProject("OpenHands 重构后两小时量子验收 · missing canary");
    const conversation = application.repository.createConversation({
      projectId: project.projectId,
      title: "missing canary",
      mode: "OPENHANDS",
      provider: "deepseek",
      modelId: "deepseek-v4-pro",
    });
    const response = await application.app.inject({
      method: "POST",
      url: "/api/openhands-acceptance/campaigns",
      payload: {
        conversationId: conversation.conversationId,
        fixedProviderModel: "deepseek/deepseek-v4-pro",
        minimumWallClockSeconds: 7_200,
        minimumOverlapSeconds: 6_900,
        hardwareMode: "READ_ONLY",
        hardwareTarget: "tianyan176",
        maxNewHardwareJobs: 0,
        shotsPerJob: 0,
        gitAction: "NONE",
      },
    });
    expect(response.statusCode).toBe(409);
    expect(application.openHandsAcceptanceService.list()).toHaveLength(0);
  });

  it("rejects a stale lease generation during exact Run resume", async () => {
    const application = await setup();
    const conversationId = seedPassedCanary(application);
    const created = application.openHandsAcceptanceService.create({
      conversationId,
      fixedProviderModel: "deepseek/deepseek-v4-pro",
      minimumWallClockSeconds: 7_200,
      minimumOverlapSeconds: 6_900,
      hardwareMode: "READ_ONLY",
      hardwareTarget: "tianyan176",
      maxNewHardwareJobs: 0,
      shotsPerJob: 0,
      gitAction: "NONE",
    });
    const started = application.openHandsAcceptanceService.start(created.campaignId);
    const run = started.runs.find((item) => item.lane === "quantum_science")!;
    const attached = application.openHandsAcceptanceRepository.attachWorker(
      run.runId,
      "science-worker-original",
      process.pid,
      false,
      run.leaseGeneration,
    );
    application.openHandsAcceptanceRepository.prepareRunRecovery(
      run.runId,
      "science-worker-original",
      attached.leaseGeneration,
    );

    expect(() => application.openHandsAcceptanceRepository.attachWorker(
      run.runId,
      "science-worker-stale-resume",
      process.pid,
      true,
      run.leaseGeneration,
    )).toThrow("lease CAS was rejected");
    const resumed = application.openHandsAcceptanceRepository.attachWorker(
      run.runId,
      "science-worker-current-resume",
      process.pid,
      true,
      attached.leaseGeneration,
    );
    expect(resumed).toMatchObject({
      runId: run.runId,
      workerId: "science-worker-current-resume",
      leaseGeneration: attached.leaseGeneration + 1,
      recoveryCount: 1,
    });
  });

  it("compensates a partially attached startup and retries every lane through recovery leases", async () => {
    const application = await setup();
    const conversationId = seedPassedCanary(application);
    const created = application.openHandsAcceptanceService.create({
      conversationId,
      fixedProviderModel: "deepseek/deepseek-v4-pro",
      minimumWallClockSeconds: 7_200,
      minimumOverlapSeconds: 6_900,
      hardwareMode: "READ_ONLY",
      hardwareTarget: "tianyan176",
      maxNewHardwareJobs: 0,
      shotsPerJob: 0,
      gitAction: "NONE",
    });
    const started = application.openHandsAcceptanceService.start(created.campaignId);
    const science = started.runs.find((run) => run.lane === "quantum_science")!;
    const attached = application.openHandsAcceptanceRepository.attachWorker(
      science.runId,
      "partial-start-science",
      process.pid,
      false,
      science.leaseGeneration,
    );

    const compensated = application.openHandsAcceptanceRepository.compensateStart(created.campaignId);
    expect(compensated.status).toBe("RECOVERING");
    expect(compensated.runs.every((run) => run.status === "RECOVERING")).toBe(true);
    expect(compensated.runs.find((run) => run.runId === science.runId)).toMatchObject({
      workerId: "partial-start-science",
      processId: process.pid,
      leaseGeneration: attached.leaseGeneration,
    });
    expect(application.openHandsAcceptanceRepository.compensateStart(created.campaignId).status).toBe("RECOVERING");

    const retrying = application.openHandsAcceptanceRepository.resumeStart(created.campaignId);
    expect(retrying.status).toBe("RUNNING");
    expect(retrying.runs.every((run) => run.status === "RECOVERING")).toBe(true);
    const resumed = retrying.runs.map((run, index) => application.openHandsAcceptanceRepository.attachWorker(
      run.runId,
      `startup-retry-${run.lane}`,
      process.pid + index + 1,
      true,
      run.leaseGeneration,
    ));
    expect(resumed.every((run) => run.status === "RUNNING" && run.recoveryCount === 1)).toBe(true);
    expect(application.openHandsAcceptanceRepository.startupComplete(created.campaignId)).toBe(false);
    const completedStartup = application.openHandsAcceptanceRepository.completeStart(created.campaignId);
    expect(completedStartup.status).toBe("RUNNING");
    expect(application.openHandsAcceptanceRepository.startupComplete(created.campaignId)).toBe(true);
    expect(application.openHandsAcceptanceRepository.completeStart(created.campaignId).campaignId).toBe(created.campaignId);
    expect(() => application.openHandsAcceptanceRepository.compensateStart(created.campaignId))
      .toThrow("completed startup and cannot be compensated");
    expect(() => application.openHandsAcceptanceRepository.resumeStart(created.campaignId))
      .toThrow("startup recovery precondition was rejected");
  });

  it("atomically rolls back RUNNING when the durable start checkpoint cannot be inserted", async () => {
    const application = await setup();
    const conversationId = seedPassedCanary(application);
    const created = application.openHandsAcceptanceService.create({
      conversationId,
      fixedProviderModel: "deepseek/deepseek-v4-pro",
      minimumWallClockSeconds: 7_200,
      minimumOverlapSeconds: 6_900,
      hardwareMode: "READ_ONLY",
      hardwareTarget: "tianyan176",
      maxNewHardwareJobs: 0,
      shotsPerJob: 0,
      gitAction: "NONE",
    });
    const observer = openDatabase(application.config.sqlitePath, path.resolve("infra/sqlite"));
    try {
      observer.database.exec(`
        CREATE TRIGGER reject_acceptance_start_checkpoint
        BEFORE INSERT ON openhands_acceptance_checkpoints
        WHEN NEW.stage = 'start_baseline'
        BEGIN
          SELECT RAISE(ABORT, 'injected start checkpoint failure');
        END;
      `);
      expect(() => application.openHandsAcceptanceService.start(created.campaignId))
        .toThrow("injected start checkpoint failure");
      expect(application.openHandsAcceptanceRepository.get(created.campaignId)).toMatchObject({
        status: "CREATED",
        baselineEventSequence: null,
      });
      expect(application.openHandsAcceptanceRepository.get(created.campaignId).runs.every(
        (run) => run.status === "CREATED",
      )).toBe(true);
      observer.database.exec("DROP TRIGGER reject_acceptance_start_checkpoint");
      expect(application.openHandsAcceptanceService.start(created.campaignId).status).toBe("RUNNING");
    } finally {
      observer.database.close();
    }
  });

  it("moves a force-killed long-query worker to recovery only with the old worker, PID, and lease", async () => {
    const application = await setup();
    const conversationId = seedPassedCanary(application);
    const created = application.openHandsAcceptanceService.create({
      conversationId,
      fixedProviderModel: "deepseek/deepseek-v4-pro",
      minimumWallClockSeconds: 7_200,
      minimumOverlapSeconds: 6_900,
      hardwareMode: "READ_ONLY",
      hardwareTarget: "tianyan176",
      maxNewHardwareJobs: 0,
      shotsPerJob: 0,
      gitAction: "NONE",
    });
    const started = application.openHandsAcceptanceService.start(created.campaignId);
    const science = started.runs.find((run) => run.lane === "quantum_science")!;
    const longQuery = spawn(process.execPath, [
      "-e",
      "process.on('SIGTERM', () => {}); process.stdout.write('ready\\n'); setInterval(() => {}, 1000)",
    ], { stdio: ["ignore", "pipe", "ignore"] });
    children.push(longQuery);
    if (!longQuery.pid) throw new Error("long-query fixture did not expose a PID");
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("long-query fixture readiness timed out")), 2_000);
      longQuery.stdout!.once("data", () => {
        clearTimeout(timer);
        resolve();
      });
    });
    const oldProcessId = longQuery.pid;
    const attached = application.openHandsAcceptanceRepository.attachWorker(
      science.runId,
      "science-long-query-old",
      oldProcessId,
      false,
      science.leaseGeneration,
    );

    longQuery.kill("SIGTERM");
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(() => process.kill(oldProcessId, 0)).not.toThrow();
    longQuery.kill("SIGKILL");
    await waitForExit(longQuery);
    expect(() => process.kill(oldProcessId, 0)).toThrow();

    expect(() => application.openHandsAcceptanceRepository.prepareRunRecoveryAfterProcessDeath(
      science.runId,
      "science-long-query-forged",
      oldProcessId,
      attached.leaseGeneration,
    )).toThrow("dead-process recovery CAS was rejected");
    const recovering = application.openHandsAcceptanceRepository.prepareRunRecoveryAfterProcessDeath(
      science.runId,
      "science-long-query-old",
      oldProcessId,
      attached.leaseGeneration,
    );
    expect(recovering).toMatchObject({ status: "RECOVERING", leaseGeneration: attached.leaseGeneration });
    expect(application.openHandsAcceptanceRepository.prepareRunRecoveryAfterProcessDeath(
      science.runId,
      "science-long-query-old",
      oldProcessId,
      attached.leaseGeneration,
    ).status).toBe("RECOVERING");

    const replacement = application.openHandsAcceptanceRepository.attachWorker(
      science.runId,
      "science-long-query-replacement",
      oldProcessId + 1,
      true,
      attached.leaseGeneration,
    );
    expect(replacement).toMatchObject({ status: "RUNNING", leaseGeneration: attached.leaseGeneration + 1 });
    expect(() => application.openHandsAcceptanceRepository.prepareRunRecoveryAfterProcessDeath(
      science.runId,
      "science-long-query-old",
      oldProcessId,
      attached.leaseGeneration,
    )).toThrow("dead-process recovery CAS was rejected");
    expect(application.openHandsAcceptanceRepository.getRun(science.runId).workerId)
      .toBe("science-long-query-replacement");
  });

  it("replays fault transitions idempotently and rejects replacement-PID retargeting", async () => {
    const application = await setup();
    const conversationId = seedPassedCanary(application);
    const created = application.openHandsAcceptanceService.create({
      conversationId,
      fixedProviderModel: "deepseek/deepseek-v4-pro",
      minimumWallClockSeconds: 7_200,
      minimumOverlapSeconds: 6_900,
      hardwareMode: "READ_ONLY",
      hardwareTarget: "tianyan176",
      maxNewHardwareJobs: 0,
      shotsPerJob: 0,
      gitAction: "NONE",
    });
    const started = application.openHandsAcceptanceService.start(created.campaignId);
    const science = started.runs.find((run) => run.lane === "quantum_science")!;
    const beforeCheckpoint = application.openHandsAcceptanceRepository.checkpoint({
      campaignId: created.campaignId,
      runId: science.runId,
      stage: "fault-before",
      idempotencyKey: `${created.campaignId}:fault-before`,
      payload: { stage: "before", formalTestSealed: true },
    });
    const beforeSnapshot = application.openHandsAcceptanceRepository.captureStateSnapshot(
      created.campaignId,
      beforeCheckpoint.payloadHash,
    );
    const input = {
      campaignId: created.campaignId,
      runId: science.runId,
      kind: "SCIENCE_WORKER_TERMINATION" as const,
      targetProcessId: process.pid + 20,
      targetProcessKey: `openhands-acceptance-science:${created.campaignId}:${science.runId}`,
      beforeCheckpointHash: beforeCheckpoint.payloadHash,
      beforeSnapshot,
    };
    const faultId = application.openHandsAcceptanceRepository.planFault(input);
    expect(application.openHandsAcceptanceRepository.planFault(input)).toBe(faultId);
    expect(() => application.openHandsAcceptanceRepository.planFault({
      ...input,
      targetProcessId: input.targetProcessId + 1,
    })).toThrow("fault idempotency collision");

    expect(application.openHandsAcceptanceRepository.markFaultInjected(faultId).state).toBe("INJECTED");
    expect(application.openHandsAcceptanceRepository.markFaultInjected(faultId).state).toBe("INJECTED");
    const afterCheckpoint = application.openHandsAcceptanceRepository.checkpoint({
      campaignId: created.campaignId,
      runId: science.runId,
      stage: "fault-after",
      idempotencyKey: `${created.campaignId}:fault-after`,
      payload: { stage: "after", formalTestSealed: true },
    });
    const verified = application.openHandsAcceptanceRepository.verifyAndRecoverFault(
      faultId,
      afterCheckpoint.payloadHash,
    );
    expect(verified).toMatchObject({
      sessionStable: true,
      idempotencyStable: true,
      hardwareStable: true,
      externalActionsReplayed: false,
    });
    expect(application.openHandsAcceptanceRepository.verifyAndRecoverFault(faultId, afterCheckpoint.payloadHash))
      .toEqual(verified);
    expect(() => application.openHandsAcceptanceRepository.verifyAndRecoverFault(faultId, "f".repeat(64)))
      .toThrow("fault recovery idempotency collision");
  });
});
