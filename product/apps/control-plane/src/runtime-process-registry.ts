import { spawn } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { link, mkdir, open, readFile, readlink, rename, rm, writeFile } from "node:fs/promises";
import path from "node:path";

import type { OpenHandsSidecarProcessIdentity } from "./agent/openhands-runtime.js";
import type { RuntimeManagedProcessRegistry } from "./agent/runtime-manager.js";

const PROCESS_NAMESPACE = "qfintelligence";
const BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id";
const LOCK_TIMEOUT_MS = 5_000;
const SIDECAR_EXEC_TIMEOUT_MS = 5_000;

interface ManagedProcessEntry extends OpenHandsSidecarProcessIdentity {
  namespace: typeof PROCESS_NAMESPACE;
  projectRoot: string;
  processBootId: string;
  processStartTimeTicks: string;
  processCommandSha256: string;
  ownerSupervisorPid: number;
  ownerSupervisorStartTimeTicks: string;
  ownerApiLeaderPid: number;
  ownerApiLeaderStartTimeTicks: string;
  registeredAt: string;
}

interface RuntimeChildGroup {
  script: string;
  title: string;
  leaderPid: number;
  leaderStartTimeTicks?: string;
}

interface RuntimeProcessRegistryDocument {
  schemaVersion: string;
  projectRoot: string;
  namespace: typeof PROCESS_NAMESPACE;
  bootId?: string;
  supervisorPid?: number;
  supervisorStartTimeTicks?: string;
  childGroups: RuntimeChildGroup[];
  managedProcesses: ManagedProcessEntry[];
  [key: string]: unknown;
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function processExists(pid: number): Promise<boolean> {
  if (!Number.isSafeInteger(pid) || pid <= 1) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

async function readLinuxBootId(): Promise<string> {
  const bootId = (await readFile(BOOT_ID_PATH, "utf8")).trim();
  if (!/^[a-f0-9-]{36}$/u.test(bootId)) throw new Error("Linux boot identity is invalid");
  return bootId;
}

interface ProcIdentity {
  pid: number;
  executable: string;
  cwd: string;
  environment: string;
  commandLine: string[];
  commandSha256: string;
  parentPid: number;
  processGroupId: number;
  startTimeTicks: string;
  uid: number;
}

async function readProcIdentity(pid: number): Promise<ProcIdentity> {
  const [executable, cwd, environment, commandLineBytes, stat, status] = await Promise.all([
    readlink(`/proc/${pid}/exe`),
    readlink(`/proc/${pid}/cwd`),
    readFile(`/proc/${pid}/environ`, "utf8"),
    readFile(`/proc/${pid}/cmdline`),
    readFile(`/proc/${pid}/stat`, "utf8"),
    readFile(`/proc/${pid}/status`, "utf8"),
  ]);
  const commandEnd = stat.lastIndexOf(")");
  const fields = commandEnd < 0 ? [] : stat.slice(commandEnd + 2).trim().split(/\s+/u);
  const parentPid = Number(fields[1]);
  const processGroupId = Number(fields[2]);
  const startTimeTicks = fields[19] ?? "";
  const uid = Number(/^Uid:\s+(\d+)/mu.exec(status)?.[1]);
  if (!Number.isSafeInteger(parentPid) || parentPid <= 0
    || !Number.isSafeInteger(processGroupId) || processGroupId <= 1
    || !/^\d+$/u.test(startTimeTicks)
    || !Number.isSafeInteger(uid) || uid < 0) {
    throw new Error("managed QF process /proc identity is invalid");
  }
  return {
    pid,
    executable,
    cwd,
    environment,
    commandLine: commandLineBytes.toString("utf8").split("\0").filter(Boolean),
    commandSha256: createHash("sha256").update(commandLineBytes).digest("hex"),
    parentPid,
    processGroupId,
    startTimeTicks,
    uid,
  };
}

function hasProcessNamespace(identity: ProcIdentity): boolean {
  return identity.environment.split("\0").includes(`QF_PROCESS_NAMESPACE=${PROCESS_NAMESPACE}`);
}

function sameArguments(actual: readonly string[], expected: readonly string[]): boolean {
  return actual.length === expected.length && actual.every((value, index) => value === expected[index]);
}

function isExactSandboxedSidecarCommand(
  identity: OpenHandsSidecarProcessIdentity,
  actual: ProcIdentity,
  projectRoot: string,
): boolean {
  const sidecarRoot = path.join(projectRoot, "workers", "openhands_sidecar");
  const stateRoot = path.join(
    projectRoot,
    ".local",
    "openhands-sessions",
    identity.conversationId,
    identity.runtimeSessionId,
  );
  const workspaceRoot = path.join(
    projectRoot,
    ".local",
    "openhands-workspaces",
    identity.runtimeSessionId,
  );
  const socketRoot = path.join(projectRoot, ".local", "ohs");
  const expectedArguments = [
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
    "--setenv", "QF_PROCESS_NAMESPACE", PROCESS_NAMESPACE,
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
  return actual.executable === "/usr/bin/bwrap"
    && path.basename(actual.commandLine[0] ?? "") === "bwrap"
    && sameArguments(actual.commandLine.slice(1), expectedArguments);
}

interface SidecarOwnership {
  processBootId: string;
  processStartTimeTicks: string;
  processCommandSha256: string;
  ownerSupervisorPid: number;
  ownerSupervisorStartTimeTicks: string;
  ownerApiLeaderPid: number;
  ownerApiLeaderStartTimeTicks: string;
}

function assertStartsAfter(child: ProcIdentity, parent: ProcIdentity): void {
  if (BigInt(child.startTimeTicks) < BigInt(parent.startTimeTicks)) {
    throw new Error("managed QF process predates its registered owner");
  }
}

function isApiAncestorCwd(cwd: string, projectRoot: string): boolean {
  return cwd === projectRoot || cwd === path.join(projectRoot, "apps", "control-plane");
}

async function assertOwnedSidecar(
  identity: OpenHandsSidecarProcessIdentity,
  registry: RuntimeProcessRegistryDocument,
  projectRoot: string,
): Promise<SidecarOwnership> {
  const bootId = await readLinuxBootId();
  if (registry.bootId !== bootId) throw new Error("runtime process registry belongs to a stale Linux boot");
  const actual = await readProcIdentity(identity.pid);
  if (actual.uid !== 1000 || !hasProcessNamespace(actual)
    || !isExactSandboxedSidecarCommand(identity, actual, projectRoot)) {
    throw new Error("managed QF process is not the exact QF OpenHands sandbox");
  }

  const apiGroups = registry.childGroups.filter((group) => (
    group.script === "dev:api" && group.title === "qfint-control-plane"
  ));
  if (apiGroups.length !== 1) throw new Error("registered QF dev:api process group is not unique");
  const apiLeaderPid = apiGroups[0]!.leaderPid;
  if (!Number.isSafeInteger(apiLeaderPid) || apiLeaderPid <= 1
    || !/^\d+$/u.test(apiGroups[0]!.leaderStartTimeTicks ?? "")
    || registry.supervisorPid === undefined
    || !/^\d+$/u.test(registry.supervisorStartTimeTicks ?? "")) {
    throw new Error("registered QF dev ownership identity is invalid");
  }

  const [apiLeader, supervisor] = await Promise.all([
    readProcIdentity(apiLeaderPid),
    readProcIdentity(registry.supervisorPid),
  ]);
  if (
    apiLeader.uid !== 1000
    || apiLeader.processGroupId !== apiLeaderPid
    || apiLeader.parentPid !== supervisor.pid
    || apiLeader.startTimeTicks !== apiGroups[0]!.leaderStartTimeTicks
    || apiLeader.cwd !== projectRoot
    || !hasProcessNamespace(apiLeader)
    || !sameArguments(apiLeader.commandLine, ["npm run dev:api"])
    || supervisor.uid !== 1000
    || supervisor.startTimeTicks !== registry.supervisorStartTimeTicks
    || supervisor.cwd !== projectRoot
    || !hasProcessNamespace(supervisor)
    || !sameArguments(supervisor.commandLine, ["qfint-dev-supervisor"])
  ) {
    throw new Error("registered QF dev:api owner identity changed");
  }
  assertStartsAfter(apiLeader, supervisor);
  if (actual.processGroupId !== apiLeaderPid) {
    throw new Error("OpenHands sidecar escaped the registered QF dev:api process group");
  }

  let child = actual;
  const visited = new Set<number>([actual.pid]);
  for (let depth = 0; child.parentPid !== apiLeaderPid; depth += 1) {
    if (depth >= 32 || child.parentPid <= 1 || visited.has(child.parentPid)) {
      throw new Error("OpenHands sidecar does not descend from the registered QF dev:api owner");
    }
    visited.add(child.parentPid);
    const parent = await readProcIdentity(child.parentPid);
    if (parent.uid !== 1000 || parent.processGroupId !== apiLeaderPid
      || !hasProcessNamespace(parent) || !isApiAncestorCwd(parent.cwd, projectRoot)) {
      throw new Error("OpenHands sidecar parent chain left the registered QF dev:api boundary");
    }
    assertStartsAfter(child, parent);
    child = parent;
  }
  assertStartsAfter(child, apiLeader);

  const [actualAgain, apiLeaderAgain, supervisorAgain] = await Promise.all([
    readProcIdentity(identity.pid),
    readProcIdentity(apiLeaderPid),
    readProcIdentity(supervisor.pid),
  ]);
  if (
    actualAgain.startTimeTicks !== actual.startTimeTicks
    || actualAgain.commandSha256 !== actual.commandSha256
    || apiLeaderAgain.startTimeTicks !== apiLeader.startTimeTicks
    || apiLeaderAgain.commandSha256 !== apiLeader.commandSha256
    || supervisorAgain.startTimeTicks !== supervisor.startTimeTicks
    || supervisorAgain.commandSha256 !== supervisor.commandSha256
  ) {
    throw new Error("managed QF process identity changed during registration");
  }
  return {
    processBootId: bootId,
    processStartTimeTicks: actual.startTimeTicks,
    processCommandSha256: actual.commandSha256,
    ownerSupervisorPid: supervisor.pid,
    ownerSupervisorStartTimeTicks: supervisor.startTimeTicks,
    ownerApiLeaderPid: apiLeader.pid,
    ownerApiLeaderStartTimeTicks: apiLeader.startTimeTicks,
  };
}

async function waitForOwnedSidecar(
  identity: OpenHandsSidecarProcessIdentity,
  registry: RuntimeProcessRegistryDocument,
  projectRoot: string,
): Promise<SidecarOwnership> {
  const deadline = Date.now() + SIDECAR_EXEC_TIMEOUT_MS;
  let lastError: unknown = new Error("OpenHands sidecar did not exec the exact bwrap sandbox");
  while (Date.now() < deadline) {
    try {
      return await assertOwnedSidecar(identity, registry, projectRoot);
    } catch (error) {
      lastError = error;
      if (!(await processExists(identity.pid))) throw error;
      const transition = await readProcIdentity(identity.pid);
      if (!["bash", "env"].includes(path.basename(transition.executable))) throw error;
      await delay(10);
    }
  }
  throw lastError;
}

function validateRegistry(value: unknown, projectRoot: string): RuntimeProcessRegistryDocument {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("QF runtime process registry must be a JSON object");
  }
  const registry = value as Record<string, unknown>;
  if (registry.projectRoot !== projectRoot || registry.namespace !== PROCESS_NAMESPACE) {
    throw new Error("runtime process registry does not belong to this q-fintelligence checkout");
  }
  if (registry.bootId !== undefined
    && (typeof registry.bootId !== "string" || !/^[a-f0-9-]{36}$/u.test(registry.bootId))) {
    throw new Error("runtime process registry boot identity is invalid");
  }
  if (!Array.isArray(registry.childGroups)) throw new Error("runtime process registry childGroups is invalid");
  if (!registry.childGroups.every((group) => (
    typeof group === "object"
    && group !== null
    && !Array.isArray(group)
    && typeof (group as Record<string, unknown>).script === "string"
    && typeof (group as Record<string, unknown>).title === "string"
    && Number.isSafeInteger((group as Record<string, unknown>).leaderPid)
    && Number((group as Record<string, unknown>).leaderPid) > 1
    && ((group as Record<string, unknown>).leaderStartTimeTicks === undefined
      || (typeof (group as Record<string, unknown>).leaderStartTimeTicks === "string"
        && /^\d+$/u.test(String((group as Record<string, unknown>).leaderStartTimeTicks))))
  ))) {
    throw new Error("runtime process registry childGroups identity is invalid");
  }
  if (registry.supervisorPid !== undefined
    && (!Number.isSafeInteger(registry.supervisorPid) || Number(registry.supervisorPid) <= 1
      || (registry.supervisorStartTimeTicks !== undefined
        && (typeof registry.supervisorStartTimeTicks !== "string"
          || !/^\d+$/u.test(registry.supervisorStartTimeTicks))))) {
    throw new Error("runtime process registry supervisor identity is invalid");
  }
  if (registry.managedProcesses !== undefined && !Array.isArray(registry.managedProcesses)) {
    throw new Error("runtime process registry managedProcesses is invalid");
  }
  return {
    ...registry,
    schemaVersion: typeof registry.schemaVersion === "string" ? registry.schemaVersion : "qf.runtime.v1",
    projectRoot,
    namespace: PROCESS_NAMESPACE,
    ...(registry.bootId === undefined ? {} : { bootId: registry.bootId as string }),
    ...(registry.supervisorPid === undefined ? {} : { supervisorPid: Number(registry.supervisorPid) }),
    ...(registry.supervisorStartTimeTicks === undefined
      ? {}
      : { supervisorStartTimeTicks: registry.supervisorStartTimeTicks as string }),
    childGroups: registry.childGroups as RuntimeChildGroup[],
    managedProcesses: (registry.managedProcesses ?? []) as ManagedProcessEntry[],
  };
}

interface RegistryLockMetadata {
  schemaVersion: "qf.process-registry-lock.v2";
  pid: number;
  bootId: string;
  ownerStartTimeTicks: string;
  nonce: string;
  createdAt: string;
}

interface RegistryLock {
  lockPath: string;
  stagingPath: string;
  nonce: string;
}

function parseRegistryLock(value: unknown): RegistryLockMetadata | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const lock = value as Record<string, unknown>;
  if (lock.schemaVersion !== "qf.process-registry-lock.v2"
    || !Number.isSafeInteger(lock.pid) || Number(lock.pid) <= 1
    || typeof lock.bootId !== "string" || !/^[a-f0-9-]{36}$/u.test(lock.bootId)
    || typeof lock.ownerStartTimeTicks !== "string" || !/^\d+$/u.test(lock.ownerStartTimeTicks)
    || typeof lock.nonce !== "string" || !lock.nonce
    || typeof lock.createdAt !== "string") return null;
  return lock as unknown as RegistryLockMetadata;
}

async function lockOwnerIsCurrent(lock: RegistryLockMetadata | null, bootId: string): Promise<boolean> {
  if (lock === null || lock.bootId !== bootId || !(await processExists(lock.pid))) return false;
  try {
    return (await readProcIdentity(lock.pid)).startTimeTicks === lock.ownerStartTimeTicks;
  } catch {
    return false;
  }
}

async function acquireLinuxFileLockGuard(
  guardPath: string,
  timeoutMilliseconds: number,
): Promise<() => Promise<void>> {
  const handle = await open(guardPath, "a", 0o600);
  try {
    const child = spawn("/usr/bin/flock", [
      "--exclusive",
      "--wait",
      String(Math.max(1, timeoutMilliseconds) / 1_000),
      "3",
    ], {
      env: { PATH: "/usr/bin:/bin" },
      stdio: ["ignore", "ignore", "pipe", handle.fd],
    });
    let stderr = "";
    child.stderr?.setEncoding("utf8");
    child.stderr?.on("data", (chunk: string) => {
      stderr = `${stderr}${chunk}`.slice(-2_000);
    });
    await new Promise<void>((resolve, reject) => {
      child.once("error", reject);
      child.once("exit", (code, signal) => {
        if (code === 0) resolve();
        else reject(new Error(
          `Linux file-lock guard failed code=${String(code)} signal=${String(signal)}: ${stderr}`,
        ));
      });
    });
    let released = false;
    return async () => {
      if (released) return;
      released = true;
      await handle.close();
    };
  } catch (error) {
    await handle.close();
    throw error;
  }
}

export class RuntimeProcessRegistry implements RuntimeManagedProcessRegistry {
  private readonly runtimeDirectory: string;
  private readonly registryPath: string;
  private readonly lockPath: string;

