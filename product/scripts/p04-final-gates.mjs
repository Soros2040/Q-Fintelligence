import { spawn, spawnSync } from "node:child_process";
import { closeSync, existsSync, openSync, readFileSync, realpathSync, writeFileSync } from "node:fs";
import { mkdir } from "node:fs/promises";
import path from "node:path";

const projectRoot = realpathSync(process.cwd());
const expectedRoot = process.cwd();
if (projectRoot !== expectedRoot) throw new Error(`P04 final gates must run from ${expectedRoot}`);
const campaignId = process.argv[2];
if (!campaignId) throw new Error("P04 final gates require a Campaign ID");
if (existsSync(".env")) process.loadEnvFile(".env");

const writableGuard = spawnSync(
  path.join(projectRoot, "node_modules", ".bin", "tsx"),
  [path.join(projectRoot, "apps", "control-plane", "src", "campaign", "cli.ts"), "p04-assert-writable", campaignId],
  { cwd: projectRoot, env: process.env, encoding: "utf8", shell: false },
);
if (writableGuard.status !== 0) {
  throw new Error(writableGuard.stderr || writableGuard.stdout || "P04 writable-conversation gate failed");
}

const outputRoot = path.join(projectRoot, ".local", "campaigns", campaignId, "final-gates");
await mkdir(outputRoot, { recursive: true });
const outputPath = path.join(projectRoot, ".local", "campaigns", campaignId, "final-gates.json");

function commandGate(name, command, args, timeoutMs = 900_000) {
  const startedAt = new Date().toISOString();
  const started = performance.now();
  const result = spawnSync(command, args, {
    cwd: projectRoot,
    env: process.env,
    encoding: "utf8",
    shell: false,
    timeout: timeoutMs,
    maxBuffer: 16 * 1024 * 1024,
  });
  const gate = {
    name,
    command: [command, ...args],
    startedAt,
    durationSeconds: (performance.now() - started) / 1000,
    exitCode: result.status ?? (result.error ? 1 : 0),
    signal: result.signal,
    stdout: String(result.stdout ?? "").slice(-40_000),
    stderr: String(result.stderr ?? "").slice(-40_000),
    passed: result.status === 0,
  };
  writeFileSync(path.join(outputRoot, `${name}.json`), `${JSON.stringify(gate, null, 2)}\n`, { mode: 0o600 });
  return gate;
}

