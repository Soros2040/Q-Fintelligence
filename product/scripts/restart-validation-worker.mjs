// Authorship category: supervisor_infrastructure
// Runs the required offline quality gates between a validated project-only stop
// and a fresh preview start. This helper is launched in its own process group so
// that scripts/stop.mjs cannot terminate it with the Control Plane.

import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { open, realpath, rm, writeFile } from "node:fs/promises";
import path from "node:path";

const EXPECTED_ROOT = process.cwd();
const projectRoot = await realpath(process.cwd());
if (projectRoot !== EXPECTED_ROOT) {
  throw new Error(`restart validation must run from ${EXPECTED_ROOT}; got ${projectRoot}`);
}

const runtimeRoot = path.join(projectRoot, ".runtime");
const lockPath = path.join(runtimeRoot, "restart-validation.lock");
const reportPath = path.join(runtimeRoot, "restart-validation.json");
const previewLogPath = path.join(runtimeRoot, "preview.log");
const nodeBin = path.dirname(process.execPath);
const npm = path.join(nodeBin, "npm");
const uv = "uv";
const environment = {
  ...process.env,
  PATH: `${nodeBin}:${process.env.PATH ?? ""}`,
  LANG: "C.UTF-8",
  LC_ALL: "C.UTF-8",
  CI: "1",
  NO_COLOR: "1",
  UV_NO_PROGRESS: "1",
};

const lock = await open(lockPath, "wx", 0o600);
await lock.writeFile(`${JSON.stringify({ pid: process.pid, startedAt: new Date().toISOString() })}\n`);
await lock.close();

function digest(value) {
  return createHash("sha256").update(value).digest("hex");
}

async function run(name, executable, args, timeoutMs) {
  const startedAt = new Date().toISOString();
  const started = performance.now();
  const child = spawn(executable, args, {
    cwd: projectRoot,
    env: environment,
    shell: false,
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stdout = "";
  let stderr = "";
  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");
  child.stdout.on("data", (chunk) => { stdout = `${stdout}${chunk}`.slice(-2_000_000); });
  child.stderr.on("data", (chunk) => { stderr = `${stderr}${chunk}`.slice(-2_000_000); });
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    child.kill("SIGTERM");
  }, timeoutMs);
  const exitCode = await new Promise((resolve, reject) => {
    child.once("error", reject);
    child.once("exit", (code) => resolve(code ?? 1));
  });
  clearTimeout(timer);
  return {
    name,
    command: [executable, ...args],
    startedAt,
    completedAt: new Date().toISOString(),
    durationSeconds: Number(((performance.now() - started) / 1000).toFixed(6)),
    exitCode,
    timedOut,
    stdoutSha256: digest(stdout),
    stderrSha256: digest(stderr),
    stdoutTail: stdout.slice(-20_000),
    stderrTail: stderr.slice(-20_000),
  };
}

async function projectIsHealthy() {
  try {
    const [api, web] = await Promise.all([
      fetch("http://127.0.0.1:27872/api/health", { signal: AbortSignal.timeout(800) }),
      fetch("http://127.0.0.1:27871/", { signal: AbortSignal.timeout(800) }),
    ]);
    return api.ok && web.ok;
  } catch {
    return false;
  }
}

async function waitForHealth(expected, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if ((await projectIsHealthy()) === expected) return true;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  return false;
}

async function startPreview() {
  if (await projectIsHealthy()) return { started: false, healthy: true, reason: "already healthy" };
  const log = await open(previewLogPath, "a", 0o600);
  const child = spawn(npm, ["run", "dev"], {
    cwd: projectRoot,
    env: environment,
    detached: true,
    shell: false,
    stdio: ["ignore", log.fd, log.fd],
  });
  child.unref();
  await log.close();
  const healthy = await waitForHealth(true, 60_000);
  return { started: true, healthy, pid: child.pid ?? null };
}

const report = {
  schemaVersion: "qf.restart-validation.v1",
  authorCategory: "supervisor_infrastructure",
  projectRoot,
  processNamespace: "qfintelligence",
  allowedPorts: [27871, 27872, 27873, 27874, 27875],
  forbiddenPortsUntouched: [43187, 43188, 43189, 43190, 43191, 43192],
  startedAt: new Date().toISOString(),
  gates: [],
  xhPidsBefore: [],
  xhPidsAfter: [],
  previewRestored: false,
  status: "RUNNING",
};

try {
  const before = await run(
    "xh_process_snapshot_before",
    "/usr/bin/pgrep",
    ["-f", "[r]eserved-external-workload"],
    10_000,
  );
  report.xhPidsBefore = before.stdoutTail.split(/\r?\n/u).filter(Boolean).sort();

  const stop = await run("project_targeted_stop", npm, ["run", "stop"], 30_000);
  report.gates.push(stop);
  const stopped = await waitForHealth(false, 30_000);
  report.gates.push({ name: "project_ports_released", exitCode: stopped ? 0 : 1, timedOut: !stopped });
  if (!stopped) throw new Error("q-fintelligence preview did not release its registered ports");

  const commands = [
    ["npm_check", npm, ["run", "check"], 900_000],
    ["uv_pytest", uv, ["run", "pytest"], 900_000],
    ["uv_ruff", uv, ["run", "ruff", "check", "."], 900_000],
    ["npm_build", npm, ["run", "build"], 900_000],
    ["npm_doctor", npm, ["run", "doctor"], 300_000],
    ["startup_health_targeted_stop_smoke", "/usr/bin/bash", ["infra/wsl/smoke.sh"], 300_000],
  ];
  for (const [name, executable, args, timeoutMs] of commands) {
    report.gates.push(await run(name, executable, args, timeoutMs));
  }

  const after = await run(
    "xh_process_snapshot_after",
    "/usr/bin/pgrep",
    ["-f", "[r]eserved-external-workload"],
    10_000,
  );
  report.xhPidsAfter = after.stdoutTail.split(/\r?\n/u).filter(Boolean).sort();
  report.xhProcessSetUnchanged = JSON.stringify(report.xhPidsBefore) === JSON.stringify(report.xhPidsAfter);
  const gateFailures = report.gates.filter((item) => item.exitCode !== 0 || item.timedOut);
  report.status = gateFailures.length === 0 && report.xhProcessSetUnchanged ? "PASSED" : "FAILED";
} catch (error) {
  report.status = "FAILED";
  report.error = String(error instanceof Error ? error.message : error).slice(0, 1000);
} finally {
  try {
    report.preview = await startPreview();
    report.previewRestored = report.preview.healthy === true;
  } catch (error) {
    report.preview = { started: false, healthy: false, error: String(error).slice(0, 1000) };
  }
  report.completedAt = new Date().toISOString();
  await writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`, { mode: 0o600 });
  await rm(lockPath, { force: true });
}
