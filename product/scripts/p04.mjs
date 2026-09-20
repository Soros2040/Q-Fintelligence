import { spawn, spawnSync } from "node:child_process";
import { closeSync, existsSync, mkdirSync, openSync, realpathSync, readFileSync, writeFileSync } from "node:fs";
import { rm } from "node:fs/promises";
import path from "node:path";

import { inspectOwnedProcess } from "./process-safety.mjs";

const projectRoot = realpathSync(process.cwd());
const expectedRoot = process.cwd();
if (projectRoot !== expectedRoot) throw new Error(`P04 commands must run from ${expectedRoot}`);
if (existsSync(".env")) process.loadEnvFile(".env");
process.env.QF_PROCESS_NAMESPACE = "qfintelligence";
process.env.NODE_USE_ENV_PROXY ??= "1";
process.env.http_proxy ??= "http://127.0.0.1:7897";
process.env.https_proxy ??= "http://127.0.0.1:7897";
process.env.HTTP_PROXY ??= process.env.http_proxy;
process.env.HTTPS_PROXY ??= process.env.https_proxy;
process.env.no_proxy ??= "localhost,127.0.0.1,::1";
process.env.NO_PROXY ??= process.env.no_proxy;

const runtimeRoot = path.join(projectRoot, ".runtime");
const registryPath = path.join(runtimeRoot, "p04-processes.json");
const tsx = path.join(projectRoot, "node_modules", ".bin", "tsx");
const cli = path.join(projectRoot, "apps", "control-plane", "src", "campaign", "cli.ts");
const worker = path.join(projectRoot, "apps", "control-plane", "src", "campaign", "p04-lane-worker.ts");
const command = process.argv[2] ?? "status";

function cliJson(args) {
  const result = spawnSync(tsx, [cli, ...args], { cwd: projectRoot, env: process.env, encoding: "utf8", shell: false });
  if (result.status !== 0) throw new Error(result.stderr || result.stdout || `P04 CLI failed with ${result.status}`);
  return JSON.parse(result.stdout);
}

function loadRegistry() {
  try {
    return JSON.parse(readFileSync(registryPath, "utf8"));
  } catch (error) {
    if (error.code === "ENOENT") return null;
    throw error;
  }
}

function saveRegistry(registry) {
  mkdirSync(runtimeRoot, { recursive: true });
  writeFileSync(registryPath, `${JSON.stringify(registry, null, 2)}\n`, { mode: 0o600 });
}

function spawnLane(campaignId, run, recovery) {
  const logRoot = path.join(projectRoot, ".local", "campaigns", campaignId, "logs");
  mkdirSync(logRoot, { recursive: true });
  const logPath = path.join(logRoot, `p04-${run.lane}.log`);
  const log = openSync(logPath, "a", 0o600);
  const child = spawn(tsx, [worker, campaignId, run.runId, run.lane], {
    cwd: projectRoot,
    env: {
      ...process.env,
      QF_CAMPAIGN_ID: campaignId,
      QF_P04_RUN_ID: run.runId,
      QF_P04_LANE: run.lane,
      QF_P04_RECOVERY: recovery ? "1" : "0",
      QF_PROCESS_NAMESPACE: "qfintelligence",
    },
    detached: true,
    shell: false,
    stdio: ["ignore", log, log],
    windowsHide: true,
  });
  closeSync(log);
  child.unref();
  return {
    lane: run.lane,
    runId: run.runId,
    pid: child.pid,
    recovery,
    logPath: path.relative(projectRoot, logPath),
    startedAt: new Date().toISOString(),
  };
}

async function inspectedRegistry(registry) {
  if (!registry) return null;
  const lanes = [];
  for (const lane of registry.lanes ?? []) {
    lanes.push({ ...lane, ...(await inspectOwnedProcess(Number(lane.pid), projectRoot)) });
  }
  return { ...registry, lanes };
}