  constructor(private readonly projectRoot: string) {
    this.runtimeDirectory = path.join(projectRoot, ".runtime");
    this.registryPath = path.join(this.runtimeDirectory, "processes.json");
    this.lockPath = path.join(this.runtimeDirectory, "processes.registry.lock");
  }

  private async acquireLock(): Promise<RegistryLock> {
    await mkdir(this.runtimeDirectory, { recursive: true, mode: 0o700 });
    const bootId = await readLinuxBootId();
    const ownerStartTimeTicks = (await readProcIdentity(process.pid)).startTimeTicks;
    const deadline = Date.now() + LOCK_TIMEOUT_MS;
    while (Date.now() < deadline) {
      const nonce = randomUUID();
      const stagingPath = path.join(
        this.runtimeDirectory,
        `processes.registry.lock.${process.pid}.${nonce}.tmp`,
      );
      await writeFile(stagingPath, `${JSON.stringify({
        schemaVersion: "qf.process-registry-lock.v2",
        pid: process.pid,
        bootId,
        ownerStartTimeTicks,
        nonce,
        createdAt: new Date().toISOString(),
      })}\n`, { mode: 0o600, flag: "wx" });
      try {
        await link(stagingPath, this.lockPath);
        return { lockPath: this.lockPath, stagingPath, nonce };
      } catch (error) {
        await rm(stagingPath, { force: true });
        if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
        const releaseCleanupGuard = await acquireLinuxFileLockGuard(
          path.join(this.runtimeDirectory, "processes.registry.cleanup.guard"),
          Math.max(1, deadline - Date.now()),
        );
        try {
          let existing: RegistryLockMetadata | null = null;
          try {
            existing = parseRegistryLock(JSON.parse(await readFile(this.lockPath, "utf8")) as unknown);
          } catch {
            // Complete lock metadata is linked atomically, so invalid content is stale.
          }
          if (!(await lockOwnerIsCurrent(existing, bootId))) {
            await rm(this.lockPath, { force: true });
            continue;
          }
        } finally {
          await releaseCleanupGuard();
        }
        await delay(20);
      }
    }
    throw new Error("timed out acquiring the QF runtime process-registry lock");
  }

