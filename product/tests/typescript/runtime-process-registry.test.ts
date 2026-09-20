import { spawn, type ChildProcess } from "node:child_process";
import { mkdir, mkdtemp, readFile, realpath, rm, symlink, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { RuntimeProcessRegistry } from "../../apps/control-plane/src/runtime-process-registry.js";
import { openHandsSandboxResourceNames } from "../../scripts/openhands-docker-cleanup.mjs";
import {
  inspectRegisteredManagedProcess,
  readLinuxBootId,
  readLinuxProcessStartTimeTicks,
} from "../../scripts/runtime-process-registry.mjs";

const temporaryRoots: string[] = [];
const children: ChildProcess[] = [];
const processGroups: number[] = [];

const RUNTIME_SESSION_ID = "11111111-1111-4111-8111-111111111111";

function sidecarIdentity(pid: number) {
  return {
    processKey: `openhands-sidecar:conversation-test:${RUNTIME_SESSION_ID}`,
    pid,
    kind: "OPENHANDS_SIDECAR" as const,
    killMode: "PID" as const,
    conversationId: "conversation-test",
    runtimeSessionId: RUNTIME_SESSION_ID,
    runtimeRevision: "qf-openhands-adapter.v2/1.39.0+qf.noobservability.1/r1",
    configHash: "a".repeat(64),
  };
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function waitForExit(child: ChildProcess): Promise<void> {
  if (child.exitCode !== null || child.signalCode !== null) return;
  await new Promise<void>((resolve) => child.once("exit", () => resolve()));
}

async function readJsonLine(child: ChildProcess): Promise<{ apiLeaderPid: number; sidecarPid: number }> {
  return new Promise((resolve, reject) => {
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => reject(new Error(`managed bwrap fixture timed out: ${stderr}`)), 5_000);
    child.stdout?.setEncoding("utf8");
    child.stderr?.setEncoding("utf8");
    child.stdout?.on("data", (chunk: string) => {
      stdout += chunk;
      const newline = stdout.indexOf("\n");
      if (newline < 0) return;
      clearTimeout(timer);
      try {
        resolve(JSON.parse(stdout.slice(0, newline)) as { apiLeaderPid: number; sidecarPid: number });
      } catch (error) {
        reject(error);
      }
    });
    child.stderr?.on("data", (chunk: string) => { stderr += chunk; });
    child.once("exit", (code, signal) => {
      clearTimeout(timer);
      reject(new Error(`managed bwrap fixture exited code=${String(code)} signal=${String(signal)}: ${stderr}`));
    });
  });
}

async function createManagedBwrapFixture(projectRoot: string): Promise<{
  supervisorPid: number;
  apiLeaderPid: number;
  sidecarPid: number;
}> {
  const controlPlaneRoot = path.join(projectRoot, "apps", "control-plane");
  const sidecarRoot = path.join(projectRoot, "workers", "openhands_sidecar");
  const stateRoot = path.join(
    projectRoot,
    ".local",
    "openhands-sessions",
    "conversation-test",
    RUNTIME_SESSION_ID,
  );
  await mkdir(controlPlaneRoot, { recursive: true });
  await mkdir(path.join(sidecarRoot, ".venv", "bin"), { recursive: true });
  await mkdir(stateRoot, { recursive: true });
  const productionPython = await realpath(path.resolve("workers/openhands_sidecar/.venv/bin/python"));
  await symlink(productionPython, path.join(sidecarRoot, ".venv", "bin", "python"));
  await writeFile(path.join(sidecarRoot, "main.py"), "import signal\nsignal.pause()\n", { mode: 0o600 });
  const workspaceRoot = path.join(projectRoot, ".local", "openhands-workspaces", RUNTIME_SESSION_ID);
  const socketRoot = path.join(projectRoot, ".local", "ohs");
  await mkdir(workspaceRoot, { recursive: true });
  await mkdir(socketRoot, { recursive: true });

  const bwrapArguments = [
    "--die-with-parent",
    "--new-session",
    "--unshare-pid",
    "--unshare-ipc",
    "--unshare-uts",
    "--unshare-cgroup",
    "--hostname", "qf-openhands-sidecar",
    "--cap-drop", "ALL",
    "--ro-bind", "/", "/",
    "--proc", "/proc",
    "--dev", "/dev",
    "--tmpfs", "/mnt",
    "--tmpfs", "/root",
    "--tmpfs", "/tmp",
    "--bind", "/run/docker.sock", "/run/docker.sock",
    "--ro-bind", sidecarRoot, "/opt",
    "--bind", stateRoot, stateRoot,
    "--bind", workspaceRoot, workspaceRoot,
    "--bind", socketRoot, socketRoot,
    "--clearenv",
    "--setenv", "LANG", "C.UTF-8",
    "--setenv", "PATH", "/opt/.venv/bin:/usr/bin:/bin",
    "--setenv", "USER", "sandbox",
    "--setenv", "LOGNAME", "sandbox",
    "--setenv", "HOME", "/home/sandbox",
    "--setenv", "DOCKER_CONFIG", "/tmp/qf-docker-config",
    "--setenv", "QF_PROCESS_NAMESPACE", "qfintelligence",
    "--setenv", "QF_OPENHANDS_STATE_ROOT", stateRoot,
    "--setenv", "QF_OPENHANDS_WORKSPACE_ROOT", workspaceRoot,
    "--setenv", "QF_OPENHANDS_PROJECT_SOURCE", projectRoot,
    "--setenv", "QF_OPENHANDS_AGENT_SERVER_IMAGE", "qfintelligence/openhands-agent-server:1.39.0-qf.1",
    "--setenv", "LITELLM_LOCAL_MODEL_COST_MAP", "true",
    "--setenv", "OPENHANDS_SUPPRESS_BANNER", "1",
    "--setenv", "PYTHONHASHSEED", "0",
    "--chdir", stateRoot,
    "/opt/.venv/bin/python", "-I", "/opt/main.py",
  ];
  const serverSource = `
    const { spawn } = require("node:child_process");
    const child = spawn("/usr/bin/bwrap", ${JSON.stringify(bwrapArguments)}, {
      cwd: ${JSON.stringify(projectRoot)},
      env: { PATH: "/usr/bin:/bin", QF_PROCESS_NAMESPACE: "qfintelligence" },
      stdio: "ignore",
    });
    child.once("spawn", () => process.stdout.write(JSON.stringify({
      apiLeaderPid: Number(process.env.API_LEADER_PID),
      sidecarPid: child.pid,
    }) + "\\n"));
    child.once("error", (error) => { process.stderr.write(String(error)); process.exit(2); });
    const stop = () => { child.kill("SIGTERM"); process.exit(0); };
    process.once("SIGTERM", stop);
    process.once("SIGINT", stop);
    setInterval(() => {}, 1000);
  `;
  const apiSource = `
    const { spawn } = require("node:child_process");
    process.title = "npm run dev:api";
    const server = spawn(process.execPath, ["--input-type=commonjs", "-e", ${JSON.stringify(serverSource)}], {
      cwd: ${JSON.stringify(controlPlaneRoot)},
      env: { PATH: process.env.PATH, QF_PROCESS_NAMESPACE: "qfintelligence", API_LEADER_PID: String(process.pid) },
      stdio: ["ignore", "pipe", "pipe"],
    });
    server.stdout.pipe(process.stdout);
    server.stderr.pipe(process.stderr);
    const stop = () => { server.kill("SIGTERM"); process.exit(0); };
    process.once("SIGTERM", stop);
    process.once("SIGINT", stop);
    setInterval(() => {}, 1000);
  `;
  const supervisorSource = `
    const { spawn } = require("node:child_process");
    process.title = "qfint-dev-supervisor";
    const api = spawn(process.execPath, ["--input-type=commonjs", "-e", ${JSON.stringify(apiSource)}], {
      cwd: ${JSON.stringify(projectRoot)},
      env: { PATH: process.env.PATH, QF_PROCESS_NAMESPACE: "qfintelligence" },
      detached: true,
      stdio: ["ignore", "pipe", "pipe"],
    });
    api.stdout.pipe(process.stdout);
    api.stderr.pipe(process.stderr);
    const stop = () => { try { process.kill(-api.pid, "SIGTERM"); } catch {} process.exit(0); };
    process.once("SIGTERM", stop);
    process.once("SIGINT", stop);
    setInterval(() => {}, 1000);
  `;
  const supervisor = spawn(process.execPath, ["--input-type=commonjs", "-e", supervisorSource], {
    cwd: projectRoot,
    env: {
      PATH: process.env.PATH ?? "/usr/bin:/bin",
      QF_PROCESS_NAMESPACE: "qfintelligence",
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  children.push(supervisor);
  const result = await readJsonLine(supervisor);
  processGroups.push(result.apiLeaderPid);
  await delay(50);
  return { supervisorPid: supervisor.pid!, ...result };
}

afterEach(async () => {
  for (const processGroupId of processGroups.splice(0)) {
    try {
      process.kill(-processGroupId, "SIGTERM");
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ESRCH") throw error;
    }
  }
  for (const child of children.splice(0)) {
    if (child.exitCode === null && child.signalCode === null) child.kill("SIGTERM");
    await waitForExit(child);
  }
  await Promise.all(temporaryRoots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
});

describe("central QF runtime process registry", () => {
  it("derives only exact per-session Docker resource names and rejects path traversal", () => {
    const resources = openHandsSandboxResourceNames(
      process.cwd(),
      "conversation-test",
      RUNTIME_SESSION_ID,
    );
    expect(resources.slug).toMatch(/^[a-f0-9]{16}$/u);
    expect(resources.containers).toEqual([
      `qf-oh-agent-${resources.slug}`,
      `qf-oh-gateway-${resources.slug}`,
      `qf-oh-proxy-${resources.slug}`,
      `qf-oh-tools-${resources.slug}`,
    ]);
    expect(resources.networks).toEqual([
      `qf-oh-net-${resources.slug}`,
      `qf-oh-ingress-${resources.slug}`,
      `qf-oh-egress-${resources.slug}`,
    ]);
    expect(() => openHandsSandboxResourceNames(
      process.cwd(),
      "../outside",
      RUNTIME_SESSION_ID,
    )).toThrow(/outside the exact QF session root/);
  });

  it("rejects a same-namespace bait process even when its argv contains sidecar-looking fragments", async () => {
    const projectRoot = await mkdtemp(path.join(os.tmpdir(), "qf-runtime-registry-test-"));
    temporaryRoots.push(projectRoot);
    const runtimeRoot = path.join(projectRoot, ".runtime");
    await mkdir(runtimeRoot, { recursive: true });
    const registryPath = path.join(runtimeRoot, "processes.json");
    const bootId = await readLinuxBootId();
    await writeFile(registryPath, `${JSON.stringify({
      schemaVersion: "qf.process-registry.v1",
      projectRoot,
      namespace: "qfintelligence",
      bootId,
      supervisorPid: process.pid,
      childGroups: [],
      managedProcesses: [],
    })}\n`, { mode: 0o600 });

    const child = spawn(process.execPath, [
      "-e",
      "setInterval(() => {}, 1000)",
      "--",
      "bwrap",
      "--hostname",
      "qf-openhands-sidecar",
      "--setenv",
      "QF_PROCESS_NAMESPACE",
      "qfintelligence",
      "/opt/.venv/bin/python",
      "-I",
      "/opt/main.py",
    ], {
      cwd: projectRoot,
      env: {
        PATH: process.env.PATH ?? "/usr/bin:/bin",
        QF_PROCESS_NAMESPACE: "qfintelligence",
      },
      stdio: "ignore",
    });
    children.push(child);
    expect(child.pid).toBeTypeOf("number");
    await delay(30);

    const registry = new RuntimeProcessRegistry(projectRoot);
    await expect(registry.register(sidecarIdentity(child.pid!))).rejects.toThrow(
      "managed QF process is not the exact QF OpenHands sandbox",
    );
    const registered = JSON.parse(await readFile(registryPath, "utf8")) as {
      managedProcesses: Array<Record<string, unknown>>;
    };
    expect(registered.managedProcesses).toEqual([]);
  });

  it("registers the exact production Agent Server controller shape only under the registered dev:api parent chain", async () => {
    const fixtureParent = path.join(process.cwd(), ".local");
    await mkdir(fixtureParent, { recursive: true });
    const projectRoot = await mkdtemp(path.join(fixtureParent, "qf-runtime-registry-agent-server-test-"));
    temporaryRoots.push(projectRoot);
    const runtimeRoot = path.join(projectRoot, ".runtime");
    await mkdir(runtimeRoot, { recursive: true });
    const fixture = await createManagedBwrapFixture(projectRoot);
    const registryPath = path.join(runtimeRoot, "processes.json");
    const bootId = await readLinuxBootId();
    const supervisorStartTimeTicks = await readLinuxProcessStartTimeTicks(fixture.supervisorPid);
    const apiLeaderStartTimeTicks = await readLinuxProcessStartTimeTicks(fixture.apiLeaderPid);
    await writeFile(registryPath, `${JSON.stringify({
      schemaVersion: "qf.process-registry.v1",
      projectRoot,
      namespace: "qfintelligence",
      bootId,
      supervisorPid: fixture.supervisorPid,
      supervisorStartTimeTicks,
      childGroups: [{
        script: "dev:api",
        title: "qfint-control-plane",
        leaderPid: fixture.apiLeaderPid,
        leaderStartTimeTicks: apiLeaderStartTimeTicks,
      }],
      managedProcesses: [],
    })}\n`, { mode: 0o600 });

    const identity = sidecarIdentity(fixture.sidecarPid);
    const registry = new RuntimeProcessRegistry(projectRoot);
    await writeFile(path.join(runtimeRoot, "processes.registry.lock"), `${JSON.stringify({
      schemaVersion: "qf.process-registry-lock.v2",
      pid: process.pid,
      bootId: "00000000-0000-0000-0000-000000000000",
      ownerStartTimeTicks: await readLinuxProcessStartTimeTicks(process.pid),
      nonce: "stale-prior-boot",
      createdAt: new Date().toISOString(),
    })}\n`, { mode: 0o600 });
    await registry.register(identity);
    await expect(inspectRegisteredManagedProcess(projectRoot, identity.processKey, {
      kind: "OPENHANDS_SIDECAR",
      conversationId: identity.conversationId,
    })).resolves.toMatchObject({
      pid: identity.pid,
      processBootId: bootId,
      processCommandSha256: expect.stringMatching(/^[a-f0-9]{64}$/u),
    });
    const registered = JSON.parse(await readFile(registryPath, "utf8")) as {
      managedProcesses: Array<Record<string, unknown>>;
    };
    expect(registered.managedProcesses).toEqual([
      expect.objectContaining({
        ...identity,
        namespace: "qfintelligence",
        projectRoot,
        processBootId: bootId,
        ownerSupervisorPid: fixture.supervisorPid,
        ownerApiLeaderPid: fixture.apiLeaderPid,
        processStartTimeTicks: expect.stringMatching(/^\d+$/u),
        processCommandSha256: expect.stringMatching(/^[a-f0-9]{64}$/u),
        ownerSupervisorStartTimeTicks: expect.stringMatching(/^\d+$/u),
        ownerApiLeaderStartTimeTicks: expect.stringMatching(/^\d+$/u),
      }),
    ]);

    await registry.unregister(identity.processKey, identity.pid);
    const unregistered = JSON.parse(await readFile(registryPath, "utf8")) as {
      managedProcesses: Array<Record<string, unknown>>;
    };
    expect(unregistered.managedProcesses).toEqual([]);
  });

  it("reconciles an exactly identified dead sidecar registration at service startup", async () => {
    const fixtureParent = path.join(process.cwd(), ".local");
    await mkdir(fixtureParent, { recursive: true });
    const projectRoot = await mkdtemp(path.join(fixtureParent, "qf-runtime-registry-reconcile-test-"));
    temporaryRoots.push(projectRoot);
    const runtimeRoot = path.join(projectRoot, ".runtime");
    await mkdir(runtimeRoot, { recursive: true });
    const fixture = await createManagedBwrapFixture(projectRoot);
    const bootId = await readLinuxBootId();
    const registryPath = path.join(runtimeRoot, "processes.json");
    await writeFile(registryPath, `${JSON.stringify({
      schemaVersion: "qf.process-registry.v1",
      projectRoot,
      namespace: "qfintelligence",
      bootId,
      supervisorPid: fixture.supervisorPid,
      supervisorStartTimeTicks: await readLinuxProcessStartTimeTicks(fixture.supervisorPid),
      childGroups: [{
        script: "dev:api",
        title: "qfint-control-plane",
        leaderPid: fixture.apiLeaderPid,
        leaderStartTimeTicks: await readLinuxProcessStartTimeTicks(fixture.apiLeaderPid),
      }],
      managedProcesses: [],
    })}\n`, { mode: 0o600 });
    const registry = new RuntimeProcessRegistry(projectRoot);
    const identity = sidecarIdentity(fixture.sidecarPid);
    await registry.register(identity);
    process.kill(fixture.sidecarPid, "SIGTERM");
    const deadline = Date.now() + 5_000;
    while (Date.now() < deadline) {
      try {
        process.kill(fixture.sidecarPid, 0);
        await delay(25);
      } catch {
        break;
      }
    }
    expect(() => process.kill(fixture.sidecarPid, 0)).toThrow();
    await expect(registry.reconcileDeadRegistrations()).resolves.toBe(1);
    const reconciled = JSON.parse(await readFile(registryPath, "utf8")) as {
      managedProcesses: Array<Record<string, unknown>>;
    };
    expect(reconciled.managedProcesses).toEqual([]);
  });
});
