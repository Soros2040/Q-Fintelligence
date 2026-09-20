import { spawn } from "node:child_process";
import path from "node:path";

import {
  assertHardwareMutationAuthorized,
  isHardwareMutationAction,
  loadHardwareExecutionPolicy,
} from "../hardware-policy.js";

import type { JsonObject } from "@q-fintelligence/contracts";

interface ProcessResult {
  exitCode: number;
  stdout: JsonObject;
  stderr: string;
  durationSeconds: number;
}

const MAX_OUTPUT_BYTES = 8 * 1024 * 1024;

function proxyEnvironment(): NodeJS.ProcessEnv {
  const names = [
    "http_proxy",
    "https_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "no_proxy",
    "NO_PROXY",
  ] as const;
  return Object.fromEntries(names.flatMap((name) => process.env[name] ? [[name, process.env[name]]] : []));
}

async function runJsonProcess(input: {
  projectRoot: string;
  module?: string;
  scriptPath?: string;
  executable?: string;
  request: JsonObject;
  timeoutSeconds: number;
  processLimit?: number;
  environment: NodeJS.ProcessEnv;
}): Promise<ProcessResult> {
  const executable = input.executable ?? path.join(input.projectRoot, ".venv", "bin", "python");
  if (!input.module && !input.scriptPath) throw new Error("registered Python module or script is required");
  const startedAt = performance.now();
  const child = spawn("/usr/bin/timeout", [
    "--signal=KILL",
    "--kill-after=5",
    `${Math.max(1, Math.ceil(input.timeoutSeconds))}s`,
    "/usr/bin/prlimit",
    "--as=8589934592",
    `--cpu=${Math.max(60, Math.ceil(input.timeoutSeconds))}`,
    `--nproc=${input.processLimit ?? 32}`,
    "--nofile=256",
    "--",
    executable,
    "-I",
    ...(input.scriptPath ? [input.scriptPath] : ["-m", input.module!]),
  ], {
    cwd: input.projectRoot,
    env: input.environment,
    shell: false,
    stdio: ["pipe", "pipe", "pipe"],
    timeout: input.timeoutSeconds * 1000,
    killSignal: "SIGKILL",
    windowsHide: true,
  });
  const stdoutChunks: Buffer[] = [];
  const stderrChunks: Buffer[] = [];
  let outputBytes = 0;
  const capture = (target: Buffer[], chunk: Buffer) => {
    outputBytes += chunk.length;
    if (outputBytes > MAX_OUTPUT_BYTES) {
      child.kill("SIGKILL");
      return;
    }
    target.push(chunk);
  };
  child.stdout.on("data", (chunk: Buffer) => capture(stdoutChunks, chunk));
  child.stderr.on("data", (chunk: Buffer) => capture(stderrChunks, chunk));
  child.stdin.end(`${JSON.stringify(input.request)}\n`);
  const timeout = setTimeout(() => {
    if (child.exitCode === null) child.kill("SIGKILL");
  }, input.timeoutSeconds * 1000);
  const exitCode = await new Promise<number>((resolve, reject) => {
    child.once("error", reject);
    child.once("exit", (code) => resolve(code ?? 1));
  }).finally(() => clearTimeout(timeout));
  const stdoutText = Buffer.concat(stdoutChunks).toString("utf8").trim();
  const stderr = Buffer.concat(stderrChunks).toString("utf8").slice(0, 32_000);
  const stdoutLines = stdoutText.split(/\r?\n/u).map((line) => line.trim()).filter(Boolean);
  const jsonCandidate = stdoutLines.at(-1) ?? "";
  let stdout: JsonObject;
  try {
    stdout = JSON.parse(jsonCandidate) as JsonObject;
  } catch {
    throw new Error(
      `registered Python module returned invalid final-line JSON (exit=${exitCode}, stdoutLines=${stdoutLines.length}, stderrBytes=${stderr.length})`,
    );
  }
  return { exitCode, stdout, stderr, durationSeconds: (performance.now() - startedAt) / 1000 };
}

