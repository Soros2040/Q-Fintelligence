import { spawn, type ChildProcess } from "node:child_process";
import { mkdir, mkdtemp, readFile, rm, symlink, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import {
  assertRegisteredProcessDead,
  inspectRegisteredManagedProcess,
  readLinuxBootId,
  readLinuxProcessStartTimeTicks,
  registerManagedProcess,
  replaceDeadManagedProcess,
  terminateRegisteredManagedProcess,
} from "../../scripts/runtime-process-registry.mjs";

const roots: string[] = [];
const children: ChildProcess[] = [];

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function waitForExit(child: ChildProcess): Promise<void> {
  if (child.exitCode !== null || child.signalCode !== null) return;
  await new Promise<void>((resolve) => child.once("exit", () => resolve()));
}

async function setupRoot(): Promise<string> {
  const projectRoot = await mkdtemp(path.join(os.tmpdir(), "qf-acceptance-process-"));
  roots.push(projectRoot);
  await mkdir(path.join(projectRoot, ".runtime"), { recursive: true });
  const workerPath = path.join(
    projectRoot,
    "apps",
    "control-plane",
    "src",
    "campaign",
    "openhands-acceptance-lane-worker.ts",
  );
  const tsxPath = path.join(projectRoot, "node_modules", ".bin", "tsx");
  await mkdir(path.dirname(workerPath), { recursive: true });
  await mkdir(path.dirname(tsxPath), { recursive: true });
  await writeFile(workerPath, "setInterval(() => {}, 1_000);\n", { mode: 0o600 });
  await symlink(path.resolve("node_modules/.bin/tsx"), tsxPath);
  await symlink(path.resolve("node_modules/tsx"), path.join(projectRoot, "node_modules", "tsx"), "dir");
  await writeFile(path.join(projectRoot, ".runtime", "processes.json"), `${JSON.stringify({
    schemaVersion: "qf.runtime.v1",
    projectRoot,
    namespace: "qfintelligence",
    bootId: await readLinuxBootId(),
    childGroups: [],
    managedProcesses: [],
  })}\n`, { mode: 0o600 });
  return projectRoot;
}

function startScienceWorker(projectRoot: string, options: { cwd?: string; namespace?: string; processIdentity?: string } = {}): ChildProcess {
  const workerPath = path.join(
    projectRoot,
    "apps",
    "control-plane",
    "src",
    "campaign",
    "openhands-acceptance-lane-worker.ts",
  );
  const child = spawn(process.execPath, [
    "--import",
    "tsx",
    workerPath,
    "campaign-test",
    "run-science",
    "quantum_science",
  ], {
    argv0: "node",
    cwd: options.cwd ?? projectRoot,
    env: {
      PATH: process.env.PATH ?? "/usr/bin:/bin",
      ...(options.namespace === undefined ? { QF_PROCESS_NAMESPACE: "qfintelligence" } : options.namespace
        ? { QF_PROCESS_NAMESPACE: options.namespace }
        : {}),
      QF_PROCESS_IDENTITY: options.processIdentity ?? "openhands-acceptance-science:campaign-test:run-science",
    },
    detached: true,
    stdio: "ignore",
  });
  children.push(child);
  return child;
}

function startBaitWorker(projectRoot: string): ChildProcess {
  const workerPath = path.join(
    projectRoot,
    "apps",
    "control-plane",
    "src",
    "campaign",
    "openhands-acceptance-lane-worker.ts",
  );
  const child = spawn(process.execPath, [
    "-e",
    "setInterval(() => {}, 1000)",
    path.join(projectRoot, "node_modules", ".bin", "tsx"),
    workerPath,
    "campaign-test",
    "run-science",
    "quantum_science",
  ], {
    cwd: projectRoot,
    env: {
      PATH: process.env.PATH ?? "/usr/bin:/bin",
      QF_PROCESS_NAMESPACE: "qfintelligence",
      QF_PROCESS_IDENTITY: "openhands-acceptance-science:campaign-test:run-science",
    },
    detached: true,
    stdio: "ignore",
  });
  children.push(child);
  return child;
}

function identity(pid: number) {
  return {
    processKey: "openhands-acceptance-science:campaign-test:run-science",
    pid,
    killMode: "PROCESS_GROUP",
    kind: "OPENHANDS_ACCEPTANCE_SCIENCE_WORKER",
    campaignId: "campaign-test",
    runId: "run-science",
    lane: "quantum_science",
  };
}

afterEach(async () => {
  for (const child of children.splice(0)) {
    if (child.exitCode === null && child.signalCode === null && child.pid) {
      try {
        process.kill(-child.pid, "SIGKILL");
      } catch {
        // Already terminated by the test.
      }
    }
    await waitForExit(child);
  }
  await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
});

describe("OpenHands acceptance exact process fault boundary", () => {
  it("registers the exact science Worker in the central registry and terminates only its process group", async () => {
    const projectRoot = await setupRoot();
    const target = startScienceWorker(projectRoot);
    const sibling = startScienceWorker(projectRoot, {
      processIdentity: "openhands-acceptance-science:campaign-other:run-other",
    });
    await delay(100);
    await writeFile(path.join(projectRoot, ".runtime", "processes.registry.lock"), `${JSON.stringify({
      schemaVersion: "qf.process-registry-lock.v2",
      pid: process.pid,
      bootId: "00000000-0000-0000-0000-000000000000",
      ownerStartTimeTicks: await readLinuxProcessStartTimeTicks(process.pid),
      nonce: "stale-prior-boot",
      createdAt: new Date().toISOString(),
    })}\n`, { mode: 0o600 });
    await registerManagedProcess(projectRoot, identity(target.pid!));

    const registered = await inspectRegisteredManagedProcess(
      projectRoot,
      identity(target.pid!).processKey,
      { kind: "OPENHANDS_ACCEPTANCE_SCIENCE_WORKER", campaignId: "campaign-test", runId: "run-science" },
    );
    expect(registered).toMatchObject({ pid: target.pid, namespace: "qfintelligence", projectRoot });
    await expect(assertRegisteredProcessDead(registered)).rejects.toThrow("did not terminate");

    const proof = await terminateRegisteredManagedProcess(projectRoot, registered.processKey, {
      kind: "OPENHANDS_ACCEPTANCE_SCIENCE_WORKER",
      campaignId: "campaign-test",
      runId: "run-science",
    }, { graceMilliseconds: 100 });
    expect(proof).toMatchObject({ pid: target.pid, pidAlive: false, processGroupAlive: false });
    expect(() => process.kill(sibling.pid!, 0)).not.toThrow();
  });

  it("rejects a Node process whose argv only contains exact-looking lane fragments", async () => {
    const projectRoot = await setupRoot();
    const bait = startBaitWorker(projectRoot);
    await delay(30);

    await expect(registerManagedProcess(projectRoot, identity(bait.pid!)))
      .rejects.toThrow("registered acceptance science Worker identity is invalid");
    expect(() => process.kill(bait.pid!, 0)).not.toThrow();
    const registry = JSON.parse(await readFile(path.join(projectRoot, ".runtime", "processes.json"), "utf8"));
    expect(registry.managedProcesses).toEqual([]);
  });

  it("rejects wrong cwd and missing namespace before registry mutation", async () => {
    const projectRoot = await setupRoot();
    const otherRoot = await mkdtemp(path.join(os.tmpdir(), "qf-wrong-cwd-"));
    roots.push(otherRoot);
    const wrongCwd = startScienceWorker(projectRoot, { cwd: otherRoot });
    const missingNamespace = startScienceWorker(projectRoot, { namespace: "" });
    await delay(100);

    await expect(registerManagedProcess(projectRoot, identity(wrongCwd.pid!))).rejects.toThrow(/identity is invalid|ENOENT/u);
    await expect(registerManagedProcess(projectRoot, identity(missingNamespace.pid!))).rejects.toThrow(/identity is invalid|ENOENT/u);
    const registry = JSON.parse(await readFile(path.join(projectRoot, ".runtime", "processes.json"), "utf8"));
    expect(registry.managedProcesses).toEqual([]);
  });

  it("CAS-replaces only a dead registration and preserves the database PID anchor across a crashed retry", async () => {
    const projectRoot = await setupRoot();
    const original = startScienceWorker(projectRoot);
    const replacementA = startScienceWorker(projectRoot);
    const replacementB = startScienceWorker(projectRoot);
    await delay(100);

    const originalIdentity = {
      ...identity(original.pid!),
      expectedLeaseGeneration: 0,
      recovery: false,
    };
    const replacementIdentity = (pid: number) => ({
      ...identity(pid),
      expectedLeaseGeneration: 1,
      recovery: true,
    });

    await registerManagedProcess(projectRoot, originalIdentity);
    const originalRegistration = await inspectRegisteredManagedProcess(
      projectRoot,
      identity(original.pid!).processKey,
    );
    await expect(registerManagedProcess(projectRoot, replacementIdentity(replacementA.pid!)))
      .rejects.toThrow("did not terminate");
    await terminateRegisteredManagedProcess(projectRoot, originalRegistration.processKey, {}, { graceMilliseconds: 100 });

    const contenders = await Promise.allSettled([
      replaceDeadManagedProcess(projectRoot, replacementIdentity(replacementA.pid!), {
        expectedCurrent: originalRegistration,
        expectedAnchorPid: original.pid!,
      }),
      replaceDeadManagedProcess(projectRoot, replacementIdentity(replacementB.pid!), {
        expectedCurrent: originalRegistration,
        expectedAnchorPid: original.pid!,
      }),
    ]);
    expect(contenders.filter((result) => result.status === "fulfilled")).toHaveLength(1);
    expect(contenders.filter((result) => result.status === "rejected")).toHaveLength(1);

    const firstReplacement = await inspectRegisteredManagedProcess(projectRoot, originalRegistration.processKey);
    expect([replacementA.pid, replacementB.pid]).toContain(firstReplacement.pid);
    expect(firstReplacement).toMatchObject({
      replacementAnchor: {
        processKey: originalRegistration.processKey,
        pid: original.pid,
        processBootId: originalRegistration.processBootId,
        processStartTimeTicks: originalRegistration.processStartTimeTicks,
        processCommandSha256: originalRegistration.processCommandSha256,
      },
      replacementAttempt: 1,
    });

    await terminateRegisteredManagedProcess(projectRoot, firstReplacement.processKey, {}, { graceMilliseconds: 100 });
    const retry = firstReplacement.pid === replacementA.pid ? replacementB : replacementA;
    await replaceDeadManagedProcess(projectRoot, replacementIdentity(retry.pid!), {
      expectedCurrent: firstReplacement,
      expectedAnchorPid: original.pid!,
    });
    await expect(inspectRegisteredManagedProcess(projectRoot, originalRegistration.processKey)).resolves.toMatchObject({
      pid: retry.pid,
      replacementAnchor: { pid: original.pid },
      replacedRegistration: { pid: firstReplacement.pid },
      replacementAttempt: 2,
    });
  });

  it("rejects forged namespace, wrong PID/start identity, and wrong process key", async () => {
    const projectRoot = await setupRoot();
    const target = startScienceWorker(projectRoot);
    const other = startScienceWorker(projectRoot, {
      processIdentity: "openhands-acceptance-science:campaign-other:run-other",
    });
    await delay(30);
    const targetIdentity = identity(target.pid!);
    await registerManagedProcess(projectRoot, targetIdentity);
    const registryPath = path.join(projectRoot, ".runtime", "processes.json");
    const baseline = JSON.parse(await readFile(registryPath, "utf8"));

    await writeFile(registryPath, `${JSON.stringify({
      ...baseline,
      managedProcesses: [{ ...baseline.managedProcesses[0], namespace: "qf-test-namespace" }],
    })}\n`);
    await expect(inspectRegisteredManagedProcess(projectRoot, targetIdentity.processKey)).rejects.toThrow("identity is invalid");

    await writeFile(registryPath, `${JSON.stringify({
      ...baseline,
      managedProcesses: [{ ...baseline.managedProcesses[0], pid: other.pid }],
    })}\n`);
    await expect(inspectRegisteredManagedProcess(projectRoot, targetIdentity.processKey)).rejects.toThrow(
      /PID was reused|identity token is missing|identity is invalid/u,
    );

    await writeFile(registryPath, `${JSON.stringify(baseline)}\n`);
    await expect(inspectRegisteredManagedProcess(projectRoot, "openhands-acceptance-science:campaign-test:wrong-run"))
      .rejects.toThrow("is not uniquely registered");

    await writeFile(registryPath, `${JSON.stringify({
      ...baseline,
      managedProcesses: [{ ...baseline.managedProcesses[0], processCommandSha256: "0".repeat(64) }],
    })}\n`);
    await expect(terminateRegisteredManagedProcess(projectRoot, targetIdentity.processKey))
      .rejects.toThrow("command identity changed");
    expect(() => process.kill(target.pid!, 0)).not.toThrow();
  });
});