  private async releaseLock(lock: RegistryLock): Promise<void> {
    await rm(lock.stagingPath, { force: true });
    let current: RegistryLockMetadata | null = null;
    try {
      current = parseRegistryLock(JSON.parse(await readFile(lock.lockPath, "utf8")) as unknown);
    } catch {
      // Never remove a missing or replaced lock on behalf of this owner.
    }
    if (current?.nonce === lock.nonce) await rm(lock.lockPath, { force: true });
  }

  private async mutate(
    operation: (
      registry: RuntimeProcessRegistryDocument,
    ) => RuntimeProcessRegistryDocument | Promise<RuntimeProcessRegistryDocument>,
    missingIsSafe = false,
  ): Promise<void> {
    const lock = await this.acquireLock();
    let temporaryPath: string | null = null;
    try {
      let registry: RuntimeProcessRegistryDocument;
      try {
        registry = validateRegistry(JSON.parse(await readFile(this.registryPath, "utf8")) as unknown, this.projectRoot);
      } catch (error) {
        if (missingIsSafe && (error as NodeJS.ErrnoException).code === "ENOENT") return;
        throw error;
      }
      const updated = validateRegistry(await operation(registry), this.projectRoot);
      temporaryPath = path.join(this.runtimeDirectory, `processes.${process.pid}.${randomUUID()}.tmp`);
      await writeFile(temporaryPath, `${JSON.stringify(updated, null, 2)}\n`, { mode: 0o600, flag: "wx" });
      await rename(temporaryPath, this.registryPath);
      temporaryPath = null;
    } finally {
      if (temporaryPath !== null) await rm(temporaryPath, { force: true });
      await this.releaseLock(lock);
    }
  }

