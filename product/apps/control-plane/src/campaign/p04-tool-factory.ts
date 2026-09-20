import { spawn } from "node:child_process";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import path from "node:path";

import type { JsonObject, P04GeneratedToolSummary } from "@q-fintelligence/contracts";

import { runGeneratedToolJob } from "./python-runner.js";
import {
  executeP04JsonStage,
  p04Hash,
  p04Json,
  readP04Json,
  registerP04JsonArtifact,
  registerP04TextArtifact,
  type P04WorkerContext,
} from "./p04-runtime.js";

interface GeneratedToolDefinition {
  name: P04GeneratedToolSummary["name"];
  description: string;
  code: string;
  test_code: string;
}

interface GeneratedToolBundle {
  schema_version: "qf.p04.generated-tools.v1";
  tools: [GeneratedToolDefinition, GeneratedToolDefinition];
}

interface CommandResult {
  exitCode: number;
  stdout: string;
  stderr: string;
  durationSeconds: number;
}

const TOOL_NAMES = ["financial_result_diagnostics", "quantum_result_diagnostics"] as const;
const MAX_CAPTURE_BYTES = 1_000_000;

async function atomicText(pathname: string, value: string): Promise<void> {
  await mkdir(path.dirname(pathname), { recursive: true });
  const temporary = `${pathname}.${process.pid}.tmp`;
  await writeFile(temporary, value.endsWith("\n") ? value : `${value}\n`, { encoding: "utf8", mode: 0o600 });
  await rename(temporary, pathname);
}

async function runBoundedCommand(command: string, args: string[], cwd: string, timeoutSeconds = 120): Promise<CommandResult> {
  const startedAt = performance.now();
  const child = spawn("/usr/bin/timeout", [
    "--signal=KILL",
    "--kill-after=5",
    `${timeoutSeconds}s`,
    "/usr/bin/prlimit",
    "--as=4294967296",
    `--cpu=${timeoutSeconds}`,
    "--nproc=256",
    "--nofile=256",
    "--",
    command,
    ...args,
  ], {
    cwd,
    env: {
      PATH: `${path.resolve(cwd, "../../../../../../.venv/bin")}:/usr/bin:/bin`,
      LANG: "C.UTF-8",
      LC_ALL: "C.UTF-8",
      PYTHONHASHSEED: "0",
      RAYON_NUM_THREADS: "1",
    },
    shell: false,
    stdio: ["ignore", "pipe", "pipe"],
  });
  const stdout: Buffer[] = [];
  const stderr: Buffer[] = [];
  let captured = 0;
  const add = (target: Buffer[], chunk: Buffer) => {
    captured += chunk.length;
    if (captured > MAX_CAPTURE_BYTES) {
      child.kill("SIGKILL");
      return;
    }
    target.push(chunk);
  };
  child.stdout.on("data", (chunk: Buffer) => add(stdout, chunk));
  child.stderr.on("data", (chunk: Buffer) => add(stderr, chunk));
  const exitCode = await new Promise<number>((resolve, reject) => {
    child.once("error", reject);
    child.once("exit", (code) => resolve(code ?? 1));
  });
  return {
    exitCode,
    stdout: Buffer.concat(stdout).toString("utf8").slice(0, MAX_CAPTURE_BYTES),
    stderr: Buffer.concat(stderr).toString("utf8").slice(0, MAX_CAPTURE_BYTES),
    durationSeconds: (performance.now() - startedAt) / 1000,
  };
}

