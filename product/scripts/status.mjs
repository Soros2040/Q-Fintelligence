import { realpathSync } from "node:fs";

import { inspectOwnedProcess } from "./process-safety.mjs";
import { readRuntimeProcessRegistry } from "./runtime-process-registry.mjs";

const projectRoot = realpathSync(process.cwd());
const registry = await readRuntimeProcessRegistry(projectRoot, false);
if (registry === null) {
  process.stdout.write(`${JSON.stringify({ project: "q-fintelligence", status: "stopped" }, null, 2)}\n`);
  process.exit(0);
}

const supervisor = await inspectOwnedProcess(Number(registry.supervisorPid), projectRoot, {
  bootId: registry.bootId,
  startTimeTicks: registry.supervisorStartTimeTicks,
});
const children = [];
for (const child of registry.childGroups ?? []) {
  children.push({ ...child, ...(await inspectOwnedProcess(Number(child.leaderPid), projectRoot, {
    bootId: registry.bootId,
    startTimeTicks: child.leaderStartTimeTicks,
  })) });
}

process.stdout.write(`${JSON.stringify({
  project: "q-fintelligence",
  status: supervisor.owned ? "running" : "stale-or-partial",
  supervisor: { pid: registry.supervisorPid, ...supervisor },
  children,
  managedProcessCount: registry.managedProcesses.length,
}, null, 2)}\n`);
