import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import path from "node:path";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

function missingDockerObject(error) {
  const stderr = typeof error?.stderr === "string" ? error.stderr : "";
  return /No such (?:container|object)|network .* not found/iu.test(stderr);
}

async function dockerOutput(arguments_, allowMissing = false) {
  try {
    const result = await execFileAsync("docker", arguments_, {
      env: { PATH: process.env.PATH ?? "/usr/local/bin:/usr/bin:/bin" },
      maxBuffer: 1024 * 1024,
      timeout: 60_000,
      windowsHide: true,
    });
    return result.stdout.trim();
  } catch (error) {
    if (allowMissing && missingDockerObject(error)) return null;
    throw error;
  }
}

export function openHandsSandboxResourceNames(projectRoot, conversationId, runtimeSessionId) {
  const base = path.resolve(projectRoot, ".local", "openhands-sessions");
  const stateRoot = path.resolve(base, conversationId, runtimeSessionId);
  const expected = path.join(base, conversationId, runtimeSessionId);
  if (stateRoot !== expected || !stateRoot.startsWith(`${base}${path.sep}`)) {
    throw new Error("refused to derive OpenHands resources outside the exact QF session root");
  }
  const slug = createHash("sha256").update(stateRoot).digest("hex").slice(0, 16);
  return {
    slug,
    stateRoot,
    containers: [
      `qf-oh-agent-${slug}`,
      `qf-oh-gateway-${slug}`,
      `qf-oh-proxy-${slug}`,
      `qf-oh-tools-${slug}`,
    ],
    networks: [
      `qf-oh-net-${slug}`,
      `qf-oh-ingress-${slug}`,
      `qf-oh-egress-${slug}`,
    ],
  };
}

export async function cleanupDeadOpenHandsSandbox(projectRoot, entry) {
  if (entry.kind !== "OPENHANDS_SIDECAR") return { containers: 0, networks: 0 };
  const resources = openHandsSandboxResourceNames(
    projectRoot,
    entry.conversationId,
    entry.runtimeSessionId,
  );
  const containers = [];
  for (const name of resources.containers) {
    const inspected = await dockerOutput([
      "inspect",
      "--format",
      '{{index .Config.Labels "qf.openhands.run"}}|{{.Name}}',
      name,
    ], true);
    if (inspected === null) continue;
    if (inspected !== `${resources.slug}|/${name}`) {
      throw new Error(`refused to remove Docker container without exact QF ownership: ${name}`);
    }
    containers.push(name);
  }
  if (containers.length > 0) await dockerOutput(["rm", "--force", ...containers]);

  const networks = [];
  for (const name of resources.networks) {
    const inspected = await dockerOutput([
      "network",
      "inspect",
      "--format",
      "{{.Name}}|{{len .Containers}}",
      name,
    ], true);
    if (inspected === null) continue;
    if (inspected !== `${name}|0`) {
      throw new Error(`refused to remove non-empty or mismatched QF Docker network: ${name}`);
    }
    networks.push(name);
  }
  if (networks.length > 0) await dockerOutput(["network", "rm", ...networks]);
  return { containers: containers.length, networks: networks.length };
}