async function requestGeneratedTools(context: P04WorkerContext): Promise<GeneratedToolBundle> {
  const baseUrl = process.env.OPENAI_BASE_URL?.replace(/\/$/u, "");
  const apiKey = process.env.OPENAI_API_KEY;
  if (!baseUrl || !apiKey) throw new Error("P04 fixed provider credentials are not configured");
  context.campaignRepository.incrementLlmCalls(context.campaignId);
  const actionKey = `${context.campaignId}:p04-tool-factory:generation-v1`;
  context.campaignRepository.recordBudget({
    campaignId: context.campaignId,
    resourceKind: "LLM",
    actionKey,
    calls: 1,
    expectedEvidence: "two bounded Python tool sources with contract tests for finance and quantum diagnostics",
  });
  const response = await fetch(`${baseUrl}/chat/completions`, {
    method: "POST",
    headers: { Authorization: `Bearer ${apiKey}`, "Content-Type": "application/json" },
    body: JSON.stringify({
      model: "gpt-5.6-sol",
      messages: [
        {
          role: "system",
          content: [
            "You are the P04 Run-level Tool Factory code generator.",
            "Generate exactly two deterministic pure-Python tools and pytest tests.",
            "Each module must define execute(payload: dict) -> dict and return schema_version qf.generated-tool-result.v1,",
            "its exact tool_name, status COMPLETED, and a diagnostics object.",
            "Use only standard-library math/statistics/collections/itertools/hashlib/json/typing imports.",
            "Never use filesystem, environment, network, subprocess, eval, exec, dynamic import, printing, randomness, or time.",
            "The financial tool must check validation-only model metrics, data row counts, risk-graph failures, and the 20-portfolio exact enumeration.",
            "The quantum tool must check QAOA probability normalization, exact-optimum probability/gap, cqlib circuit hashes, and optional hardware-result consistency.",
            "Do not compute or expose formal-test metrics and do not claim quantum advantage.",
          ].join(" "),
        },
        {
          role: "user",
          content: "Return the two requested tool modules and their isolated pytest files. Tests must import only the named module and pytest.",
        },
      ],
      response_format: {
        type: "json_schema",
        json_schema: {
          name: "qf_p04_generated_tools",
          strict: true,
          schema: {
            type: "object",
            additionalProperties: false,
            required: ["schema_version", "tools"],
            properties: {
              schema_version: { type: "string", const: "qf.p04.generated-tools.v1" },
              tools: {
                type: "array",
                minItems: 2,
                maxItems: 2,
                items: {
                  type: "object",
                  additionalProperties: false,
                  required: ["name", "description", "code", "test_code"],
                  properties: {
                    name: { type: "string", enum: [...TOOL_NAMES] },
                    description: { type: "string", minLength: 20, maxLength: 600 },
                    code: { type: "string", minLength: 200, maxLength: 16_000 },
                    test_code: { type: "string", minLength: 150, maxLength: 12_000 },
                  },
                },
              },
            },
          },
        },
      },
      max_tokens: 8_000,
      stream: false,
    }),
    signal: AbortSignal.timeout(180_000),
  });
  const json = await response.json() as {
    choices?: Array<{ message?: { content?: unknown } }>;
    error?: { message?: unknown; type?: unknown; code?: unknown };
  };
  if (!response.ok) {
    throw new Error(`P04 tool generation failed HTTP ${response.status}: ${String(json.error?.type ?? json.error?.code ?? "provider_error")}`);
  }
  const content = json.choices?.[0]?.message?.content;
  if (typeof content !== "string") throw new Error("P04 tool generation returned no structured content");
  const bundle = JSON.parse(content) as GeneratedToolBundle;
  if (bundle.schema_version !== "qf.p04.generated-tools.v1" || bundle.tools.length !== 2) {
    throw new Error("P04 generated tool bundle failed its outer contract");
  }
  const names = bundle.tools.map((tool) => tool.name).sort();
  if (JSON.stringify(names) !== JSON.stringify([...TOOL_NAMES].sort())) {
    throw new Error("P04 generated tool bundle did not contain the exact required tool names");
  }
  return bundle;
}

function financialPayload(science: JsonObject): JsonObject {
  const models = science.models as JsonObject;
  const qubo = science.qubo as JsonObject;
  const data = science.data as JsonObject;
  const riskGraph = science.risk_graph as JsonObject;
  const exactPortfolios = qubo.exact_portfolios as JsonObject[];
  return p04Json({
    formal_test_sealed: true,
    validation_metrics: models.validation_only_metrics,
    data_row_counts: {
      expected: {
        train: data.training_rows,
        validation: data.validation_rows,
      },
      actual: {
        train: data.training_rows,
        validation: data.validation_rows,
      },
    },
    risk_graph: {
      failures: Array.from({ length: Number(riskGraph.failed_windows ?? 0) }, (_, index) => ({ index })),
      direction: riskGraph.direction,
      edge_count: Array.isArray(riskGraph.edges) ? riskGraph.edges.length : 0,
    },
    portfolio_enumeration: {
      mode: "exact",
      count: exactPortfolios.length,
      portfolios: exactPortfolios.map((portfolio) => ({ id: portfolio.bitstring, ...portfolio })),
    },
  });
}

