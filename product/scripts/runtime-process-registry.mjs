import { spawn } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { link, mkdir, open, readFile, readlink, rename, rm, writeFile } from "node:fs/promises";
import path from "node:path";

const NAMESPACE = "qfintelligence";
const BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id";
const LOCK_TIMEOUT_MS = 5_000;
const TERMINATION_GRACE_MS = 5_000;

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function processExists(pid) {
  if (!Number.isSafeInteger(pid) || pid <= 1) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

async function processGroupExists(processGroupId) {
  if (!Number.isSafeInteger(processGroupId) || processGroupId <= 1) return false;
  try {
    process.kill(-processGroupId, 0);
    return true;
  } catch {
    return false;
  }
}

function parseProcStat(value) {
  const commandEnd = value.lastIndexOf(")");
  if (commandEnd < 0) throw new Error("managed QF process stat is invalid");
  const fields = value.slice(commandEnd + 2).trim().split(/\s+/u);
  const parentPid = Number(fields[1]);
  const processGroupId = Number(fields[2]);
  const startTimeTicks = fields[19];
  if (!Number.isSafeInteger(parentPid) || parentPid <= 0
    || !Number.isSafeInteger(processGroupId) || processGroupId <= 1 || !/^\d+$/u.test(startTimeTicks ?? "")) {
    throw new Error("managed QF process stat identity is invalid");
  }
  return { parentPid, processGroupId, startTimeTicks };
}

export async function readLinuxBootId() {
  const bootId = (await readFile(BOOT_ID_PATH, "utf8")).trim();
  if (!/^[a-f0-9-]{36}$/u.test(bootId)) throw new Error("Linux boot identity is invalid");
  return bootId;
}

export async function readLinuxProcessStartTimeTicks(pid) {
  if (!Number.isSafeInteger(pid) || pid <= 1) throw new Error("Linux process PID is invalid");
  return parseProcStat(await readFile(`/proc/${pid}/stat`, "utf8")).startTimeTicks;
}

async function readProcessIdentity(pid) {
  const [executable, cwd, environment, commandLineBytes, stat, status] = await Promise.all([
    readlink(`/proc/${pid}/exe`),
    readlink(`/proc/${pid}/cwd`),
    readFile(`/proc/${pid}/environ`, "utf8"),
    readFile(`/proc/${pid}/cmdline`),
    readFile(`/proc/${pid}/stat`, "utf8"),
    readFile(`/proc/${pid}/status`, "utf8"),
  ]);
  const commandLine = commandLineBytes.toString("utf8").split("\0").filter(Boolean);
  const parsedStat = parseProcStat(stat);
  const uid = Number(/^Uid:\s+(\d+)/mu.exec(status)?.[1]);
  if (!Number.isSafeInteger(uid) || uid < 0) throw new Error("managed QF process uid is invalid");
  return {
    pid,
    executable,
    cwd,
    environment,
    commandLine,
    commandSha256: createHash("sha256").update(commandLineBytes).digest("hex"),
    uid,
    ...parsedStat,
  };
}

function hasNamespace(actual) {
  return actual.environment.split("\0").includes(`QF_PROCESS_NAMESPACE=${NAMESPACE}`);
}

function sameArguments(actual, expected) {
  return actual.length === expected.length && actual.every((value, index) => value === expected[index]);
}

function isExactSandboxedOpenHandsCommand(entry, actual, projectRoot) {
  const sidecarRoot = path.join(projectRoot, "workers", "openhands_sidecar");
  const stateRoot = path.join(projectRoot, ".local", "openhands-sessions", entry.conversationId, entry.runtimeSessionId);
  const workspaceRoot = path.join(projectRoot, ".local", "openhands-workspaces", entry.runtimeSessionId);
  const socketRoot = path.join(projectRoot, ".local", "ohs");
  const expectedArguments = [
    "--die-with-parent", "--new-session", "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-cgroup",
    "--hostname", "qf-openhands-sidecar", "--cap-drop", "ALL", "--ro-bind", "/", "/", "--proc", "/proc",
    "--dev", "/dev", "--tmpfs", "/mnt", "--tmpfs", "/root", "--tmpfs", "/tmp", "--bind", "/run/docker.sock",
    "/run/docker.sock", "--ro-bind", sidecarRoot, "/opt", "--bind", stateRoot, stateRoot, "--bind", workspaceRoot,
    workspaceRoot, "--bind", socketRoot, socketRoot, "--clearenv", "--setenv", "LANG", "C.UTF-8", "--setenv", "PATH",
    "/opt/.venv/bin:/usr/bin:/bin", "--setenv", "USER", "sandbox", "--setenv", "LOGNAME", "sandbox", "--setenv",
    "HOME", "/home/sandbox", "--setenv", "DOCKER_CONFIG", "/tmp/qf-docker-config", "--setenv",
    "QF_PROCESS_NAMESPACE", NAMESPACE, "--setenv", "QF_OPENHANDS_STATE_ROOT", stateRoot, "--setenv",
    "QF_OPENHANDS_WORKSPACE_ROOT", workspaceRoot, "--setenv", "QF_OPENHANDS_PROJECT_SOURCE", projectRoot, "--setenv",
    "QF_OPENHANDS_AGENT_SERVER_IMAGE", "qfintelligence/openhands-agent-server:1.39.0-qf.1", "--setenv",
    "LITELLM_LOCAL_MODEL_COST_MAP",
    "true", "--setenv", "OPENHANDS_SUPPRESS_BANNER", "1", "--setenv", "PYTHONHASHSEED", "0", "--chdir",
    stateRoot, "/opt/.venv/bin/python", "-I", "/opt/main.py",
  ];
  return actual.executable === "/usr/bin/bwrap"
    && path.basename(actual.commandLine[0] ?? "") === "bwrap"
    && sameArguments(actual.commandLine.slice(1), expectedArguments);
}

function assertStartsAfter(child, parent) {
  if (BigInt(child.startTimeTicks) < BigInt(parent.startTimeTicks)) {
    throw new Error("managed QF process predates its registered owner");
  }
}

function isApiAncestorCwd(cwd, projectRoot) {
  return cwd === projectRoot || cwd === path.join(projectRoot, "apps", "control-plane");
}

async function assertRegisteredApiOwner(registry, projectRoot, expected = {}) {
  const apiGroups = registry.childGroups.filter((group) => group.script === "dev:api" && group.title === "qfint-control-plane");
  if (apiGroups.length !== 1) throw new Error("registered QF dev:api process group is not unique");
  const apiGroup = apiGroups[0];
  if (!Number.isSafeInteger(apiGroup.leaderPid) || apiGroup.leaderPid <= 1
    || !/^\d+$/u.test(apiGroup.leaderStartTimeTicks ?? "")
    || !Number.isSafeInteger(registry.supervisorPid) || registry.supervisorPid <= 1
    || !/^\d+$/u.test(registry.supervisorStartTimeTicks ?? "")) {
    throw new Error("registered QF dev ownership identity is invalid");
  }
  const [apiLeader, supervisor] = await Promise.all([
    readProcessIdentity(apiGroup.leaderPid),
    readProcessIdentity(registry.supervisorPid),
  ]);
  if (apiLeader.uid !== 1000 || apiLeader.processGroupId !== apiLeader.pid || apiLeader.parentPid !== supervisor.pid
    || apiLeader.startTimeTicks !== apiGroup.leaderStartTimeTicks || apiLeader.cwd !== projectRoot || !hasNamespace(apiLeader)
    || !sameArguments(apiLeader.commandLine, ["npm run dev:api"]) || supervisor.uid !== 1000
    || supervisor.startTimeTicks !== registry.supervisorStartTimeTicks || supervisor.cwd !== projectRoot
    || !hasNamespace(supervisor) || !sameArguments(supervisor.commandLine, ["qfint-dev-supervisor"])
    || (expected.ownerApiLeaderPid !== undefined && expected.ownerApiLeaderPid !== apiLeader.pid)
    || (expected.ownerApiLeaderStartTimeTicks !== undefined
      && expected.ownerApiLeaderStartTimeTicks !== apiLeader.startTimeTicks)
    || (expected.ownerSupervisorPid !== undefined && expected.ownerSupervisorPid !== supervisor.pid)
    || (expected.ownerSupervisorStartTimeTicks !== undefined
      && expected.ownerSupervisorStartTimeTicks !== supervisor.startTimeTicks)) {
    throw new Error("registered QF dev:api owner identity changed");
  }
  assertStartsAfter(apiLeader, supervisor);
  return { apiLeader, supervisor };
}

async function assertSidecarOwnership(entry, actual, registry, projectRoot) {
  if (actual.uid !== 1000 || !hasNamespace(actual) || !isExactSandboxedOpenHandsCommand(entry, actual, projectRoot)) {
    throw new Error("managed QF process is not the exact QF OpenHands sandbox");
  }
  const { apiLeader, supervisor } = await assertRegisteredApiOwner(registry, projectRoot, entry);
  if (actual.processGroupId !== apiLeader.pid) {
    throw new Error("OpenHands sidecar escaped the registered QF dev:api process group");
  }
  let child = actual;
  const visited = new Set([actual.pid]);
  for (let depth = 0; child.parentPid !== apiLeader.pid; depth += 1) {
    if (depth >= 32 || child.parentPid <= 1 || visited.has(child.parentPid)) {
      throw new Error("OpenHands sidecar does not descend from the registered QF dev:api owner");
    }
    visited.add(child.parentPid);
    const parent = await readProcessIdentity(child.parentPid);
    if (parent.uid !== 1000 || parent.processGroupId !== apiLeader.pid
      || !hasNamespace(parent) || !isApiAncestorCwd(parent.cwd, projectRoot)) {
      throw new Error("OpenHands sidecar parent chain left the registered QF dev:api boundary");
    }
    assertStartsAfter(child, parent);
    child = parent;
  }
  assertStartsAfter(child, apiLeader);
  return {
    ownerSupervisorPid: supervisor.pid,
    ownerSupervisorStartTimeTicks: supervisor.startTimeTicks,
    ownerApiLeaderPid: apiLeader.pid,
    ownerApiLeaderStartTimeTicks: apiLeader.startTimeTicks,
  };
}

function assertRegistrationEntry(entry) {
  if (!entry || typeof entry !== "object" || Array.isArray(entry)
    || typeof entry.processKey !== "string" || !entry.processKey.trim()
    || !Number.isSafeInteger(entry.pid) || entry.pid <= 1 || !["PID", "PROCESS_GROUP"].includes(entry.killMode)) {
    throw new Error("managed QF process entry is invalid");
  }
}

function assertEntryShape(entry, projectRoot, bootId) {
  assertRegistrationEntry(entry);
  if (entry.namespace !== NAMESPACE || entry.projectRoot !== projectRoot || entry.processBootId !== bootId
    || typeof entry.processBootId !== "string" || !/^[a-f0-9-]{36}$/u.test(entry.processBootId)
    || typeof entry.processStartTimeTicks !== "string" || !/^\d+$/u.test(entry.processStartTimeTicks)
    || typeof entry.processCommandSha256 !== "string" || !/^[a-f0-9]{64}$/u.test(entry.processCommandSha256)) {
    throw new Error("managed QF process registry identity is invalid");
  }
}

function assertExpectedIdentity(entry, expected) {
  for (const [key, value] of Object.entries(expected)) {
    if (value !== undefined && entry[key] !== value) throw new Error(`managed QF process ${key} mismatch`);
  }
}

const REGISTRATION_IDENTITY_KEYS = [
  "processKey",
  "pid",
  "kind",
  "killMode",
  "campaignId",
  "runId",
  "lane",
  "conversationId",
  "runtimeSessionId",
  "processBootId",
  "processStartTimeTicks",
  "processCommandSha256",
  "expectedLeaseGeneration",
  "recovery",
];

function registrationIdentitySnapshot(entry) {
  assertRegistrationEntry(entry);
  const snapshot = {};
  for (const key of REGISTRATION_IDENTITY_KEYS) {
    if (entry[key] !== undefined) snapshot[key] = entry[key];
  }
  assertRegistrationEntry(snapshot);
  if (!/^[a-f0-9-]{36}$/u.test(snapshot.processBootId ?? "")
    || !/^\d+$/u.test(snapshot.processStartTimeTicks ?? "")
    || !/^[a-f0-9]{64}$/u.test(snapshot.processCommandSha256 ?? "")) {
    throw new Error("managed QF process replacement identity is invalid");
  }
  return snapshot;
}

function assertSameRegistrationIdentity(actual, expected) {
  const expectedSnapshot = registrationIdentitySnapshot(expected);
  const actualSnapshot = registrationIdentitySnapshot(actual);
  for (const [key, value] of Object.entries(expectedSnapshot)) {
    if (actualSnapshot[key] !== value) {
      throw new Error(`managed QF process ${key} changed before dead-process replacement`);
    }
  }
}

function expectedLaneArguments(entry) {
  return [
    "node",
    "--import",
    "tsx",
    path.join(entry.projectRoot, "apps", "control-plane", "src", "campaign", "openhands-acceptance-lane-worker.ts"),
    entry.campaignId,
    entry.runId,
    entry.lane,
  ];
}

async function assertExactRuntimeIdentity(entry, actual, registry, projectRoot) {
  if (entry.kind === "OPENHANDS_SIDECAR") {
    const expectedKey = `openhands-sidecar:${entry.conversationId}:${entry.runtimeSessionId}`;
    if (entry.processKey !== expectedKey || entry.killMode !== "PID" || typeof entry.runtimeRevision !== "string"
      || !entry.runtimeRevision || typeof entry.configHash !== "string" || !/^[a-f0-9]{64}$/u.test(entry.configHash)) {
      throw new Error("registered OpenHands sidecar identity is invalid");
    }
    return assertSidecarOwnership(entry, actual, registry, projectRoot);
  }
  const science = entry.kind === "OPENHANDS_ACCEPTANCE_SCIENCE_WORKER";
  const lane = entry.kind === "OPENHANDS_ACCEPTANCE_LANE";
  const allowedLane = science ? entry.lane === "quantum_science"
    : lane && ["openhands_orchestration", "evidence_audit"].includes(entry.lane);
  const expectedKey = science ? `openhands-acceptance-science:${entry.campaignId}:${entry.runId}`
    : `openhands-acceptance-lane:${entry.campaignId}:${entry.runId}`;
  if ((!science && !lane) || !allowedLane || entry.processKey !== expectedKey || entry.killMode !== "PROCESS_GROUP"
    || actual.uid !== 1000 || actual.cwd !== projectRoot || !hasNamespace(actual) || actual.processGroupId !== entry.pid
    || actual.executable !== process.execPath || !sameArguments(actual.commandLine, expectedLaneArguments(entry))
    || !actual.environment.split("\0").includes(`QF_PROCESS_IDENTITY=${entry.processKey}`)) {
    throw new Error(`registered acceptance ${science ? "science Worker" : "lane"} identity is invalid`);
  }
  return {};
}

async function assertStableIdentity(entry, actual, registry, projectRoot, registered = true) {
  const bootId = await readLinuxBootId();
  if (registry.bootId !== bootId) throw new Error("runtime process registry belongs to a stale Linux boot");
  if (registered) {
    assertEntryShape(entry, projectRoot, bootId);
    if (actual.startTimeTicks !== entry.processStartTimeTicks) throw new Error("managed QF process PID was reused");
    if (actual.commandSha256 !== entry.processCommandSha256) {
      throw new Error("managed QF process command identity changed");
    }
  }
  const ownership = await assertExactRuntimeIdentity({ ...entry, projectRoot }, actual, registry, projectRoot);
  const actualAgain = await readProcessIdentity(entry.pid);
  if (actualAgain.startTimeTicks !== actual.startTimeTicks || actualAgain.commandSha256 !== actual.commandSha256
    || actualAgain.executable !== actual.executable || actualAgain.processGroupId !== actual.processGroupId) {
    throw new Error("managed QF process identity changed during validation");
  }
  if (entry.killMode === "PROCESS_GROUP" && actualAgain.processGroupId !== entry.pid) {
    throw new Error("managed QF process is not its registered process-group leader");
  }
  return { actual: actualAgain, ownership, bootId };
}

function validateRegistry(registry, projectRoot) {
  if (!registry || typeof registry !== "object" || Array.isArray(registry)) {
    throw new Error("QF runtime process registry is not a JSON object");
  }
  if (registry.projectRoot !== projectRoot || registry.namespace !== NAMESPACE) {
    throw new Error("runtime process registry does not belong to this q-fintelligence checkout");
  }
  if (registry.bootId !== undefined && !/^[a-f0-9-]{36}$/u.test(registry.bootId)) {
    throw new Error("runtime process registry boot identity is invalid");
  }
  if (!Array.isArray(registry.childGroups)) throw new Error("runtime process registry childGroups is invalid");
  if (!registry.childGroups.every((group) => group && typeof group === "object" && !Array.isArray(group)
    && typeof group.script === "string" && typeof group.title === "string"
    && Number.isSafeInteger(group.leaderPid) && group.leaderPid > 1
    && (group.leaderStartTimeTicks === undefined || /^\d+$/u.test(group.leaderStartTimeTicks)))) {
    throw new Error("runtime process registry childGroups identity is invalid");
  }
  if (registry.supervisorPid !== undefined && (!Number.isSafeInteger(registry.supervisorPid) || registry.supervisorPid <= 1
    || (registry.supervisorStartTimeTicks !== undefined && !/^\d+$/u.test(registry.supervisorStartTimeTicks)))) {
    throw new Error("runtime process registry supervisor identity is invalid");
  }
  if (registry.managedProcesses !== undefined && !Array.isArray(registry.managedProcesses)) {
    throw new Error("runtime process registry managedProcesses is invalid");
  }
  return { ...registry, managedProcesses: registry.managedProcesses ?? [] };
}

function parseRegistryLock(value) {
  if (!value || typeof value !== "object" || Array.isArray(value) || value.schemaVersion !== "qf.process-registry-lock.v2"
    || !Number.isSafeInteger(value.pid) || value.pid <= 1 || !/^[a-f0-9-]{36}$/u.test(value.bootId ?? "")
    || !/^\d+$/u.test(value.ownerStartTimeTicks ?? "") || typeof value.nonce !== "string" || !value.nonce) return null;
  return value;
}

async function lockOwnerIsCurrent(lock, bootId) {
  if (!lock || lock.bootId !== bootId || !(await processExists(lock.pid))) return false;
  try {
    return await readLinuxProcessStartTimeTicks(lock.pid) === lock.ownerStartTimeTicks;
  } catch {
    return false;
  }
}

export async function acquireLinuxFileLockGuard(guardPath, timeoutMilliseconds = LOCK_TIMEOUT_MS) {
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
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk) => {
      stderr = `${stderr}${chunk}`.slice(-2_000);
    });
    await new Promise((resolve, reject) => {
      child.once("error", reject);
      child.once("exit", (code, signal) => {
        if (code === 0) resolve();
        else reject(new Error(`Linux file-lock guard failed code=${String(code)} signal=${String(signal)}: ${stderr}`));
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

async function acquireLock(runtimeDir) {
  await mkdir(runtimeDir, { recursive: true, mode: 0o700 });
  const lockPath = path.join(runtimeDir, "processes.registry.lock");
  const bootId = await readLinuxBootId();
  const ownerStartTimeTicks = await readLinuxProcessStartTimeTicks(process.pid);
  const deadline = Date.now() + LOCK_TIMEOUT_MS;
  while (Date.now() < deadline) {
    const nonce = randomUUID();
    const stagingPath = path.join(runtimeDir, `processes.registry.lock.${process.pid}.${nonce}.tmp`);
    await writeFile(stagingPath, `${JSON.stringify({
      schemaVersion: "qf.process-registry-lock.v2", pid: process.pid, bootId, ownerStartTimeTicks, nonce,
      createdAt: new Date().toISOString(),
    })}\n`, { mode: 0o600, flag: "wx" });
    try {
      await link(stagingPath, lockPath);
      return { lockPath, nonce, stagingPath };
    } catch (error) {
      await rm(stagingPath, { force: true });
      if (error.code !== "EEXIST") throw error;
      const releaseCleanupGuard = await acquireLinuxFileLockGuard(
        path.join(runtimeDir, "processes.registry.cleanup.guard"),
        Math.max(1, deadline - Date.now()),
      );
      try {
        let existing = null;
        try {
          existing = parseRegistryLock(JSON.parse(await readFile(lockPath, "utf8")));
        } catch {
          // A complete lock is hard-linked atomically, so invalid content is stale.
        }
        if (!(await lockOwnerIsCurrent(existing, bootId))) {
          await rm(lockPath, { force: true });
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

async function releaseLock(lock) {
  await rm(lock.stagingPath, { force: true });
  let current = null;
  try {
    current = parseRegistryLock(JSON.parse(await readFile(lock.lockPath, "utf8")));
  } catch {
    // Missing or invalid content is never removed on behalf of this owner.
  }
  if (current?.nonce === lock.nonce) await rm(lock.lockPath, { force: true });
}

async function writeRegistryAtomic(runtimeDir, registryPath, registry) {
  let temporaryPath = path.join(runtimeDir, `processes.${process.pid}.${randomUUID()}.tmp`);
  try {
    await writeFile(temporaryPath, `${JSON.stringify(registry, null, 2)}\n`, { mode: 0o600, flag: "wx" });
    await rename(temporaryPath, registryPath);
    temporaryPath = null;
  } finally {
    if (temporaryPath) await rm(temporaryPath, { force: true });
  }
}

export async function readRuntimeProcessRegistry(projectRoot, required = true) {
  const registryPath = path.join(projectRoot, ".runtime", "processes.json");
  try {
    return validateRegistry(JSON.parse(await readFile(registryPath, "utf8")), projectRoot);
  } catch (error) {
    if (error.code === "ENOENT" && !required) return null;
    throw error;
  }
}

async function mutateRegistry(projectRoot, mutation) {
  const runtimeDir = path.join(projectRoot, ".runtime");
  const registryPath = path.join(runtimeDir, "processes.json");
  const lock = await acquireLock(runtimeDir);
  try {
    const registry = validateRegistry(JSON.parse(await readFile(registryPath, "utf8")), projectRoot);
    const updated = validateRegistry(await mutation(registry), projectRoot);
    await writeRegistryAtomic(runtimeDir, registryPath, updated);
    return updated;
  } finally {
    await releaseLock(lock);
  }
}

export async function updateRuntimeSupervisorRegistration(projectRoot, input) {
  const runtimeDir = path.join(projectRoot, ".runtime");
  const registryPath = path.join(runtimeDir, "processes.json");
  const lock = await acquireLock(runtimeDir);
  try {
    const bootId = await readLinuxBootId();
    if (!Number.isSafeInteger(input.supervisorPid) || input.supervisorPid <= 1
      || await readLinuxProcessStartTimeTicks(input.supervisorPid) !== input.supervisorStartTimeTicks
      || !Array.isArray(input.childGroups)) throw new Error("QF supervisor registry identity is invalid");
    for (const group of input.childGroups) {
      if (!Number.isSafeInteger(group.leaderPid) || group.leaderPid <= 1
        || await readLinuxProcessStartTimeTicks(group.leaderPid) !== group.leaderStartTimeTicks) {
        throw new Error("QF supervisor child-group identity is invalid");
      }
    }
    let existing;
    try {
      existing = validateRegistry(JSON.parse(await readFile(registryPath, "utf8")), projectRoot);
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
      existing = { schemaVersion: "qf.runtime.v2", projectRoot, namespace: NAMESPACE, childGroups: [], managedProcesses: [] };
    }
    const updated = validateRegistry({
      ...existing,
      schemaVersion: "qf.runtime.v2",
      projectRoot,
      namespace: NAMESPACE,
      bootId,
      supervisorPid: input.supervisorPid,
      supervisorStartTimeTicks: input.supervisorStartTimeTicks,
      childGroups: input.childGroups.map((group) => ({ ...group })),
      managedProcesses: existing.managedProcesses,
      startedAt: input.startedAt,
      updatedAt: new Date().toISOString(),
    }, projectRoot);
    await writeRegistryAtomic(runtimeDir, registryPath, updated);
    return updated;
  } finally {
    await releaseLock(lock);
  }
}

export async function clearRuntimeSupervisorRegistration(projectRoot, expected) {
  const runtimeDir = path.join(projectRoot, ".runtime");
  const registryPath = path.join(runtimeDir, "processes.json");
  const lock = await acquireLock(runtimeDir);
  try {
    let registry;
    try {
      registry = validateRegistry(JSON.parse(await readFile(registryPath, "utf8")), projectRoot);
    } catch (error) {
      if (error.code === "ENOENT") return null;
      throw error;
    }
    if (registry.supervisorPid !== expected.supervisorPid
      || registry.supervisorStartTimeTicks !== expected.supervisorStartTimeTicks) {
      throw new Error("refused to clear a different QF supervisor registration");
    }
    const { supervisorPid: _pid, supervisorStartTimeTicks: _start, ...withoutSupervisor } = registry;
    const updated = validateRegistry({
      ...withoutSupervisor,
      childGroups: [],
      managedProcesses: registry.managedProcesses,
      stoppedAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
    }, projectRoot);
    await writeRegistryAtomic(runtimeDir, registryPath, updated);
    return updated;
  } finally {
    await releaseLock(lock);
  }
}

export async function registerManagedProcess(projectRoot, entry) {
  assertRegistrationEntry(entry);
  return mutateRegistry(projectRoot, async (registry) => {
    const bootId = await readLinuxBootId();
    if (registry.bootId !== bootId) throw new Error("runtime process registry belongs to a stale Linux boot");
    const existing = registry.managedProcesses.filter((item) => item.processKey === entry.processKey);
    if (existing.length > 1) throw new Error(`managed QF process key ${entry.processKey} is not uniquely registered`);
    if (existing.length === 1) {
      assertEntryShape(existing[0], projectRoot, existing[0].processBootId);
      if (existing[0].pid !== entry.pid) await assertRegisteredProcessDead(existing[0]);
    }
    const actual = await readProcessIdentity(entry.pid);
    const { actual: stable, ownership } = await assertStableIdentity(entry, actual, registry, projectRoot, false);
    return {
      ...registry,
      managedProcesses: [
        ...registry.managedProcesses.filter((item) => item.processKey !== entry.processKey),
        {
          ...entry,
          ...ownership,
          namespace: NAMESPACE,
          projectRoot,
          processBootId: bootId,
          processStartTimeTicks: stable.startTimeTicks,
          processCommandSha256: stable.commandSha256,
          registeredAt: new Date().toISOString(),
        },
      ],
    };
  });
}

export async function replaceDeadManagedProcess(projectRoot, entry, options) {
  assertRegistrationEntry(entry);
  const expectedCurrent = options?.expectedCurrent;
  const expectedAnchorPid = options?.expectedAnchorPid;
  assertRegistrationEntry(expectedCurrent);
  const expectedReplacementLease = Number(expectedCurrent?.expectedLeaseGeneration)
    + (expectedCurrent?.pid === expectedAnchorPid ? 1 : 0);
  if (entry.processKey !== expectedCurrent.processKey || entry.pid === expectedCurrent.pid
    || !Number.isSafeInteger(expectedAnchorPid) || expectedAnchorPid <= 1
    || entry.killMode !== "PROCESS_GROUP" || expectedCurrent.killMode !== "PROCESS_GROUP"
    || !["OPENHANDS_ACCEPTANCE_SCIENCE_WORKER", "OPENHANDS_ACCEPTANCE_LANE"].includes(entry.kind)
    || entry.kind !== expectedCurrent.kind || entry.campaignId !== expectedCurrent.campaignId
    || entry.runId !== expectedCurrent.runId || entry.lane !== expectedCurrent.lane
    || !Number.isSafeInteger(expectedCurrent.expectedLeaseGeneration)
    || expectedCurrent.expectedLeaseGeneration < 0 || typeof expectedCurrent.recovery !== "boolean"
    || !Number.isSafeInteger(entry.expectedLeaseGeneration) || entry.expectedLeaseGeneration < 1
    || entry.expectedLeaseGeneration !== expectedReplacementLease || entry.recovery !== true) {
    throw new Error("managed QF dead-process replacement request is invalid");
  }
  return mutateRegistry(projectRoot, async (registry) => {
    const bootId = await readLinuxBootId();
    if (registry.bootId !== bootId) throw new Error("runtime process registry belongs to a stale Linux boot");
    const matches = registry.managedProcesses.filter((item) => item.processKey === entry.processKey);
    if (matches.length !== 1) throw new Error(`managed QF process key ${entry.processKey} is not uniquely registered`);
    const current = matches[0];
    assertEntryShape(current, projectRoot, current.processBootId);
    assertSameRegistrationIdentity(current, expectedCurrent);
    await assertRegisteredProcessDead(current);

    let replacementAnchor;
    let replacementAttempt;
    if (current.pid === expectedAnchorPid) {
      replacementAnchor = registrationIdentitySnapshot(current);
      replacementAttempt = 1;
    } else {
      replacementAnchor = registrationIdentitySnapshot(current.replacementAnchor);
      if (replacementAnchor.processKey !== current.processKey || replacementAnchor.pid !== expectedAnchorPid
        || replacementAnchor.kind !== current.kind || replacementAnchor.campaignId !== current.campaignId
        || replacementAnchor.runId !== current.runId || replacementAnchor.lane !== current.lane
        || replacementAnchor.expectedLeaseGeneration !== entry.expectedLeaseGeneration - 1) {
        throw new Error("managed QF dead-process replacement anchor mismatch");
      }
      replacementAttempt = Number(current.replacementAttempt) + 1;
      if (!Number.isSafeInteger(replacementAttempt) || replacementAttempt <= 1) {
        throw new Error("managed QF dead-process replacement attempt is invalid");
      }
    }

    const actual = await readProcessIdentity(entry.pid);
    const { actual: stable, ownership } = await assertStableIdentity(entry, actual, registry, projectRoot, false);
    return {
      ...registry,
      managedProcesses: [
        ...registry.managedProcesses.filter((item) => item.processKey !== entry.processKey),
        {
          ...entry,
          ...ownership,
          namespace: NAMESPACE,
          projectRoot,
          processBootId: bootId,
          processStartTimeTicks: stable.startTimeTicks,
          processCommandSha256: stable.commandSha256,
          replacementAnchor,
          replacedRegistration: registrationIdentitySnapshot(current),
          replacementAttempt,
          registeredAt: new Date().toISOString(),
        },
      ],
    };
  });
}

export async function inspectRegisteredManagedProcess(projectRoot, processKey, expected = {}) {
  const registry = await readRuntimeProcessRegistry(projectRoot);
  const matches = registry.managedProcesses.filter((entry) => entry.processKey === processKey);
  if (matches.length !== 1) throw new Error(`managed QF process key ${processKey} is not uniquely registered`);
  const entry = matches[0];
  assertExpectedIdentity(entry, expected);
  const actual = await readProcessIdentity(entry.pid);
  const { actual: stable } = await assertStableIdentity(entry, actual, registry, projectRoot, true);
  return { ...entry, commandSha256: stable.commandSha256, processGroupId: stable.processGroupId };
}

async function assertSignalIdentity(projectRoot, inspectedEntry) {
  const registry = await readRuntimeProcessRegistry(projectRoot);
  const matches = registry.managedProcesses.filter((entry) => entry.processKey === inspectedEntry.processKey);
  if (matches.length !== 1) throw new Error("managed QF process registration changed before signal delivery");
  const current = matches[0];
  for (const key of ["pid", "kind", "killMode", "processBootId", "processStartTimeTicks", "processCommandSha256"]) {
    if (current[key] !== inspectedEntry[key]) throw new Error(`managed QF process ${key} changed before signal delivery`);
  }
  const actual = await readProcessIdentity(current.pid);
  const { actual: stable } = await assertStableIdentity(current, actual, registry, projectRoot, true);
  return { entry: current, actual: stable };
}

export async function assertRegisteredProcessDead(entry) {
  if (typeof entry.processBootId === "string" && entry.processBootId !== await readLinuxBootId()) {
    return {
      pid: Number(entry.pid),
      processKey: entry.processKey,
      pidAlive: false,
      processGroupAlive: false,
      staleBoot: true,
    };
  }
  const pidAlive = await processExists(Number(entry.pid));
  const groupAlive = entry.killMode === "PROCESS_GROUP" && await processGroupExists(Number(entry.pid));
  if (pidAlive || groupAlive) throw new Error("registered QF process did not terminate");
  return { pid: Number(entry.pid), processKey: entry.processKey, pidAlive: false, processGroupAlive: false };
}

export async function terminateRegisteredManagedProcess(projectRoot, processKey, expected = {}, options = {}) {
  const inspected = await inspectRegisteredManagedProcess(projectRoot, processKey, expected);
  const beforeTerm = await assertSignalIdentity(projectRoot, inspected);
  const targetPid = beforeTerm.entry.killMode === "PROCESS_GROUP" ? -beforeTerm.entry.pid : beforeTerm.entry.pid;
  // Node exposes neither pidfd_send_signal nor a pidfd operation for a process group. This complete second validation is
  // intentionally the final awaited operation before the synchronous syscall; only the kernel numeric-PID race remains.
  process.kill(targetPid, "SIGTERM");
  const graceMilliseconds = options.graceMilliseconds ?? TERMINATION_GRACE_MS;
  const deadline = Date.now() + graceMilliseconds;
  while (Date.now() < deadline) {
    try {
      return await assertRegisteredProcessDead(beforeTerm.entry);
    } catch {
      await delay(25);
    }
  }
  if (await processExists(beforeTerm.entry.pid)) {
    const beforeKill = await assertSignalIdentity(projectRoot, beforeTerm.entry);
    const killTarget = beforeKill.entry.killMode === "PROCESS_GROUP" ? -beforeKill.entry.pid : beforeKill.entry.pid;
    process.kill(killTarget, "SIGKILL");
  } else if (beforeTerm.entry.killMode === "PROCESS_GROUP" && await processGroupExists(beforeTerm.entry.pid)) {
    // A PGID cannot be reused while any member of the original group remains.
    process.kill(-beforeTerm.entry.pid, "SIGKILL");
  }
  const killDeadline = Date.now() + 2_000;
  while (Date.now() < killDeadline) {
    try {
      return await assertRegisteredProcessDead(beforeTerm.entry);
    } catch {
      await delay(25);
    }
  }
  return assertRegisteredProcessDead(beforeTerm.entry);
}

export async function unregisterManagedProcess(projectRoot, processKey, expectedPid = null) {
  return mutateRegistry(projectRoot, (registry) => ({
    ...registry,
    managedProcesses: registry.managedProcesses.filter((item) => item.processKey !== processKey
      || (expectedPid !== null && item.pid !== expectedPid)),
  }));
}

export async function unregisterCampaignProcesses(projectRoot, campaignId) {
  return mutateRegistry(projectRoot, (registry) => ({
    ...registry,
    managedProcesses: registry.managedProcesses.filter((item) => item.campaignId !== campaignId),
  }));
}
