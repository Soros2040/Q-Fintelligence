import { realpathSync } from "node:fs";
import { rm } from "node:fs/promises";
import path from "node:path";

import { cleanupDeadOpenHandsSandbox } from "./openhands-docker-cleanup.mjs";
import { inspectOwnedProcess } from "./process-safety.mjs";
import {
  assertRegisteredProcessDead,
  clearRuntimeSupervisorRegistration,
  readRuntimeProcessRegistry,
  unregisterManagedProcess,
} from "./runtime-process-registry.mjs";

const projectRoot = realpathSync(process.cwd());
const runtimeDir = path.join(projectRoot, ".runtime");
const processPath = path.join(runtimeDir, "processes.json");
const lockPath = path.join(runtimeDir, "dev.lock");

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function waitForExactProcessExit(pid, expected) {
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    const inspection = await inspectOwnedProcess(pid, projectRoot, expected);
    if (!inspection.owned) return;
    await delay(100);
  }
  throw new Error(`validated q-fintelligence process ${pid} did not stop within 30 seconds`);
}

async function finalizeStoppedRegistry() {
  let current = await readRuntimeProcessRegistry(projectRoot, false);
  if (current === null) return { registrations: 0, containers: 0, networks: 0 };
  if (
    Number.isSafeInteger(current.supervisorPid)
    && /^\d+$/u.test(current.supervisorStartTimeTicks ?? "")
  ) {
    await waitForExactProcessExit(Number(current.supervisorPid), {
      bootId: current.bootId,
      startTimeTicks: current.supervisorStartTimeTicks,
    });
    await clearRuntimeSupervisorRegistration(projectRoot, {
      supervisorPid: current.supervisorPid,
      supervisorStartTimeTicks: current.supervisorStartTimeTicks,
    });
    current = await readRuntimeProcessRegistry(projectRoot);
  }
  let containers = 0;
  let networks = 0;
  for (const managed of current.managedProcesses) {
    await assertRegisteredProcessDead(managed);
    const cleaned = await cleanupDeadOpenHandsSandbox(projectRoot, managed);
    containers += cleaned.containers;
    networks += cleaned.networks;
    await unregisterManagedProcess(projectRoot, managed.processKey, managed.pid);
  }
  const cleared = await readRuntimeProcessRegistry(projectRoot);
  if (
    cleared.managedProcesses.length > 0
    || cleared.childGroups.length > 0
    || cleared.supervisorPid !== undefined
  ) {
    throw new Error("refused to remove a runtime registry that still contains QF processes");
  }
  await rm(processPath, { force: true });
  await rm(lockPath, { force: true });
  return { registrations: current.managedProcesses.length, containers, networks };
}

const registry = await readRuntimeProcessRegistry(projectRoot, false);
if (registry === null) {
  process.stdout.write("q-fintelligence has no registered development supervisor.\n");
  process.exit(0);
}

const supervisor = await inspectOwnedProcess(Number(registry.supervisorPid), projectRoot, {
  bootId: registry.bootId,
  startTimeTicks: registry.supervisorStartTimeTicks,
});
if (supervisor.owned) {
  process.kill(Number(registry.supervisorPid), "SIGTERM");
  process.stdout.write(`Sent SIGTERM to validated q-fintelligence supervisor ${registry.supervisorPid}.\n`);
  await waitForExactProcessExit(Number(registry.supervisorPid), {
    bootId: registry.bootId,
    startTimeTicks: registry.supervisorStartTimeTicks,
  });
} else {
  let stopped = 0;
  for (const child of registry.childGroups ?? []) {
    const inspection = await inspectOwnedProcess(Number(child.leaderPid), projectRoot, {
      bootId: registry.bootId,
      startTimeTicks: child.leaderStartTimeTicks,
    });
    if (!inspection.owned) continue;
    process.kill(-Number(child.leaderPid), "SIGTERM");
    stopped += 1;
  }
  if (stopped === 0) {
    process.stdout.write("No live q-fintelligence process passed ownership validation; checking exact stale registrations.\n");
  } else {
    process.stdout.write(`Stopped ${stopped} validated q-fintelligence process groups.\n`);
    for (const child of registry.childGroups ?? []) {
      await waitForExactProcessExit(Number(child.leaderPid), {
        bootId: registry.bootId,
        startTimeTicks: child.leaderStartTimeTicks,
      });
    }
  }
}
const cleaned = await finalizeStoppedRegistry();
process.stdout.write(
  `Cleared ${cleaned.registrations} exact managed registrations, `
  + `${cleaned.containers} QF OpenHands containers, and ${cleaned.networks} empty QF networks.\n`,
);