export async function runScienceJob(input: {
  projectRoot: string;
  request: JsonObject;
  timeoutSeconds?: number;
}): Promise<ProcessResult> {
  const environment: NodeJS.ProcessEnv = {
    PATH: `${path.join(input.projectRoot, ".venv", "bin")}:/usr/bin:/bin`,
    LANG: "C.UTF-8",
    LC_ALL: "C.UTF-8",
    PYTHONHASHSEED: "0",
    OMP_NUM_THREADS: "1",
    OPENBLAS_NUM_THREADS: "1",
    MKL_NUM_THREADS: "1",
    NUMEXPR_NUM_THREADS: "1",
  };
  return runJsonProcess({
    projectRoot: input.projectRoot,
    module: "qf_finance_worker.science_job",
    request: input.request,
    timeoutSeconds: input.timeoutSeconds ?? 3_600,
    environment,
  });
}

export async function runP05UploadParser(input: {
  projectRoot: string;
  request: JsonObject;
  timeoutSeconds?: number;
}): Promise<ProcessResult> {
  return runJsonProcess({
    projectRoot: input.projectRoot,
    module: "qf_finance_worker.upload_parser",
    request: input.request,
    timeoutSeconds: input.timeoutSeconds ?? 120,
    // RLIMIT_NPROC counts every thread owned by the WSL user, including the
    // already-running Node/Vite services. PyArrow needs one bounded worker
    // even with use_threads=false, so keep a finite but workspace-compatible cap.
    processLimit: 512,
    environment: {
      PATH: `${path.join(input.projectRoot, ".venv", "bin")}:/usr/bin:/bin`,
      LANG: "C.UTF-8",
      LC_ALL: "C.UTF-8",
      PYTHONHASHSEED: "0",
      OMP_NUM_THREADS: "1",
      OPENBLAS_NUM_THREADS: "1",
      MKL_NUM_THREADS: "1",
      NUMEXPR_NUM_THREADS: "1",
      ARROW_NUM_THREADS: "1",
    },
  });
}

export async function runP05QaoaGenerator(input: {
  projectRoot: string;
  request: JsonObject;
  timeoutSeconds?: number;
}): Promise<ProcessResult> {
  return runJsonProcess({
    projectRoot: input.projectRoot,
    module: "qf_quantum_worker.p05_qaoa",
    request: input.request,
    timeoutSeconds: input.timeoutSeconds ?? 600,
    environment: {
      PATH: `${path.join(input.projectRoot, ".venv", "bin")}:/usr/bin:/bin`,
      LANG: "C.UTF-8",
      LC_ALL: "C.UTF-8",
      PYTHONHASHSEED: "0",
      OMP_NUM_THREADS: "1",
      OPENBLAS_NUM_THREADS: "1",
      MKL_NUM_THREADS: "1",
      NUMEXPR_NUM_THREADS: "1",
    },
  });
}

export async function runP06WorkspaceTool(input: {
  projectRoot: string;
  request: JsonObject;
  timeoutSeconds?: number;
}): Promise<ProcessResult> {
  return runJsonProcess({
    projectRoot: input.projectRoot,
    module: "qf_quantum_worker.p06_workspace",
    request: input.request,
    timeoutSeconds: input.timeoutSeconds ?? 180,
    environment: {
      PATH: `${path.join(input.projectRoot, ".venv", "bin")}:/usr/bin:/bin`,
      LANG: "C.UTF-8",
      LC_ALL: "C.UTF-8",
      PYTHONHASHSEED: "0",
      OMP_NUM_THREADS: "1",
      OPENBLAS_NUM_THREADS: "1",
      MKL_NUM_THREADS: "1",
      NUMEXPR_NUM_THREADS: "1",
    },
  });
}

export async function runP07Pipeline(input: {
  projectRoot: string;
  request: JsonObject;
  timeoutSeconds?: number;
}): Promise<ProcessResult> {
  return runJsonProcess({
    projectRoot: input.projectRoot,
    module: "qf_finance_worker.p07_pipeline",
    request: input.request,
    timeoutSeconds: input.timeoutSeconds ?? 600,
    environment: {
      PATH: `${path.join(input.projectRoot, ".venv", "bin")}:/usr/bin:/bin`,
      LANG: "C.UTF-8",
      LC_ALL: "C.UTF-8",
      PYTHONHASHSEED: "0",
      OMP_NUM_THREADS: "1",
      OPENBLAS_NUM_THREADS: "1",
      MKL_NUM_THREADS: "1",
      NUMEXPR_NUM_THREADS: "1",
    },
  });
}