function secretScan() {
  const filesResult = spawnSync("git", ["ls-files", "-co", "--exclude-standard", "-z"], {
    cwd: projectRoot,
    encoding: "buffer",
    shell: false,
  });
  if (filesResult.status !== 0) throw new Error("git file inventory failed during the secret scan");
  const files = filesResult.stdout.toString("utf8").split("\0").filter(Boolean);
  const rules = [
    ["private-key", /-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----/u],
    ["openai-style-token", /\bsk-[A-Za-z0-9_-]{20,}\b/u],
    ["literal-project-secret", /(?:OPENAI_API_KEY|TUSHARE_TOKEN|TIANYAN_CONNECTION_KEY)\s*[:=]\s*["'][A-Za-z0-9_./+=-]{16,}["']/u],
  ];
  const findings = [];
  let scannedFiles = 0;
  for (const relative of files) {
    const pathname = path.join(projectRoot, relative);
    let data;
    try {
      data = readFileSync(pathname);
    } catch {
      continue;
    }
    if (data.byteLength > 2_000_000 || data.includes(0)) continue;
    scannedFiles += 1;
    const text = data.toString("utf8");
    for (const [rule, pattern] of rules) {
      const match = text.match(pattern);
      if (match && !/(?:test|fake|dummy|example|placeholder|not-a-real)/iu.test(match[0])) {
        findings.push({ file: relative, rule });
      }
    }
  }
  return {
    name: "secret-scan",
    scannedFiles,
    rules: rules.map(([name]) => name),
    findings,
    passed: findings.length === 0,
  };
}

async function serviceGate() {
  const stdoutFd = openSync(path.join(outputRoot, "dev-services.stdout.log"), "w", 0o600);
  const stderrFd = openSync(path.join(outputRoot, "dev-services.stderr.log"), "w", 0o600);
  const supervisor = spawn("npm", ["run", "dev"], {
    cwd: projectRoot,
    env: process.env,
    detached: true,
    shell: false,
    stdio: ["ignore", stdoutFd, stderrFd],
  });
  closeSync(stdoutFd);
  closeSync(stderrFd);
  supervisor.unref();
  const startedAt = new Date().toISOString();
  const deadline = Date.now() + 45_000;
  let health = null;
  let campaign = null;
  let web = null;
  try {
    while (Date.now() < deadline) {
      try {
        const response = await fetch("http://127.0.0.1:27872/api/health", { signal: AbortSignal.timeout(1_500) });
        if (response.ok) {
          health = { status: response.status, body: await response.json() };
          break;
        }
      } catch {
        // The registered project services are still starting.
      }
      await new Promise((resolve) => setTimeout(resolve, 200));
    }
    if (!health) throw new Error("P04 API readiness timed out");
    const campaignResponse = await fetch(`http://127.0.0.1:27872/api/campaigns/${campaignId}/p04`, {
      signal: AbortSignal.timeout(5_000),
    });
    campaign = { status: campaignResponse.status, body: await campaignResponse.json() };
    while (Date.now() < deadline) {
      try {
        const webResponse = await fetch("http://127.0.0.1:27871/", { signal: AbortSignal.timeout(1_500) });
        if (webResponse.ok) {
          const html = await webResponse.text();
          web = { status: webResponse.status, htmlBytes: Buffer.byteLength(html), hasRoot: html.includes('id="root"') };
          break;
        }
      } catch {
        // Vite readiness follows API readiness but is not instantaneous.
      }
      await new Promise((resolve) => setTimeout(resolve, 200));
    }
    if (!web) throw new Error("P04 Web readiness timed out");
  } finally {
    const stop = spawnSync("npm", ["run", "stop"], {
      cwd: projectRoot,
      env: process.env,
      encoding: "utf8",
      shell: false,
      timeout: 30_000,
    });
    if (stop.status !== 0) {
      throw new Error(`registered q-fintelligence service stop failed: ${String(stop.stderr || stop.stdout)}`);
    }
  }
  return {
    name: "api-web-runtime",
    startedAt,
    health,
    campaignStatus: campaign?.status,
    campaignId: campaign?.body?.campaign?.campaignId ?? null,
    runCount: Array.isArray(campaign?.body?.runs) ? campaign.body.runs.length : 0,
    web,
    passed: health?.status === 200
      && campaign?.status === 200
      && campaign?.body?.campaign?.campaignId === campaignId
      && campaign?.body?.runs?.length === 3
      && web?.status === 200
      && web.hasRoot === true,
  };
}

const gates = [];
gates.push(commandGate("git-diff-check", "git", ["diff", "--check"]));
gates.push(commandGate("npm-check", "npm", ["run", "check"]));
gates.push(commandGate("python-pytest", "uv", ["run", "pytest"]));
gates.push(commandGate("python-ruff", "uv", ["run", "ruff", "check", "."]));
gates.push(commandGate("campaign-tests", "npm", ["run", "campaign:test"]));
gates.push(commandGate("wsl-doctor", "npm", ["run", "doctor"]));
gates.push(commandGate("campaign-doctor", "npm", ["run", "campaign:doctor"]));
gates.push(secretScan());
gates.push(await serviceGate());

const evidence = {
  schemaVersion: "qf.p04.final-test-gates.v1",
  capturedAt: new Date().toISOString(),
  campaignId,
  projectRoot,
  fixedModel: "openai/getoken/gpt-5.6-sol",
  gates,
  passed: gates.every((gate) => gate.passed === true),
};
writeFileSync(outputPath, `${JSON.stringify(evidence, null, 2)}\n`, { mode: 0o600 });
process.stdout.write(`${JSON.stringify({ outputPath, passed: evidence.passed, gates: gates.map(({ name, passed }) => ({ name, passed })) }, null, 2)}\n`);
if (!evidence.passed) process.exitCode = 1;
