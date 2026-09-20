import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { existsSync, realpathSync } from "node:fs";
import { link, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import path from "node:path";

import { EXPECTED_WSL_ROOT, inspectQfPorts, PROCESS_NAMESPACE } from "./port-registry.mjs";
import {
  acquireLinuxFileLockGuard,
  clearRuntimeSupervisorRegistration,
  readLinuxBootId,
  readLinuxProcessStartTimeTicks,
  updateRuntimeSupervisorRegistration,
} from "./runtime-process-registry.mjs";

if (existsSync(".env")) process.loadEnvFile(".env");
process.env.QF_PROCESS_NAMESPACE = PROCESS_NAMESPACE;
process.env.QF_RUNTIME_PROCESS_REGISTRY_REQUIRED = "1";
process.title = "qfint-dev-supervisor";

const projectRoot = realpathSync(process.cwd());
if (projectRoot !== EXPECTED_WSL_ROOT) {
  throw new Error(`development services must run from ${EXPECTED_WSL_ROOT}; got ${projectRoot}`);
}

const runtimeDir = path.join(projectRoot, ".runtime");
const lockPath = path.join(runtimeDir, "dev.lock");
await mkdir(runtimeDir, { recursive: true });
const supervisorBootId = await readLinuxBootId();
const supervisorStartTimeTicks = await readLinuxProcessStartTimeTicks(process.pid);
const supervisorStartedAt = new Date().toISOString();
let devLockNonce = "";

async function processExists(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

async function acquireLock() {
  const nonce = randomUUID();
  const stagingPath = path.join(runtimeDir, `dev.lock.${process.pid}.${nonce}.tmp`);
  await writeFile(stagingPath, `${JSON.stringify({
    schemaVersion: "qf.dev-lock.v2",
    pid: process.pid,
    projectRoot,
    bootId: supervisorBootId,
    ownerStartTimeTicks: supervisorStartTimeTicks,
    nonce,
    startedAt: supervisorStartedAt,
  })}\n`, { mode: 0o600, flag: "wx" });
  try {
    await link(stagingPath, lockPath);
    devLockNonce = nonce;
    await rm(stagingPath, { force: true });
    return;
  } catch (error) {
    await rm(stagingPath, { force: true });
    if (error.code !== "EEXIST") throw error;
  }

  const releaseCleanupGuard = await acquireLinuxFileLockGuard(`${lockPath}.cleanup.guard`);
  try {
    let existing = null;
    try {
      existing = JSON.parse(await readFile(lockPath, "utf8"));
    } catch {
      // Complete lock metadata is linked atomically, so invalid content is stale.
    }
    const existingPid = Number(existing?.pid);
    let exactOwner = false;
    if (existing?.schemaVersion === "qf.dev-lock.v2"
      && existing?.projectRoot === projectRoot
      && existing?.bootId === supervisorBootId
      && /^\d+$/u.test(existing?.ownerStartTimeTicks ?? "")
      && existingPid > 1
      && (await processExists(existingPid))) {
      try {
        exactOwner = await readLinuxProcessStartTimeTicks(existingPid) === existing.ownerStartTimeTicks;
      } catch {
        exactOwner = false;
      }
    }
    if (exactOwner) {
      throw new Error(`q-fintelligence supervisor is already running as PID ${existingPid}`);
    }
    await rm(lockPath, { force: true });
  } finally {
    await releaseCleanupGuard();
  }
  return acquireLock();
}

async function releaseDevLock() {
  let existing = null;
  try {
    existing = JSON.parse(await readFile(lockPath, "utf8"));
  } catch {
    // A missing or replaced lock is not removed on behalf of this supervisor.
  }
  if (existing?.nonce === devLockNonce) await rm(lockPath, { force: true });
}

await acquireLock();

const portResults = await inspectQfPorts();
const busy = portResults.filter((result) => !result.available);
if (busy.length > 0) {
  await releaseDevLock();
  throw new Error(`q-fintelligence port guard blocked startup: ${JSON.stringify(busy)}`);
}

const childGroups = [];
let stopping = false;
const apiNodeOptions = `${process.env.NODE_OPTIONS ?? ""} --conditions=qf-development`.trim();

async function persistProcesses() {
  for (const child of childGroups) {
    const actualStartTimeTicks = await readLinuxProcessStartTimeTicks(child.leaderPid);
    if (child.leaderStartTimeTicks !== undefined && child.leaderStartTimeTicks !== actualStartTimeTicks) {
      throw new Error(`q-fintelligence ${child.title} PID was reused before registry persistence`);
    }
    child.leaderStartTimeTicks = actualStartTimeTicks;
  }
  await updateRuntimeSupervisorRegistration(projectRoot, {
    supervisorPid: process.pid,
    supervisorStartTimeTicks,
    childGroups,
    startedAt: supervisorStartedAt,
  });
}

function start(script, title) {
  const child = spawn("npm", ["run", script], {
    cwd: projectRoot,
    env: {
      ...process.env,
      QF_PROCESS_NAMESPACE: PROCESS_NAMESPACE,
      QF_PROCESS_TITLE: title,
      ...(script === "dev:api" ? { NODE_OPTIONS: apiNodeOptions } : {}),
    },
    stdio: "inherit",
    shell: false,
    detached: true,
  });
  childGroups.push({ script, title, leaderPid: child.pid });
  child.once("exit", (code) => {
    if (!stopping && code !== 0) {
      process.stderr.write(`${title} exited with code ${code}\n`);
      void stop("SIGTERM", 1);
    }
  });
  return child;
}

async function waitForApi(child) {
  const port = Number(process.env.QF_API_PORT ?? 27_872);
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`control plane exited with code ${child.exitCode}`);
    try {
      const response = await fetch(`http://127.0.0.1:${port}/api/health`, {
        signal: AbortSignal.timeout(1_000),
      });
      if (response.ok) return;
    } catch {
      // Startup is still in progress.
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`control plane readiness timed out on port ${port}`);
}

async function stop(signal, exitCode = 0) {
  if (stopping) return;
  stopping = true;
  for (const child of childGroups.toReversed()) {
    try {
      process.kill(-child.leaderPid, signal);
    } catch (error) {
      if (error.code !== "ESRCH") process.stderr.write(`${String(error)}\n`);
    }
  }
  try {
    await clearRuntimeSupervisorRegistration(projectRoot, {
      supervisorPid: process.pid,
      supervisorStartTimeTicks,
    });
  } catch (error) {
    process.stderr.write(`Failed to clear exact QF supervisor registration: ${String(error)}\n`);
  }
  await releaseDevLock();
  process.exit(exitCode);
}

process.once("SIGINT", () => void stop("SIGINT"));
process.once("SIGTERM", () => void stop("SIGTERM"));

try {
  const api = start("dev:api", "qfint-control-plane");
  await persistProcesses();
  await waitForApi(api);
  start("dev:web", "qfint-web");
  await persistProcesses();
} catch (error) {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
  await stop("SIGTERM", 1);
}