export async function runP07QaoaGenerator(input: {
  projectRoot: string;
  request: JsonObject;
  timeoutSeconds?: number;
}): Promise<ProcessResult> {
  return runJsonProcess({
    projectRoot: input.projectRoot,
    module: "qf_quantum_worker.p07_qaoa",
    request: input.request,
    timeoutSeconds: input.timeoutSeconds ?? 900,
    environment: {
      PATH: `${path.join(input.projectRoot, ".venv", "bin")}:/usr/bin:/bin`,
      LANG: "C.UTF-8",
      LC_ALL: "C.UTF-8",
      PYTHONHASHSEED: "0",
      OMP_NUM_THREADS: "1",
      OPENBLAS_NUM_THREADS: "1",
      MKL_NUM_THREADS: "1",
      NUMEXPR_NUM_THREADS: "1",
    },
  });
}

export async function runP07CapabilityReference(input: {
  projectRoot: string;
  request: JsonObject;
  timeoutSeconds?: number;
}): Promise<ProcessResult> {
  const capabilityRoot = path.join(input.projectRoot, "99_local_cache", "p07-capability-venv");
  return runJsonProcess({
    projectRoot: input.projectRoot,
    scriptPath: path.join(input.projectRoot, "workers", "capabilities", "p07_reference_adapter.py"),
    executable: path.join(capabilityRoot, "bin", "python"),
    request: input.request,
    timeoutSeconds: input.timeoutSeconds ?? 300,
    // RLIMIT_NPROC is per WSL user rather than per child process.  The
    // isolated NumPy/Qiskit reference still needs headroom alongside the
    // already-running Node, Vite, OpenHands sidecar and monitoring processes.
    processLimit: 512,
    environment: {
      PATH: `${path.join(capabilityRoot, "bin")}:/usr/bin:/bin`,
      LANG: "C.UTF-8",
      LC_ALL: "C.UTF-8",
      PYTHONHASHSEED: "0",
      QISKIT_PARALLEL: "FALSE",
      OMP_NUM_THREADS: "1",
      OPENBLAS_NUM_THREADS: "1",
      MKL_NUM_THREADS: "1",
      NUMEXPR_NUM_THREADS: "1",
    },
  });
}

export async function runTianyanJob(input: {
  projectRoot: string;
  request: JsonObject;
  timeoutSeconds?: number;
}): Promise<ProcessResult> {
  if (isHardwareMutationAction(input.request.action)) {
    const circuits = Array.isArray(input.request.circuits) ? input.request.circuits.length : 1;
    assertHardwareMutationAuthorized(loadHardwareExecutionPolicy(), {
      operation: `TianYan ${String(input.request.action)}`,
      authorizationBasis: input.request.authorization_basis,
      jobCount: circuits,
      shotsPerJob: Number(input.request.shots ?? 0),
      target: String(input.request.machine_name ?? ""),
    });
  }
  const connectionKey = process.env.TIANYAN_CONNECTION_KEY;
  if (!connectionKey) throw new Error("TIANYAN_CONNECTION_KEY is not configured");
  return runJsonProcess({
    projectRoot: input.projectRoot,
    module: "qf_quantum_worker.tianyan_job",
    request: input.request,
    timeoutSeconds: input.timeoutSeconds ?? 180,
    environment: {
      PATH: `${path.join(input.projectRoot, ".venv", "bin")}:/usr/bin:/bin`,
      LANG: "C.UTF-8",
      LC_ALL: "C.UTF-8",
      TIANYAN_CONNECTION_KEY: connectionKey,
      OMP_NUM_THREADS: "1",
      OPENBLAS_NUM_THREADS: "1",
      MKL_NUM_THREADS: "1",
      NUMEXPR_NUM_THREADS: "1",
      ...proxyEnvironment(),
    },
  });
}

export async function runGeneratedToolJob(input: {
  projectRoot: string;
  request: JsonObject;
  timeoutSeconds?: number;
}): Promise<ProcessResult> {
  return runJsonProcess({
    projectRoot: input.projectRoot,
    module: "qf_finance_worker.tool_factory_job",
    request: input.request,
    timeoutSeconds: input.timeoutSeconds ?? 180,
    environment: {
      PATH: `${path.join(input.projectRoot, ".venv", "bin")}:/usr/bin:/bin`,
      LANG: "C.UTF-8",
      LC_ALL: "C.UTF-8",
      PYTHONHASHSEED: "0",
      OMP_NUM_THREADS: "1",
      OPENBLAS_NUM_THREADS: "1",
      MKL_NUM_THREADS: "1",
      NUMEXPR_NUM_THREADS: "1",
    },
  });
}
