import { spawn, spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { closeSync, existsSync, mkdirSync, openSync, realpathSync } from "node:fs";
import path from "node:path";

import {
  assertRegisteredProcessDead,
  inspectRegisteredManagedProcess,
  readRuntimeProcessRegistry,
  registerManagedProcess,
  replaceDeadManagedProcess,
  terminateRegisteredManagedProcess,
  unregisterManagedProcess,
} from "./runtime-process-registry.mjs";
import {
  laneRegistrationAnchor,
  laneResumeRegistrationAction,
  plannedFaultProcessAction,
  scienceFaultRunAction,
  startupProcessAction,
} from "./openhands-acceptance-state-machine.mjs";

const projectRoot = realpathSync(process.cwd());
const expectedRoot = process.cwd();
if (projectRoot !== expectedRoot) throw new Error(`OpenHands acceptance commands must run from ${expectedRoot}`);
if (existsSync(".env")) process.loadEnvFile(".env");
process.env.QF_PROCESS_NAMESPACE = "qfintelligence";
const frozen = {
  FIXED_PROVIDER_MODEL: "deepseek/deepseek-v4-pro",
  MIN_WALL_CLOCK_SECONDS: "7200",
  HARDWARE_MODE: "READ_ONLY",
  HARDWARE_TARGET: "tianyan176",
  MAX_NEW_HARDWARE_JOBS: "0",
  SHOTS_PER_JOB: "0",
  GIT_ACTION: "NONE",
};
for (const [name, value] of Object.entries(frozen)) {
  if (process.env[name] !== value) throw new Error(`${name} must equal the frozen OpenHands acceptance value`);
}

const tsx = path.join(projectRoot, "node_modules", ".bin", "tsx");
const cli = path.join(projectRoot, "apps", "control-plane", "src", "campaign", "openhands-acceptance-cli.ts");
const worker = path.join(projectRoot, "apps", "control-plane", "src", "campaign", "openhands-acceptance-lane-worker.ts");
const command = process.argv[2] ?? "status";

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function cliJson(args) {
  const result = spawnSync(tsx, [cli, ...args], { cwd: projectRoot, env: process.env, encoding: "utf8", shell: false });
  if (result.status !== 0) throw new Error(result.stderr || result.stdout || `OpenHands acceptance CLI failed with ${result.status}`);
  return JSON.parse(result.stdout);
}

function laneProcessKey(campaignId, run) {
  return run.lane === "quantum_science"
    ? `openhands-acceptance-science:${campaignId}:${run.runId}`
    : `openhands-acceptance-lane:${campaignId}:${run.runId}`;
}

function laneExpectedIdentity(campaignId, run) {
  return {
    campaignId,
    runId: run.runId,
    lane: run.lane,
    kind: run.lane === "quantum_science"
      ? "OPENHANDS_ACCEPTANCE_SCIENCE_WORKER"
      : "OPENHANDS_ACCEPTANCE_LANE",
  };
}

async function spawnLane(campaignId, run, recovery, previousRegistration = null) {
  const logRoot = path.join(projectRoot, ".local", "openhands-acceptance", campaignId, "logs");
  mkdirSync(logRoot, { recursive: true });
  const logPath = path.join(logRoot, `${run.lane}.log`);
  const log = openSync(logPath, "a", 0o600);
  const registrationToken = randomUUID();
  // The tsx CLI launches a second Node process, so its returned PID is only a
  // transient launcher. Run the Worker in the registered process itself: the
  // registry PID, registration-gate PID, process-group leader, and later fault
  // target must all describe the same OS process.
  const child = spawn(process.execPath, ["--import", "tsx", worker, campaignId, run.runId, run.lane], {
    argv0: "node",
    cwd: projectRoot,
    env: {
      ...process.env,
      QF_PROCESS_NAMESPACE: "qfintelligence",
      QF_OPENHANDS_ACCEPTANCE_CAMPAIGN_ID: campaignId,
      QF_OPENHANDS_ACCEPTANCE_RUN_ID: run.runId,
      QF_OPENHANDS_ACCEPTANCE_LANE: run.lane,
      QF_OPENHANDS_ACCEPTANCE_RECOVERY: recovery ? "1" : "0",
      QF_OPENHANDS_ACCEPTANCE_EXPECTED_LEASE_GENERATION: String(run.leaseGeneration),
      QF_OPENHANDS_ACCEPTANCE_REGISTRATION_TOKEN: registrationToken,
      QF_PROCESS_IDENTITY: laneProcessKey(campaignId, run),
    },
    detached: true,
    shell: false,
    stdio: ["pipe", log, log],
    windowsHide: true,
  });
  closeSync(log);
  if (!Number.isSafeInteger(child.pid) || child.pid <= 1) throw new Error(`failed to spawn acceptance ${run.lane} process`);
  const identity = {
    processKey: laneProcessKey(campaignId, run),
    pid: child.pid,
    killMode: "PROCESS_GROUP",
    ...laneExpectedIdentity(campaignId, run),
    expectedLeaseGeneration: run.leaseGeneration,
    recovery,
    logPath: path.relative(projectRoot, logPath),
    startedAt: new Date().toISOString(),
  };
  try {
    if (previousRegistration === null) {
      await registerManagedProcess(projectRoot, identity);
    } else {
      await replaceDeadManagedProcess(projectRoot, identity, {
        expectedCurrent: previousRegistration,
        expectedAnchorPid: run.processId,
      });
    }
    const registered = await inspectRegisteredManagedProcess(projectRoot, identity.processKey, laneExpectedIdentity(campaignId, run));
    return {
      child,
      identity: registered,
      expectedLeaseGeneration: run.leaseGeneration,
      release: async () => {
        const gate = `${JSON.stringify({
          schemaVersion: "qf.openhands-acceptance-registration-gate.v1",
          token: registrationToken,
          processKey: identity.processKey,
          processId: child.pid,
          campaignId,
          runId: run.runId,
          lane: run.lane,
          recovery,
          expectedLeaseGeneration: run.leaseGeneration,
        })}\n`;
        await new Promise((resolve, reject) => {
          child.stdin.end(gate, "utf8", (error) => error ? reject(error) : resolve());
        });
        child.unref();
      },
    };
  } catch (error) {
    child.stdin.destroy();
    try {
      process.kill(-child.pid, "SIGKILL");
    } catch {
      // A worker that failed before registration is already gone.
    }
    throw error;
  }
}

async function terminateLaneHandle(campaignId, handle) {
  handle.child.stdin.destroy();
  try {
    return await terminateRegisteredManagedProcess(
      projectRoot,
      handle.identity.processKey,
      laneExpectedIdentity(campaignId, handle.identity),
    );
  } catch (error) {
    await assertRegisteredProcessDead(handle.identity);
    return null;
  }
}

async function liveCampaignProcesses(campaignId) {
  const registry = await readRuntimeProcessRegistry(projectRoot);
  const entries = registry.managedProcesses.filter((entry) => entry.campaignId === campaignId);
  return Promise.all(entries.map(async (entry) => {
    try {
      return { ...(await inspectRegisteredManagedProcess(projectRoot, entry.processKey)), live: true };
    } catch (error) {
      return { ...entry, live: false, validationError: error instanceof Error ? error.message : String(error) };
    }
  }));
}

function acceptanceWorkerProcesses(processes) {
  return processes.filter((entry) => (
    entry.kind === "OPENHANDS_ACCEPTANCE_LANE"
    || entry.kind === "OPENHANDS_ACCEPTANCE_SCIENCE_WORKER"
  ));
}

async function quiescePartialStartup(campaignId) {
  const registry = await readRuntimeProcessRegistry(projectRoot);
  const targets = acceptanceWorkerProcesses(
    registry.managedProcesses.filter((entry) => entry.campaignId === campaignId),
  );
  const stopped = [];
  for (const target of targets) {
    try {
      const live = await inspectRegisteredManagedProcess(
        projectRoot,
        target.processKey,
        laneExpectedIdentity(campaignId, target),
      );
      stopped.push(await terminateRegisteredManagedProcess(
        projectRoot,
        live.processKey,
        laneExpectedIdentity(campaignId, target),
      ));
    } catch (error) {
      try {
        stopped.push(await assertRegisteredProcessDead(target));
      } catch {
        throw error;
      }
    }
  }
  return stopped;
}

async function findLiveSidecar(campaign, expectedProcessKey, excludedPid) {
  const registry = await readRuntimeProcessRegistry(projectRoot);
  const candidates = registry.managedProcesses.filter((entry) => (
    entry.kind === "OPENHANDS_SIDECAR"
    && entry.conversationId === campaign.conversationId
    && (expectedProcessKey === undefined || entry.processKey === expectedProcessKey)
    && (excludedPid === undefined || entry.pid !== excludedPid)
  ));
  const live = [];
  for (const candidate of candidates) {
    try {
      live.push(await inspectRegisteredManagedProcess(projectRoot, candidate.processKey, {
        kind: "OPENHANDS_SIDECAR",
        conversationId: campaign.conversationId,
        configHash: campaign.runtimeConfigHash,
      }));
    } catch {
      // Stale and forged candidates are never fault targets.
    }
  }
  if (live.length !== 1) throw new Error(`Campaign requires exactly one live registered OpenHands sidecar; found ${live.length}`);
  return live[0];
}

async function associateSidecar(campaign, run, existing) {
  await registerManagedProcess(projectRoot, {
    ...existing,
    campaignId: campaign.campaignId,
    runId: run.runId,
    lane: run.lane,
    acceptanceRole: "OPENHANDS_FAULT_TARGET",
  });
  return inspectRegisteredManagedProcess(projectRoot, existing.processKey, {
    kind: "OPENHANDS_SIDECAR",
    campaignId: campaign.campaignId,
    runId: run.runId,
    lane: "openhands_orchestration",
    conversationId: campaign.conversationId,
    configHash: campaign.runtimeConfigHash,
  });
}

async function waitForReplacementSidecar(campaign, fault, timeoutMilliseconds = 30_000) {
  const deadline = Date.now() + timeoutMilliseconds;
  while (Date.now() < deadline) {
    try {
      return await findLiveSidecar(campaign, fault.targetProcessKey, fault.targetProcessId);
    } catch {
      await delay(50);
    }
  }
  throw new Error("OpenHands exact recovery did not register a distinct live replacement sidecar");
}

async function waitForDistinctCheckpoint(runId, beforeCheckpointHash, timeoutMilliseconds = 30_000) {
  const deadline = Date.now() + timeoutMilliseconds;
  while (Date.now() < deadline) {
    const checkpoint = cliJson(["latest-checkpoint", runId]);
    if (checkpoint.payloadHash !== beforeCheckpointHash) return checkpoint;
    await delay(50);
  }
  throw new Error("fault recovery did not produce a distinct durable after-checkpoint");
}

async function waitForLaneLeases(campaignId, handles, timeoutMilliseconds = 10_000) {
  const deadline = Date.now() + timeoutMilliseconds;
  while (Date.now() < deadline) {
    const campaign = cliJson(["status", campaignId]).campaign;
    const ready = handles.every((handle) => {
      const run = campaign.runs.find((candidate) => candidate.runId === handle.identity.runId);
      return run?.status === "RUNNING"
        && run.workerId !== null
        && run.processId === handle.identity.pid
        && run.leaseGeneration === handle.expectedLeaseGeneration + 1;
    });
    if (ready) return campaign;
    await delay(25);
  }
  throw new Error("OpenHands acceptance workers did not acquire every registered lane lease");
}

async function recoverOpenHandsFault(campaign, run, fault) {
  await assertRegisteredProcessDead({
    pid: fault.targetProcessId,
    processKey: fault.targetProcessKey,
    killMode: "PID",
  });
  const port = Number(process.env.QF_API_PORT ?? 27_872);
  const response = await fetch(`http://127.0.0.1:${port}/api/conversations/${encodeURIComponent(campaign.conversationId)}/runtime/recover`, {
    method: "POST",
    signal: AbortSignal.timeout(30_000),
  });
  if (!response.ok) throw new Error(`OpenHands exact-recovery API failed with HTTP ${response.status}: ${(await response.text()).slice(0, 1_000)}`);
  const recovered = await waitForReplacementSidecar(campaign, fault);
  await associateSidecar(campaign, run, recovered);
  const checkpoint = await waitForDistinctCheckpoint(run.runId, fault.beforeCheckpointHash);
  const verification = cliJson(["verify-fault-recovery", fault.faultId, checkpoint.payloadHash]);
  return { recovered, checkpoint, verification };
}

if (command === "start") {
  const campaignId = process.argv[3];
  if (!campaignId) throw new Error("start requires a Campaign ID created by the real API");
  await readRuntimeProcessRegistry(projectRoot);
  let beforeStart = cliJson(["status", campaignId]).campaign;
  let current = await liveCampaignProcesses(campaignId);
  const complete = cliJson(["startup-complete", campaignId]).complete;
  let action = startupProcessAction(beforeStart, acceptanceWorkerProcesses(current), complete);
  if (action === "REUSE_COMPLETE" || action === "MARK_COMPLETE") {
    const orchestrationRun = beforeStart.runs.find((run) => run.lane === "openhands_orchestration");
    if (!orchestrationRun) throw new Error("Campaign has no OpenHands orchestration Run");
    // A hot-restarted control plane can recreate the sidecar registration
    // without its acceptance association. Re-assert that association even
    // when all lane Workers are reusable; never fault-target an unassociated
    // sidecar by inference.
    const sidecar = await findLiveSidecar(beforeStart);
    await associateSidecar(beforeStart, orchestrationRun, sidecar);
    if (action === "MARK_COMPLETE") cliJson(["complete-start", campaignId]);
    process.stdout.write(`${JSON.stringify({
      accepted: true,
      reused: true,
      recoveredStartupMarker: action === "MARK_COMPLETE",
      campaignId,
      registryPath: ".runtime/processes.json",
      sidecar,
      lanes: acceptanceWorkerProcesses(current),
    }, null, 2)}\n`);
  } else {
    if (action === "REFUSE_COMPLETE" || action === "REFUSE_START") {
      throw new Error(`Campaign cannot start or recover startup from ${beforeStart.status}`);
    }
    if (action === "COMPENSATE_PARTIAL") {
      await quiescePartialStartup(campaignId);
      beforeStart = cliJson(["compensate-start", campaignId]);
      current = await liveCampaignProcesses(campaignId);
      action = startupProcessAction(beforeStart, acceptanceWorkerProcesses(current), false);
    }
    if (action !== "START_CREATED" && action !== "RESUME_COMPENSATED") {
      throw new Error(`Campaign startup did not converge from ${action}`);
    }
    const liveWorkers = acceptanceWorkerProcesses(current).filter((entry) => entry.live);
    if (liveWorkers.length > 0) {
      await quiescePartialStartup(campaignId);
      current = await liveCampaignProcesses(campaignId);
    }
    const orchestrationRun = beforeStart.runs.find((run) => run.lane === "openhands_orchestration");
    if (!orchestrationRun) throw new Error("Campaign has no OpenHands orchestration Run");
    const sidecar = await findLiveSidecar(beforeStart);
    const startupRecovery = action === "RESUME_COMPENSATED";
    const campaign = cliJson([startupRecovery ? "resume-start" : "start", campaignId]);
    const lanes = [];
    try {
      await associateSidecar(campaign, orchestrationRun, sidecar);
      for (const run of campaign.runs) lanes.push(await spawnLane(campaignId, run, startupRecovery));
      for (const lane of lanes) await lane.release();
      await waitForLaneLeases(campaignId, lanes);
      cliJson(["complete-start", campaignId]);
    } catch (error) {
      for (const lane of lanes) {
        await terminateLaneHandle(campaignId, lane);
      }
      cliJson(["compensate-start", campaignId]);
      throw error;
    }
    process.stdout.write(`${JSON.stringify({
      accepted: true,
      campaignId,
      startupRecovery,
      registryPath: ".runtime/processes.json",
      sidecar,
      lanes: lanes.map((lane) => lane.identity),
    }, null, 2)}\n`);
  }
} else if (command === "status") {
  const campaignId = process.argv[3];
  process.stdout.write(`${JSON.stringify({
    registryPath: ".runtime/processes.json",
    process: campaignId ? await liveCampaignProcesses(campaignId) : await readRuntimeProcessRegistry(projectRoot, false),
    detail: campaignId ? cliJson(["status", campaignId]) : null,
  }, null, 2)}\n`);
} else if (command === "resume-lane") {
  const [campaignId, laneName] = process.argv.slice(3);
  if (!campaignId || !laneName) throw new Error("resume-lane requires Campaign ID and lane");
  const campaign = cliJson(["status", campaignId]).campaign;
  const run = campaign.runs.find((item) => item.lane === laneName);
  if (!run) throw new Error(`OpenHands acceptance lane ${laneName} does not exist`);
  if (laneName === "openhands_orchestration") {
    const fault = cliJson(["fault", campaignId, run.runId, "OPENHANDS_PROCESS_TERMINATION"]).fault;
    if (!fault) throw new Error("OpenHands orchestration fault has not been planned");
    if (fault.state === "RECOVERED") {
      process.stdout.write(`${JSON.stringify({ accepted: true, reused: true, campaignId, lane: laneName, fault }, null, 2)}\n`);
      process.exitCode = 0;
    } else if (fault.state !== "INJECTED") {
      throw new Error(`OpenHands orchestration fault cannot resume from ${fault.state}`);
    } else {
      const recovered = await recoverOpenHandsFault(campaign, run, fault);
      process.stdout.write(`${JSON.stringify({ accepted: true, campaignId, lane: laneName, ...recovered }, null, 2)}\n`);
    }
  } else {
    const scienceFault = laneName === "quantum_science"
      ? cliJson(["fault", campaignId, run.runId, "SCIENCE_WORKER_TERMINATION"]).fault
      : null;
    if (scienceFault?.state === "PLANNED") {
      throw new Error("science fault termination must be durably marked before lane resume");
    }
    const registry = await readRuntimeProcessRegistry(projectRoot);
    const processKey = laneProcessKey(campaignId, run);
    const previous = registry.managedProcesses.find((entry) => entry.processKey === processKey);
    if (!previous) throw new Error(`OpenHands acceptance lane ${laneName} is not registered`);
    let live = null;
    try {
      live = await inspectRegisteredManagedProcess(projectRoot, processKey, laneExpectedIdentity(campaignId, run));
    } catch (error) {
      try {
        await assertRegisteredProcessDead(previous);
      } catch {
        throw error;
      }
    }
    const registrationAction = laneResumeRegistrationAction(run, previous, live !== null);
    if (registrationAction === "REUSE_ATTACHED") {
      let faultRecovery = null;
      if (scienceFault?.state === "INJECTED") {
        const checkpoint = await waitForDistinctCheckpoint(run.runId, scienceFault.beforeCheckpointHash);
        faultRecovery = cliJson(["verify-fault-recovery", scienceFault.faultId, checkpoint.payloadHash]);
      }
      process.stdout.write(`${JSON.stringify({
        accepted: true,
        reused: true,
        campaignId,
        ...live,
        faultRecovery,
      }, null, 2)}\n`);
    } else {
      laneRegistrationAnchor(campaignId, run, previous);
      const resumed = await spawnLane(campaignId, run, true, previous);
      try {
        await resumed.release();
        await waitForLaneLeases(campaignId, [resumed]);
      } catch (error) {
        await terminateLaneHandle(campaignId, resumed);
        throw error;
      }
      process.stdout.write(`${JSON.stringify({ accepted: true, campaignId, ...resumed.identity }, null, 2)}\n`);
    }
  }
} else if (command === "inject-fault") {
  const [campaignId, laneName] = process.argv.slice(3);
  if (!campaignId || !laneName) throw new Error("inject-fault requires Campaign ID and lane");
  if (laneName !== "openhands_orchestration" && laneName !== "quantum_science") {
    throw new Error("fault injection is limited to the OpenHands sidecar and science Worker");
  }
  const campaign = cliJson(["status", campaignId]).campaign;
  const run = campaign.runs.find((item) => item.lane === laneName);
  if (!run) throw new Error(`OpenHands acceptance lane ${laneName} does not exist`);
  const kind = laneName === "openhands_orchestration" ? "OPENHANDS_PROCESS_TERMINATION" : "SCIENCE_WORKER_TERMINATION";
  let fault = cliJson(["fault", campaignId, run.runId, kind]).fault;
  if (!fault) {
    const target = laneName === "openhands_orchestration"
      ? await findLiveSidecar(campaign)
      : await inspectRegisteredManagedProcess(projectRoot, laneProcessKey(campaignId, run), laneExpectedIdentity(campaignId, run));
    const checkpoint = cliJson(["latest-checkpoint", run.runId]);
    const { faultId } = cliJson([
      "plan-fault",
      campaignId,
      run.runId,
      kind,
      String(target.pid),
      target.processKey,
      checkpoint.payloadHash,
    ]);
    fault = cliJson(["fault", campaignId, run.runId, kind]).fault;
    if (!fault || fault.faultId !== faultId) throw new Error("planned fault could not be read back exactly");
  }
  let deathProof = null;
  if (fault.state === "PLANNED") {
    const registry = await readRuntimeProcessRegistry(projectRoot);
    const registered = registry.managedProcesses.find((entry) => entry.processKey === fault.targetProcessKey);
    const action = plannedFaultProcessAction(fault, registered);
    if (action === "TERMINATE_PLANNED_TARGET") {
      let exact = null;
      try {
        exact = await inspectRegisteredManagedProcess(projectRoot, fault.targetProcessKey, {
          kind: laneName === "openhands_orchestration" ? "OPENHANDS_SIDECAR" : "OPENHANDS_ACCEPTANCE_SCIENCE_WORKER",
          campaignId,
          runId: run.runId,
          conversationId: laneName === "openhands_orchestration" ? campaign.conversationId : undefined,
        });
      } catch (error) {
        try {
          deathProof = await assertRegisteredProcessDead({
            pid: fault.targetProcessId,
            processKey: fault.targetProcessKey,
            killMode: laneName === "quantum_science" ? "PROCESS_GROUP" : "PID",
          });
        } catch {
          throw error;
        }
      }
      if (exact !== null) {
        if (exact.pid !== fault.targetProcessId) throw new Error("planned fault target PID changed before termination");
        deathProof = await terminateRegisteredManagedProcess(projectRoot, fault.targetProcessKey, {
          kind: exact.kind,
          campaignId,
          runId: run.runId,
          conversationId: laneName === "openhands_orchestration" ? campaign.conversationId : undefined,
        });
      }
    } else {
      await assertRegisteredProcessDead({
        pid: fault.targetProcessId,
        processKey: fault.targetProcessKey,
        killMode: laneName === "quantum_science" ? "PROCESS_GROUP" : "PID",
      });
    }
  }
  if (fault.state === "PLANNED" || fault.state === "INJECTED") {
    if (laneName === "quantum_science") {
      const currentRun = cliJson(["status", campaignId]).campaign.runs.find((item) => item.runId === run.runId);
      if (!currentRun) throw new Error("science fault Run disappeared during recovery preparation");
      const runAction = scienceFaultRunAction(fault, currentRun);
      await assertRegisteredProcessDead({
        pid: fault.targetProcessId,
        processKey: fault.targetProcessKey,
        killMode: "PROCESS_GROUP",
      });
      if (runAction === "PREPARE_OLD_RUN_RECOVERY") {
        cliJson([
          "prepare-run-recovery-after-death",
          currentRun.runId,
          currentRun.workerId,
          String(fault.targetProcessId),
          String(currentRun.leaseGeneration),
        ]);
      } else if (runAction === "MARK_WITH_REPLACEMENT") {
        const replacement = await inspectRegisteredManagedProcess(
          projectRoot,
          fault.targetProcessKey,
          laneExpectedIdentity(campaignId, currentRun),
        );
        if (replacement.pid !== currentRun.processId) {
          throw new Error("science replacement registry and Run PIDs differ");
        }
      }
    }
    cliJson(["mark-fault-injected", fault.faultId]);
    fault = cliJson(["fault", campaignId, run.runId, kind]).fault;
    if (!fault) throw new Error("durably marked fault disappeared during read-back");
  }
  if (fault.state !== "INJECTED" && fault.state !== "RECOVERED") {
    throw new Error(`fault injection could not converge from ${fault.state}`);
  }
  process.stdout.write(`${JSON.stringify({
    injected: true,
    reused: deathProof === null,
    campaignId,
    lane: laneName,
    pid: fault.targetProcessId,
    processKey: fault.targetProcessKey,
    faultId: fault.faultId,
    kind,
    state: fault.state,
    deathProof,
  }, null, 2)}\n`);
} else if (command === "finalize") {
  const campaignId = process.argv[3];
  if (!campaignId) throw new Error("finalize requires a Campaign ID");
  process.stdout.write(`${JSON.stringify(cliJson(["finalize", campaignId]), null, 2)}\n`);
} else if (command === "stop") {
  const campaignId = process.argv[3];
  if (!campaignId) throw new Error("stop requires the exact Campaign ID");
  const registry = await readRuntimeProcessRegistry(projectRoot, false);
  const targets = (registry?.managedProcesses ?? []).filter((entry) => (
    entry.campaignId === campaignId
    && ["OPENHANDS_ACCEPTANCE_LANE", "OPENHANDS_ACCEPTANCE_SCIENCE_WORKER"].includes(entry.kind)
  ));
  const stopped = [];
  for (const target of targets) {
    try {
      const deathProof = await terminateRegisteredManagedProcess(projectRoot, target.processKey, {
        campaignId,
        runId: target.runId,
        kind: target.kind,
      });
      stopped.push({ processKey: target.processKey, pid: target.pid, deathProof });
    } catch (error) {
      await assertRegisteredProcessDead(target);
    }
    await unregisterManagedProcess(projectRoot, target.processKey, target.pid);
  }
  process.stdout.write(`${JSON.stringify({ campaignId, stopped, registryPath: ".runtime/processes.json" }, null, 2)}\n`);
} else {
  throw new Error(`unknown OpenHands acceptance command ${command}`);
}