if (command === "start") {
  mkdirSync(runtimeRoot, { recursive: true });
  const current = await inspectedRegistry(loadRegistry());
  if (current?.lanes.some((lane) => lane.owned)) throw new Error("a P04 lane worker is already running");
  const guard = spawnSync(process.execPath, [path.join(projectRoot, "scripts", "port-guard.mjs"), "--check"], {
    cwd: projectRoot,
    env: process.env,
    encoding: "utf8",
    shell: false,
  });
  if (guard.status !== 0) throw new Error(guard.stderr || guard.stdout || "P04 port guard failed");
  const campaignId = process.argv[3] ?? cliJson(["p04-init"]).campaign.campaignId;
  const detail = cliJson(["p04-prepare", campaignId]);
  const lanes = detail.runs.map((run) => spawnLane(campaignId, run, false));
  const registry = {
    schemaVersion: "qf.p04-process-registry.v1",
    projectRoot,
    namespace: "qfintelligence",
    campaignId,
    lanes,
    startedAt: new Date().toISOString(),
  };
  saveRegistry(registry);
  process.stdout.write(`${JSON.stringify({ accepted: true, ...registry }, null, 2)}\n`);
} else if (command === "status") {
  const registry = loadRegistry();
  const campaignId = process.argv[3] ?? registry?.campaignId;
  const detail = campaignId ? cliJson(["p04-status", campaignId]) : null;
  process.stdout.write(`${JSON.stringify({ process: await inspectedRegistry(registry), detail }, null, 2)}\n`);
} else if (command === "resume-lane") {
  const campaignId = process.argv[3];
  const laneName = process.argv[4];
  if (!campaignId || !laneName) throw new Error("resume-lane requires Campaign ID and lane");
  const registry = loadRegistry();
  if (!registry || registry.campaignId !== campaignId) throw new Error("P04 registry does not match the Campaign");
  const previous = registry.lanes.find((lane) => lane.lane === laneName);
  if (!previous) throw new Error(`P04 lane ${laneName} is not registered`);
  const inspection = await inspectOwnedProcess(Number(previous.pid), projectRoot);
  if (inspection.owned) throw new Error(`P04 lane ${laneName} is still running`);
  const detail = cliJson(["p04-status", campaignId]);
  const run = detail.runs.find((item) => item.lane === laneName);
  if (!run || run.runId !== previous.runId) throw new Error("P04 recovery would not reuse the original Run ID");
  cliJson(["p04-assert-writable", campaignId]);
  const resumed = spawnLane(campaignId, run, true);
  registry.lanes = registry.lanes.map((lane) => lane.lane === laneName ? resumed : lane);
  saveRegistry(registry);
  process.stdout.write(`${JSON.stringify({ accepted: true, campaignId, ...resumed }, null, 2)}\n`);
} else if (command === "stop-lane") {
  const campaignId = process.argv[3];
  const laneName = process.argv[4];
  if (!campaignId || !laneName) throw new Error("stop-lane requires Campaign ID and lane");
  const registry = loadRegistry();
  if (!registry || registry.campaignId !== campaignId) throw new Error("P04 registry does not match the Campaign");
  const lane = registry.lanes.find((item) => item.lane === laneName);
  if (!lane) throw new Error(`P04 lane ${laneName} is not registered`);
  const inspection = await inspectOwnedProcess(Number(lane.pid), projectRoot);
  if (!inspection.owned) throw new Error(`P04 lane ${laneName} is not a live validated project process`);
  process.kill(-Number(lane.pid), "SIGTERM");
  registry.lanes = registry.lanes.map((item) => item.lane === laneName
    ? { ...item, maintenanceStoppedAt: new Date().toISOString() }
    : item);
  saveRegistry(registry);
  process.stdout.write(`${JSON.stringify({ stopped: true, campaignId, lane: laneName, pid: lane.pid }, null, 2)}\n`);
} else if (command === "inject-science" || command === "inject-query") {
  const campaignId = process.argv[3];
  if (!campaignId) throw new Error(`${command} requires Campaign ID`);
  const registry = loadRegistry();
  if (!registry || registry.campaignId !== campaignId) throw new Error("P04 registry does not match the Campaign");
  const lane = registry.lanes.find((item) => item.lane === "finance");
  if (!lane) throw new Error("P04 finance lane is not registered");
  const inspection = await inspectOwnedProcess(Number(lane.pid), projectRoot);
  if (!inspection.owned) throw new Error("P04 finance lane is not a live validated project process");
  const latest = cliJson(["p04-latest-event", campaignId, "finance"]);
  const expectedEvent = command === "inject-science" ? "STAGE_STARTED" : "EXTERNAL_QUERY_STARTED";
  if (latest.latestEvent?.eventType !== expectedEvent) {
    throw new Error(`${command} requires latest finance event ${expectedEvent}; got ${latest.latestEvent?.eventType ?? "none"}`);
  }
  if (command === "inject-science" && latest.latestEvent.payload?.stage !== "reproducibility_block_0") {
    throw new Error("science fault injection is limited to the first post-core reproducibility block");
  }
  const kind = command === "inject-science"
    ? "SCIENCE_PROCESS_TERMINATION"
    : "EXTERNAL_QUERY_PROCESS_TERMINATION";
  const fault = cliJson(["p04-plan-fault", campaignId, "finance", kind, String(lane.pid)]);
  process.kill(-Number(lane.pid), "SIGTERM");
  cliJson(["p04-mark-fault-injected", fault.faultId]);
  registry.lanes = registry.lanes.map((item) => item.lane === "finance"
    ? { ...item, faultInjected: kind, stoppedAt: new Date().toISOString() }
    : item);
  saveRegistry(registry);
  process.stdout.write(`${JSON.stringify({ injected: true, campaignId, lane: "finance", pid: lane.pid, faultId: fault.faultId, kind }, null, 2)}\n`);
} else if (command === "stop") {
  const registry = loadRegistry();
  if (!registry) {
    process.stdout.write("No registered q-fintelligence P04 workers.\n");
  } else {
    const stopped = [];
    for (const lane of registry.lanes ?? []) {
      const inspection = await inspectOwnedProcess(Number(lane.pid), projectRoot);
      if (!inspection.owned) continue;
      process.kill(-Number(lane.pid), "SIGTERM");
      stopped.push({ lane: lane.lane, pid: lane.pid });
    }
    await rm(registryPath, { force: true });
    process.stdout.write(`${JSON.stringify({ stopped }, null, 2)}\n`);
  }
} else {
  throw new Error(`unknown P04 command ${command}`);
}
