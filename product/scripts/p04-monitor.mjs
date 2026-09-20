import { appendFileSync, existsSync, realpathSync } from "node:fs";
import { spawnSync } from "node:child_process";
import path from "node:path";

const projectRoot = realpathSync(process.cwd());
const expectedRoot = process.cwd();
if (projectRoot !== expectedRoot) throw new Error(`P04 monitor must run from ${expectedRoot}`);
const campaignId = process.argv[2];
if (!campaignId) throw new Error("P04 monitor requires a Campaign ID");
if (existsSync(".env")) process.loadEnvFile(".env");
const monitorPath = path.join(projectRoot, ".local", "campaigns", campaignId, "monitor.ndjson");

function status() {
  const result = spawnSync(process.execPath, ["scripts/p04.mjs", "status", campaignId], {
    cwd: projectRoot,
    env: process.env,
    encoding: "utf8",
    shell: false,
    timeout: 30_000,
    maxBuffer: 8 * 1024 * 1024,
  });
  if (result.status !== 0) throw new Error(result.stderr || result.stdout || "P04 status failed");
  return JSON.parse(result.stdout);
}

function sample() {
  const current = status();
  const campaign = current.detail.campaign;
  const processByLane = new Map(current.process.lanes.map((lane) => [lane.lane, lane]));
  const heartbeatAges = current.detail.runs.map((run) => ({
    lane: run.lane,
    seconds: run.heartbeatAt ? (Date.now() - Date.parse(run.heartbeatAt)) / 1000 : null,
  }));
  const alerts = [];
  for (const run of current.detail.runs) {
    if (run.status === "FAILED") alerts.push(`${run.lane}:FAILED`);
    const process = processByLane.get(run.lane);
    if (run.status !== "COMPLETED" && process?.owned !== true) alerts.push(`${run.lane}:PROCESS_NOT_OWNED`);
  }
  for (const heartbeat of heartbeatAges) {
    if (heartbeat.seconds !== null && heartbeat.seconds > 90
      && current.detail.runs.find((run) => run.lane === heartbeat.lane)?.status !== "COMPLETED") {
      alerts.push(`${heartbeat.lane}:STALE_HEARTBEAT_${heartbeat.seconds.toFixed(1)}s`);
    }
  }
  if (campaign.hardwareJobs > 1) alerts.push(`HARDWARE_JOBS_${campaign.hardwareJobs}`);
  if (campaign.wallClockSeconds > 1_200) {
    if (current.detail.tools.length !== 2
      || current.detail.tools.some((tool) => tool.status !== "REGISTERED" || tool.invocationCount < 1)) {
      alerts.push("TOOL_FACTORY_GATE");
    }
    if (current.detail.faults.length !== 2 || current.detail.faults.some((fault) => fault.state !== "RECOVERED")) {
      alerts.push("FAULT_RECOVERY_GATE");
    }
  }
  const record = {
    capturedAt: new Date().toISOString(),
    campaignId,
    wallClockSeconds: campaign.wallClockSeconds,
    remainingSeconds: Math.max(0, campaign.minimumRuntimeSeconds - campaign.wallClockSeconds),
    activeComputeSeconds: campaign.activeComputeSeconds,
    externalWaitSeconds: campaign.externalWaitSeconds,
    hardwareJobs: campaign.hardwareJobs,
    llmCalls: campaign.llmCalls,
    runs: current.detail.runs.map((run) => ({
      lane: run.lane,
      status: run.status,
      events: run.eventCount,
      artifacts: run.artifactCount,
    })),
    heartbeatAges,
    tools: current.detail.tools.map((tool) => ({ name: tool.name, status: tool.status, calls: tool.invocationCount })),
    faults: current.detail.faults.map((fault) => ({ kind: fault.kind, state: fault.state })),
    alerts,
  };
  appendFileSync(monitorPath, `${JSON.stringify(record)}\n`, { encoding: "utf8", mode: 0o600 });
  process.stdout.write(`${JSON.stringify(record)}\n`);
  if (alerts.length > 0) process.exitCode = 2;
  const completed = campaign.wallClockSeconds >= campaign.minimumRuntimeSeconds
    && current.detail.runs.length === 3
    && current.detail.runs.every((run) => run.status === "COMPLETED");
  return { completed, alert: alerts.length > 0 };
}

while (true) {
  const result = sample();
  if (result.completed || result.alert) break;
  await new Promise((resolve) => setTimeout(resolve, 45_000));
}
