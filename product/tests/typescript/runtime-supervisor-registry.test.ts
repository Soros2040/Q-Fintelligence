import { spawn, type ChildProcess } from "node:child_process";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import {
  acquireLinuxFileLockGuard,
  clearRuntimeSupervisorRegistration,
  readLinuxBootId,
  readLinuxProcessStartTimeTicks,
  updateRuntimeSupervisorRegistration,
} from "../../scripts/runtime-process-registry.mjs";

const roots: string[] = [];
const children: ChildProcess[] = [];

async function waitForExit(child: ChildProcess): Promise<void> {
  if (child.exitCode !== null || child.signalCode !== null) return;
  await new Promise<void>((resolve) => child.once("exit", () => resolve()));
}

async function tryNonblockingFileLock(lockPath: string): Promise<number | null> {
  const child = spawn("/usr/bin/flock", ["--exclusive", "--nonblock", lockPath, "/usr/bin/true"], {
    stdio: "ignore",
  });
  return new Promise((resolve, reject) => {
    child.once("error", reject);
    child.once("exit", (code) => resolve(code));
  });
}

afterEach(async () => {
  for (const child of children.splice(0)) {
    if (child.exitCode === null && child.signalCode === null) child.kill("SIGTERM");
    await waitForExit(child);
  }
  await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
});

describe("development supervisor unified runtime registry", () => {
  it("retains the kernel cleanup guard after its helper exits and releases it with the parent descriptor", async () => {
    const projectRoot = await mkdtemp(path.join(os.tmpdir(), "qf-registry-guard-"));
    roots.push(projectRoot);
    const guardPath = path.join(projectRoot, "cleanup.guard");
    const release = await acquireLinuxFileLockGuard(guardPath, 1_000);
    expect(await tryNonblockingFileLock(guardPath)).toBe(1);
    await release();
    expect(await tryNonblockingFileLock(guardPath)).toBe(0);
  });

  it("serializes supervisor updates and preserves managed Campaign entries on stop", async () => {
    const projectRoot = await mkdtemp(path.join(os.tmpdir(), "qf-supervisor-registry-"));
    roots.push(projectRoot);
    const runtimeRoot = path.join(projectRoot, ".runtime");
    await mkdir(runtimeRoot, { recursive: true });
    const registryPath = path.join(runtimeRoot, "processes.json");
    const bootId = await readLinuxBootId();
    const managedEntry = {
      processKey: "openhands-acceptance-science:campaign-preserved:run-preserved",
      pid: 999_999,
      kind: "OPENHANDS_ACCEPTANCE_SCIENCE_WORKER",
      killMode: "PROCESS_GROUP",
      campaignId: "campaign-preserved",
      runId: "run-preserved",
      lane: "quantum_science",
      namespace: "qfintelligence",
      projectRoot,
      processBootId: bootId,
      processStartTimeTicks: "1",
      processCommandSha256: "a".repeat(64),
    };
    await writeFile(registryPath, `${JSON.stringify({
      schemaVersion: "qf.runtime.v1",
      projectRoot,
      namespace: "qfintelligence",
      bootId,
      childGroups: [],
      managedProcesses: [managedEntry],
    })}\n`, { mode: 0o600 });

    const child = spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)"], {
      cwd: projectRoot,
      env: { PATH: process.env.PATH ?? "/usr/bin:/bin", QF_PROCESS_NAMESPACE: "qfintelligence" },
      detached: true,
      stdio: "ignore",
    });
    children.push(child);
    expect(child.pid).toBeTypeOf("number");
    const supervisorStartTimeTicks = await readLinuxProcessStartTimeTicks(process.pid);
    const leaderStartTimeTicks = await readLinuxProcessStartTimeTicks(child.pid!);
    const input = {
      supervisorPid: process.pid,
      supervisorStartTimeTicks,
      childGroups: [{
        script: "dev:api",
        title: "qfint-control-plane",
        leaderPid: child.pid!,
        leaderStartTimeTicks,
      }],
      startedAt: new Date().toISOString(),
    };

    await writeFile(path.join(runtimeRoot, "processes.registry.lock"), `${JSON.stringify({
      schemaVersion: "qf.process-registry-lock.v2",
      pid: process.pid,
      bootId: "00000000-0000-0000-0000-000000000000",
      ownerStartTimeTicks: supervisorStartTimeTicks,
      nonce: "stale-concurrent-supervisor-lock",
      createdAt: new Date().toISOString(),
    })}\n`, { mode: 0o600 });

    await Promise.all([
      updateRuntimeSupervisorRegistration(projectRoot, input),
      updateRuntimeSupervisorRegistration(projectRoot, input),
    ]);
    const running = JSON.parse(await readFile(registryPath, "utf8"));
    expect(running).toMatchObject({
      schemaVersion: "qf.runtime.v2",
      bootId,
      supervisorPid: process.pid,
      supervisorStartTimeTicks,
      childGroups: [expect.objectContaining({ leaderPid: child.pid, leaderStartTimeTicks })],
      managedProcesses: [managedEntry],
    });

    await clearRuntimeSupervisorRegistration(projectRoot, {
      supervisorPid: process.pid,
      supervisorStartTimeTicks,
    });
    const stopped = JSON.parse(await readFile(registryPath, "utf8"));
    expect(stopped.supervisorPid).toBeUndefined();
    expect(stopped.supervisorStartTimeTicks).toBeUndefined();
    expect(stopped.childGroups).toEqual([]);
    expect(stopped.managedProcesses).toEqual([managedEntry]);
  });
});