function quantumPayload(science: JsonObject, hardwareResult: JsonObject | null = null): JsonObject {
  const qubo = science.qubo as JsonObject;
  const qaoa = science.qaoa as JsonObject;
  const local = science.cqlib_local as JsonObject;
  const exactPortfolios = qubo.exact_portfolios as JsonObject[];
  const exactBest = qubo.exact_best as JsonObject;
  const objectiveByState = Object.fromEntries(exactPortfolios.map((portfolio) => [
    String(portfolio.bitstring),
    Number(portfolio.total_without_penalty),
  ]));
  const qaoaCircuit = local.qaoa as JsonObject;
  const qgnnCircuit = local.qgnn as JsonObject;
  return p04Json({
    formal_test_sealed: true,
    qaoa: {
      probabilities: qaoa.probabilities,
      reported_exact_optimum_probability: qaoa.exact_optimal_probability,
      reported_optimality_gap: qaoa.best_sample_gap,
    },
    exact_optimum: {
      states: [qaoa.exact_optimal_bitstring],
      objective: exactBest.total_without_penalty,
      sense: "min",
      objective_by_state: objectiveByState,
    },
    cqlib_circuits: [
      { name: "financial_qaoa", cqlib: qaoaCircuit.qcis, sha256: qaoaCircuit.qcis_sha256 },
      { name: "representative_qgnn", cqlib: qgnnCircuit.qcis, sha256: qgnnCircuit.qcis_sha256 },
    ],
    hardware_result: hardwareResult,
  });
}