  async register(identity: OpenHandsSidecarProcessIdentity): Promise<void> {
    await this.mutate(async (registry) => {
      const existing = registry.managedProcesses.filter((entry) => entry.processKey === identity.processKey);
      if (existing.length > 1) {
        throw new Error(`managed QF process key ${identity.processKey} is not uniquely registered`);
      }
      if (existing.length === 1 && existing[0]!.pid !== identity.pid) {
        const prior = existing[0]!;
        const bootId = await readLinuxBootId();
        if (prior.namespace !== PROCESS_NAMESPACE || prior.projectRoot !== this.projectRoot
          || !/^[a-f0-9-]{36}$/u.test(prior.processBootId)
          || !/^\d+$/u.test(prior.processStartTimeTicks)
          || !/^[a-f0-9]{64}$/u.test(prior.processCommandSha256)
          || (prior.processBootId === bootId && await processExists(prior.pid))) {
          throw new Error("refused to replace a live or invalid OpenHands sidecar registration");
        }
      }
      const ownership = await waitForOwnedSidecar(identity, registry, this.projectRoot);
      return {
        ...registry,
        managedProcesses: [
          ...registry.managedProcesses.filter((entry) => entry.processKey !== identity.processKey),
          {
            ...identity,
            ...ownership,
            namespace: PROCESS_NAMESPACE,
            projectRoot: this.projectRoot,
            registeredAt: new Date().toISOString(),
          },
        ],
      };
    });
  }

  async unregister(processKey: string, expectedPid: number): Promise<void> {
    await this.mutate((registry) => ({
      ...registry,
      managedProcesses: registry.managedProcesses.filter((entry) => (
        entry.processKey !== processKey || entry.pid !== expectedPid
      )),
    }), true);
  }

  async reconcileDeadRegistrations(): Promise<number> {
    let removed = 0;
    await this.mutate(async (registry) => {
      const bootId = await readLinuxBootId();
      const retained: ManagedProcessEntry[] = [];
      for (const entry of registry.managedProcesses) {
        let stale = entry.processBootId !== bootId || !(await processExists(entry.pid));
        if (!stale) {
          try {
            const actual = await readProcIdentity(entry.pid);
            stale = actual.startTimeTicks !== entry.processStartTimeTicks
              || actual.commandSha256 !== entry.processCommandSha256;
          } catch {
            // Preserve an entry if /proc is temporarily unavailable; never discard an unverified live process.
            stale = false;
          }
        }
        if (stale) removed += 1;
        else retained.push(entry);
      }
      return { ...registry, managedProcesses: retained };
    }, true);
    return removed;
  }
}
