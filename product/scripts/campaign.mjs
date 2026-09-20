import { spawn, spawnSync } from "node:child_process";
import { closeSync, existsSync, mkdirSync, openSync, realpathSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";

import { inspectOwnedProcess } from "./process-safety.mjs";

const projectRoot = realpathSync(process.cwd());
const expectedRoot = process.cwd();
if (projectRoot !== expectedRoot) throw new Error(`campaign commands must run from ${expectedRoot}`);
if (existsSync(".env")) process.loadEnvFile(".env");
process.env.QF_PROCESS_NAMESPACE = "qfintelligence";
process.env.NODE_USE_ENV_PROXY ??= "1";
process.env.http_proxy ??= "http://127.0.0.1:7897";
process.env.https_proxy ??= "http://127.0.0.1:7897";
process.env.HTTP_PROXY ??= process.env.http_proxy;
process.env.HTTPS_PROXY ??= process.env.https_proxy;
process.env.no_proxy ??= "localhost,127.0.0.1,::1";
process.env.NO_PROXY ??= process.env.no_proxy;

const command = process.argv[2] ?? "status";
const runtimeRoot = path.join(projectRoot, ".runtime");
const registryPath = path.join(runtimeRoot, "campaign-processes.json");
const p03RegistryPath = path.join(runtimeRoot, "p03-queue-process.json");
const tsx = path.join(projectRoot, "node_modules", ".bin", "tsx");
const cli = path.join(projectRoot, "apps", "control-plane", "src", "campaign", "cli.ts");
const worker = path.join(projectRoot, "apps", "control-plane", "src", "campaign", "worker.ts");
const p03Worker = path.join(projectRoot, "apps", "control-plane", "src", "campaign", "p03-queue-worker.ts");

function cliJson(args) {
  const result = spawnSync(tsx, [cli, ...args], { cwd: projectRoot, env: process.env, encoding: "utf8", shell: false });
  if (result.status !== 0) throw new Error(result.stderr || result.stdout || `campaign CLI failed with ${result.status}`);
  return JSON.parse(result.stdout);
}

function registry() {
  try {
    return JSON.parse(readFileSync(registryPath, "utf8"));
  } catch (error) {
    if (error.code === "ENOENT") return null;
    throw error;
  }
}

if (command === "start" || command === "resume") {
  mkdirSync(runtimeRoot, { recursive: true });
  const current = registry();
  if (current) {
    const inspection = await inspectOwnedProcess(Number(current.pid), projectRoot);
    if (inspection.owned) throw new Error(`campaign worker ${current.pid} is already running`);
    rmSync(registryPath, { force: true });
  }
  const campaign = process.argv[3]
    ? cliJson(["status", process.argv[3]]).campaigns[0]
    : cliJson(["init"]);
  const campaignRoot = path.join(projectRoot, ".local", "campaigns", campaign.campaignId);
  const logRoot = path.join(campaignRoot, "logs");
  mkdirSync(logRoot, { recursive: true });
  const logPath = path.join(logRoot, "worker.log");
  const log = openSync(logPath, "a", 0o600);
  const child = spawn(tsx, [worker, campaign.campaignId], {
    cwd: projectRoot,
    env: { ...process.env, QF_CAMPAIGN_ID: campaign.campaignId, QF_PROCESS_NAMESPACE: "qfintelligence" },
    detached: true,
    shell: false,
    stdio: ["ignore", log, log],
    windowsHide: true,
  });
  closeSync(log);
  child.unref();
  writeFileSync(registryPath, `${JSON.stringify({
    schemaVersion: "qf.campaign-process.v1",
    projectRoot,
    namespace: "qfintelligence",
    campaignId: campaign.campaignId,
    pid: child.pid,
    logPath: path.relative(projectRoot, logPath),
    startedAt: new Date().toISOString(),
  }, null, 2)}\n`, { mode: 0o600 });
  process.stdout.write(`${JSON.stringify({ accepted: true, campaignId: campaign.campaignId, pid: child.pid, logPath }, null, 2)}\n`);
} else if (command === "status") {
  const current = registry();
  const processState = current
    ? { ...current, ...(await inspectOwnedProcess(Number(current.pid), projectRoot)) }
    : { owned: false, reason: "not-registered" };
  const campaignId = process.argv[3] ?? current?.campaignId;
  const databaseState = cliJson(campaignId ? ["status", campaignId] : ["status"]);
  process.stdout.write(`${JSON.stringify({ process: processState, ...databaseState }, null, 2)}\n`);
} else if (command === "audit" || command === "doctor" || command === "tianyan-probe" || command === "tianyan176-hardware" || command === "tianyan-config" || command === "p03-sse" || command === "p03-old-cloud" || command === "p03-queue-step" || command === "p03-finalize" || command.startsWith("p04-")) {
  const result = cliJson(command.startsWith("p04-") ? process.argv.slice(2) : process.argv[3] ? [command, process.argv[3]] : [command]);
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
} else if (command === "p03-queue-resume") {
  mkdirSync(runtimeRoot, { recursive: true });
  const campaignId = process.argv[3] ?? cliJson(["status"]).campaigns[0]?.campaignId;
  if (!campaignId) throw new Error("no P02 campaign exists");
  let current = null;
  try {
    current = JSON.parse(readFileSync(p03RegistryPath, "utf8"));
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  if (current) {
    const inspection = await inspectOwnedProcess(Number(current.pid), projectRoot);
    if (inspection.owned) throw new Error(`P03 queue worker ${current.pid} is already running`);
    rmSync(p03RegistryPath, { force: true });
  }
  const logPath = path.join(projectRoot, ".local", "campaigns", campaignId, "logs", "p03-queue.log");
  mkdirSync(path.dirname(logPath), { recursive: true });
  const log = openSync(logPath, "a", 0o600);
  const child = spawn(tsx, [p03Worker, campaignId], {
    cwd: projectRoot,
    env: { ...process.env, QF_CAMPAIGN_ID: campaignId, QF_PROCESS_NAMESPACE: "qfintelligence" },
    detached: true,
    shell: false,
    stdio: ["ignore", log, log],
    windowsHide: true,
  });
  closeSync(log);
  child.unref();
  writeFileSync(p03RegistryPath, `${JSON.stringify({
    schemaVersion: "qf.p03-queue-process.v1",
    projectRoot,
    namespace: "qfintelligence",
    campaignId,
    pid: child.pid,
    logPath: path.relative(projectRoot, logPath),
    startedAt: new Date().toISOString(),
  }, null, 2)}\n`, { mode: 0o600 });
  process.stdout.write(`${JSON.stringify({ accepted: true, campaignId, pid: child.pid, logPath }, null, 2)}\n`);
} else if (command === "p03-queue-status") {
  let current = null;
  try {
    current = JSON.parse(readFileSync(p03RegistryPath, "utf8"));
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  const processState = current
    ? { ...current, ...(await inspectOwnedProcess(Number(current.pid), projectRoot)) }
    : { owned: false, reason: "not-registered" };
  const campaignId = process.argv[3] ?? current?.campaignId;
  const databaseState = cliJson(campaignId ? ["status", campaignId] : ["status"]);
  process.stdout.write(`${JSON.stringify({ process: processState, ...databaseState }, null, 2)}\n`);
} else if (command === "p03-queue-stop") {
  let current = null;
  try {
    current = JSON.parse(readFileSync(p03RegistryPath, "utf8"));
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  if (!current) {
    process.stdout.write("No registered q-fintelligence P03 queue worker.\n");
  } else {
    const inspection = await inspectOwnedProcess(Number(current.pid), projectRoot);
    if (!inspection.owned) {
      rmSync(p03RegistryPath, { force: true });
      process.stdout.write("P03 queue registry was stale; no signal was sent.\n");
    } else {
      process.kill(-Number(current.pid), "SIGTERM");
      rmSync(p03RegistryPath, { force: true });
      process.stdout.write(`Stopped validated q-fintelligence P03 queue worker ${current.pid}.\n`);
    }
  }
} else if (command === "stop") {
  const current = registry();
  if (!current) {
    process.stdout.write("No registered q-fintelligence campaign worker.\n");
  } else {
    const inspection = await inspectOwnedProcess(Number(current.pid), projectRoot);
    if (!inspection.owned) {
      rmSync(registryPath, { force: true });
      process.stdout.write("Campaign registry was stale; no signal was sent.\n");
    } else {
      process.kill(-Number(current.pid), "SIGTERM");
      rmSync(registryPath, { force: true });
      process.stdout.write(`Stopped validated q-fintelligence campaign worker ${current.pid}.\n`);
    }
  }
} else {
  throw new Error(`unknown campaign command ${command}`);
}