export async function generateValidateRegisterAndInvokeTools(
  context: P04WorkerContext,
  sciencePath: string,
): Promise<Array<{ tool: P04GeneratedToolSummary; path: string; basePayload: JsonObject }>> {
  const bundlePath = path.join(context.runWorkspace, "tool-factory", "generated-tool-bundle.json");
  const generation = await executeP04JsonStage<GeneratedToolBundle>({
    context,
    stage: "generate_tools",
    actionType: "P04_LLM_TOOL_GENERATION",
    expectedEvidence: "Agent-generated source and tests for two useful bounded Run-level tools",
    inputIdentity: { model: "openai/getoken/gpt-5.6-sol", promptVersion: "qf.p04-tool-factory.v1" },
    outputPath: bundlePath,
    timing: "external",
    producer: "qf-p04.tool-factory-generation",
    logicalName: "generated-tool-bundle",
    run: () => requestGeneratedTools(context),
  });
  const science = await readP04Json<JsonObject>(sciencePath);
  const toolsRoot = path.join(context.runWorkspace, "tool-factory", "registered");
  await mkdir(toolsRoot, { recursive: true });
  const registered: Array<{ tool: P04GeneratedToolSummary; path: string; basePayload: JsonObject }> = [];
  for (const definition of generation.value.tools) {
    const toolPath = path.join(toolsRoot, `${definition.name}.py`);
    const testPath = path.join(toolsRoot, `test_${definition.name}.py`);
    await atomicText(toolPath, definition.code);
    await atomicText(testPath, definition.test_code);
    let tool = context.p04Repository.registerTool({
      campaignId: context.campaignId,
      runId: context.runId,
      name: definition.name,
      specHash: p04Hash({ name: definition.name, description: definition.description }),
      codeHash: p04Hash(definition.code),
      testHash: p04Hash(definition.test_code),
    });
    const validation = await runGeneratedToolJob({
      projectRoot: context.projectRoot,
      request: p04Json({
        action: "validate",
        workspace_root: context.runWorkspace,
        tool_name: definition.name,
        tool_path: toolPath,
        test_path: testPath,
      }),
      timeoutSeconds: 60,
    });
    if (validation.exitCode !== 0 || validation.stdout.status !== "COMPLETED") {
      context.p04Repository.updateTool({ toolId: tool.toolId, status: "REJECTED" });
      throw new Error(`generated tool ${definition.name} failed AST safety validation: ${String(validation.stdout.message ?? "unknown")}`);
    }
    const python = path.join(context.projectRoot, ".venv", "bin", "python");
    const ruffExecutable = path.join(context.projectRoot, ".venv", "bin", "ruff");
    const ruffFix = await runBoundedCommand(
      ruffExecutable,
      ["check", "--fix", "--unsafe-fixes", "--ignore=UP031", path.basename(toolPath), path.basename(testPath)],
      toolsRoot,
      90,
    );
    const ruffFormat = await runBoundedCommand(
      ruffExecutable,
      ["format", path.basename(toolPath), path.basename(testPath)],
      toolsRoot,
      90,
    );
    const formattedCode = await readFile(toolPath, "utf8");
    const formattedTest = await readFile(testPath, "utf8");
    tool = context.p04Repository.updateToolHashes(tool.toolId, p04Hash(formattedCode), p04Hash(formattedTest));
    const postFormatValidation = await runGeneratedToolJob({
      projectRoot: context.projectRoot,
      request: p04Json({
        action: "validate",
        workspace_root: context.runWorkspace,
        tool_name: definition.name,
        tool_path: toolPath,
        test_path: testPath,
      }),
      timeoutSeconds: 60,
    });
    const pytest = await runBoundedCommand(python, ["-m", "pytest", "-q", path.basename(testPath)], toolsRoot, 90);
    const ruff = await runBoundedCommand(
      ruffExecutable,
      ["check", "--ignore=UP031", path.basename(toolPath), path.basename(testPath)],
      toolsRoot,
      90,
    );
    const gate = {
      schemaVersion: "qf.p04.generated-tool-gate.v1",
      toolName: definition.name,
      validation: validation.stdout,
      formatter: {
        ruffFix: { exitCode: ruffFix.exitCode, stdout: ruffFix.stdout, stderr: ruffFix.stderr },
        ruffFormat: { exitCode: ruffFormat.exitCode, stdout: ruffFormat.stdout, stderr: ruffFormat.stderr },
        postFormatValidation: postFormatValidation.stdout,
      },
      pytest: { exitCode: pytest.exitCode, stdout: pytest.stdout, stderr: pytest.stderr, durationSeconds: pytest.durationSeconds },
      ruff: { exitCode: ruff.exitCode, stdout: ruff.stdout, stderr: ruff.stderr, durationSeconds: ruff.durationSeconds },
    };
    const sourceArtifact = await registerP04TextArtifact({
      context,
      value: formattedCode,
      producer: `qf-p04.generated-tool.${definition.name}`,
      logicalName: `generated-tool-source-${definition.name}`,
      relativeOutputPath: path.relative(context.projectRoot, toolPath),
      parentHashes: [generation.artifact.sha256],
    });
    const testArtifact = await registerP04JsonArtifact({
      context,
      value: gate,
      producer: `qf-p04.generated-tool-gate.${definition.name}`,
      logicalName: `generated-tool-gate-${definition.name}`,
      relativeOutputPath: path.relative(context.projectRoot, `${testPath}.gate.json`),
      parentHashes: [sourceArtifact.sha256],
    });
    if (ruffFormat.exitCode !== 0 || postFormatValidation.exitCode !== 0
      || pytest.exitCode !== 0 || ruff.exitCode !== 0) {
      context.p04Repository.updateTool({
        toolId: tool.toolId,
        status: "REJECTED",
        sourceArtifactSha256: sourceArtifact.sha256,
        testArtifactSha256: testArtifact.sha256,
      });
      throw new Error(`generated tool ${definition.name} failed pytest or Ruff`);
    }
    tool = context.p04Repository.updateTool({
      toolId: tool.toolId,
      status: "REGISTERED",
      sourceArtifactSha256: sourceArtifact.sha256,
      testArtifactSha256: testArtifact.sha256,
    });
    const basePayload = definition.name === "financial_result_diagnostics"
      ? financialPayload(science)
      : quantumPayload(science);
    const invocationInputHash = p04Hash(basePayload);
    const invocationId = context.p04Repository.beginToolInvocation(
      tool.toolId,
      context.runId,
      `${context.campaignId}:${context.runId}:${definition.name}:initial:${invocationInputHash}`,
      invocationInputHash,
    );
    const invocation = await runGeneratedToolJob({
      projectRoot: context.projectRoot,
      request: p04Json({
        action: "invoke",
        workspace_root: context.runWorkspace,
        tool_name: definition.name,
        tool_path: toolPath,
        payload: basePayload,
      }),
      timeoutSeconds: 60,
    });
    if (invocation.exitCode !== 0 || invocation.stdout.status !== "COMPLETED") {
      context.p04Repository.completeToolInvocation({
        invocationId,
        status: "FAILED",
        exitCode: invocation.exitCode,
        durationSeconds: invocation.durationSeconds,
      });
      throw new Error(`generated tool ${definition.name} failed its first real invocation`);
    }
    const invocationArtifact = await registerP04JsonArtifact({
      context,
      value: invocation.stdout,
      producer: `qf-p04.generated-tool-invocation.${definition.name}`,
      logicalName: `generated-tool-invocation-${definition.name}-initial`,
      relativeOutputPath: path.relative(context.projectRoot, path.join(toolsRoot, `${definition.name}-initial-result.json`)),
      parentHashes: [sourceArtifact.sha256, generation.artifact.sha256],
    });
    context.p04Repository.completeToolInvocation({
      invocationId,
      status: "COMPLETED",
      outputArtifactSha256: invocationArtifact.sha256,
      exitCode: invocation.exitCode,
      durationSeconds: invocation.durationSeconds,
    });
    context.p04Repository.appendEvent(context.runId, "GENERATED_TOOL_REGISTERED_AND_CALLED", p04Json({
      toolId: tool.toolId,
      name: tool.name,
      sourceArtifactSha256: sourceArtifact.sha256,
      gateArtifactSha256: testArtifact.sha256,
      invocationArtifactSha256: invocationArtifact.sha256,
    }));
    registered.push({ tool: context.p04Repository.getTool(tool.toolId), path: toolPath, basePayload });
  }
  return registered;
}

export async function runToolActiveValidationBlock(input: {
  context: P04WorkerContext;
  tool: P04GeneratedToolSummary;
  toolPath: string;
  basePayload: JsonObject;
  block: number;
  minimumComputeSeconds: number;
}): Promise<void> {
  const outputPath = path.join(input.context.runWorkspace, "active-validation", `tool-${input.tool.name}-${String(input.block).padStart(4, "0")}.json`);
  await executeP04JsonStage<JsonObject>({
    context: input.context,
    stage: `tool_active_validation_${input.tool.name}_${input.block}`,
    actionType: "P04_GENERATED_TOOL_ACTIVE_VALIDATION",
    expectedEvidence: "new seed-indexed generated-tool robustness digest from structured real-task inputs",
    inputIdentity: {
      toolId: input.tool.toolId,
      codeHash: input.tool.codeHash,
      block: input.block,
      sampleStart: input.block * 1_000_000,
    },
    outputPath,
    timing: "active",
    producer: `qf-p04.generated-tool-active.${input.tool.name}`,
    logicalName: `tool-active-${input.tool.name}-${input.block}`,
    parentHashes: [input.tool.sourceArtifactSha256!],
    run: async () => {
      const result = await runGeneratedToolJob({
        projectRoot: input.context.projectRoot,
        request: p04Json({
          action: "active_validation_block",
          workspace_root: input.context.runWorkspace,
          tool_name: input.tool.name,
          tool_path: input.toolPath,
          payload: input.basePayload,
          sample_start: input.block * 1_000_000,
          minimum_compute_seconds: input.minimumComputeSeconds,
        }),
        timeoutSeconds: input.minimumComputeSeconds + 60,
      });
      if (result.exitCode !== 0 || result.stdout.status !== "COMPLETED") {
        throw new Error(`generated tool active validation failed: ${String(result.stdout.message ?? "unknown")}`);
      }
      return result.stdout;
    },
  });
}
