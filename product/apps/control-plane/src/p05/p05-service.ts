import { createHash, randomUUID } from "node:crypto";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import path from "node:path";

import type {
  AgentUiEvent,
  JsonObject,
  P05LaunchCommand,
  P05TaskDetail,
  P05UploadCommand,
  P05UploadSummary,
  ProjectSourceCommand,
  ProjectSourceSummary,
} from "@q-fintelligence/contracts";

import { storeArtifact } from "../artifact-store.js";
import { runP05UploadParser } from "../campaign/python-runner.js";
import type { WorkspaceRepository } from "../db/repository.js";
import {
  assertHardwareMutationAuthorized,
  READ_ONLY_HARDWARE_POLICY,
  type HardwareExecutionPolicy,
} from "../hardware-policy.js";
import { P05Orchestrator } from "./p05-orchestrator.js";
import type { P05Repository } from "./p05-repository.js";

const MAX_UPLOAD_BYTES = 16 * 1024 * 1024;
const REQUIRED_EXTENSIONS = [
  ".csv",
  ".tsv",
  ".xlsx",
  ".json",
  ".jsonl",
  ".parquet",
  ".npy",
  ".npz",
  ".hdf5",
  ".txt",
  ".md",
  ".pdf",
  ".qasm",
  ".qcis",
] as const;

const VENDOR_CANDIDATES = [
  {
    name: "OpenQAOA",
    repositoryUrl: "https://github.com/entropicalabs/openqaoa",
    commitSha: "c6e7dd759392fcca180bab9bd29c5f5e7dab3662",
    licenseSpdx: "Apache-2.0",
    decision: "AUDIT_ONLY" as const,
    evidence: {
      domain: "QAOA SDK",
      python311: "requires isolated dependency verification",
      qcis: "no native tianyan176/QCIS contract proven",
      security: "external backend plugins expand network and dependency surface",
      benchmark: "upstream SDK examples and test suite; not run in project environment",
      reason: "retain cqlib 1.3.11 as the production hardware adapter",
    },
  },
  {
    name: "Qiskit Optimization",
    repositoryUrl: "https://github.com/qiskit-community/qiskit-optimization",
    commitSha: "65ce191a4c3ed48f203d8dd6eea476d0deea9e71",
    licenseSpdx: "Apache-2.0",
    decision: "AUDIT_ONLY" as const,
    evidence: {
      domain: "QUBO and QAOA optimization",
      dependency: "Qiskit 2.x stack would materially expand the locked cqlib environment",
      maintenance: "community repository states that IBM official support ended",
      security: "no direct TianYan credentials granted",
      benchmark: "upstream Max-Cut/QAOA examples and tests",
      reason: "use concepts only; avoid conflicting runtime stack in P05",
    },
  },
  {
    name: "Mitiq",
    repositoryUrl: "https://github.com/unitaryfoundation/mitiq",
    commitSha: "7f43ec140aad2e1209f802ab725fc2d9e6778904",
    licenseSpdx: "GPL-3.0",
    decision: "AUDIT_ONLY" as const,
    evidence: {
      domain: "error mitigation",
      dependency: "multiple optional quantum frontends; no QCIS-native path proven",
      security: "must not receive hardware keys or network capability",
      benchmark: "upstream unit tests and mitigation examples",
      reason: "license and backend integration require separate review",
    },
  },
  {
    name: "LLM Guard",
    repositoryUrl: "https://github.com/protectai/llm-guard",
    commitSha: "168c1034ffdb33837e7ae6fd6a16b80567c1be03",
    licenseSpdx: "MIT",
    decision: "AUDIT_ONLY" as const,
    evidence: {
      domain: "prompt injection and secret scanning",
      dependency: "transformer/model assets and optional automatic installs are too broad for this task",
      security: "scanner output is advisory and cannot grant capabilities",
      benchmark: "upstream scanner tests",
      reason: "project requires deterministic file metadata/hidden-sheet/encoding routing without model download",
    },
  },
  {
    name: "Great Expectations",
    repositoryUrl: "https://github.com/great-expectations/great_expectations",
    commitSha: "18d0fd4b25af7dcf49596adea2f682a08e85b530",
    licenseSpdx: "Apache-2.0",
    decision: "AUDIT_ONLY" as const,
    evidence: {
      domain: "data quality",
      python311: true,
      dependency: "large Data Context and integration surface",
      security: "no need for cloud data sources or network",
      benchmark: "upstream expectation suite",
      reason: "Pandera provides the bounded dataframe validation needed by P05",
    },
  },
  {
    name: "RestrictedPython",
    repositoryUrl: "https://github.com/zopefoundation/RestrictedPython",
    commitSha: "61f184969384c4141913176104a36e920c5fd222",
    licenseSpdx: "ZPL-2.1",
    decision: "REJECT" as const,
    evidence: {
      domain: "controlled Python execution",
      security: "not an OS sandbox; public issue 284 and historical security advisories require defense in depth",
      benchmark: "upstream tox suite",
      reason: "existing AST allowlist plus prlimit, isolated environment, no secrets and subprocess/network monkeypatch is stricter",
    },
  },
  {
    name: "MCP Python SDK",
    repositoryUrl: "https://github.com/modelcontextprotocol/python-sdk",
    commitSha: "3a6f2996cdd8358957479791e8b26198c07d6a75",
    licenseSpdx: "MIT",
    decision: "AUDIT_ONLY" as const,
    evidence: {
      domain: "scientific tool protocol",
      release: "v1 maintenance; v2 alpha at audit time",
      security: "remote transports and OAuth unnecessary for local deterministic worker boundary",
      benchmark: "official conformance and test suite",
      reason: "versioned JSON worker protocol already satisfies the bounded P05 tool path",
    },
  },
  {
    name: "Pandera",
    repositoryUrl: "https://github.com/unionai-oss/pandera",
    commitSha: "0d685d24315540be6cee97155c9726539d562e44",
    licenseSpdx: "MIT",
    decision: "ACCEPT" as const,
    evidence: {
      domain: "dataframe schema and data quality",
      pinnedPackage: "pandera[pandas]==0.32.1",
      pythonRequires: ">=3.10",
      licenseSha256: "142d0a4e2b2ade63e1d3f85d8289865f729397aafeec9afced01877826c54640",
      pyprojectSha256: "7e1114c6b5b3981a09249ec5387988b94c5bad4d78f6a17d0ac75416536869ac",
      security: "runs without network, secrets, subprocess or filesystem writes in the upload parser",
      benchmark: "project fixture validation plus upstream CI/ASV evidence",
      reason: "bounded third-party validation integrated into every tabular/structured upload",
    },
  },
] satisfies Array<{
  name: string;
  repositoryUrl: string;
  commitSha: string;
  licenseSpdx: string;
  decision: "ACCEPT" | "REJECT" | "AUDIT_ONLY";
  evidence: JsonObject;
}>;

function sha256(data: Buffer): string {
  return createHash("sha256").update(data).digest("hex");
}

function safeName(fileName: string): string {
  const base = path.basename(fileName).normalize("NFKC");
  const replaced = base.replace(/[^A-Za-z0-9._\-\u4e00-\u9fff]/gu, "_");
  if (!replaced || replaced === "." || replaced === "..") throw new Error("upload file name is invalid");
  return replaced.slice(0, 180);
}

function objectiveIsSafe(objective: string): boolean {
  return !/(ignore|override|replace).{0,40}(system|developer|instruction)|tianyan_sw|unseal.{0,30}test|reveal.{0,30}(key|token|secret)/isu.test(
    objective.normalize("NFKC"),
  );
}

export class P05Service {
  readonly orchestrator: P05Orchestrator;

  constructor(
    private readonly projectRoot: string,
    private readonly artifactRoot: string,
    private readonly repository: WorkspaceRepository,
    private readonly p05Repository: P05Repository,
    private readonly publish: (event: AgentUiEvent) => void,
    private readonly hardwarePolicy: HardwareExecutionPolicy = READ_ONLY_HARDWARE_POLICY,
  ) {
    this.orchestrator = new P05Orchestrator(
      projectRoot,
      repository,
      p05Repository,
      this.publish,
    );
    for (const candidate of VENDOR_CANDIDATES) {
      this.p05Repository.registerVendorCandidate(candidate);
    }
  }

  private async prepareUpload(input: {
    fileName: string;
    declaredMediaType: string;
    bytesBase64: string;
    rootName: string;
    scopeId: string;
  }) {
    const fileName = safeName(input.fileName);
    const extension = path.extname(fileName).toLocaleLowerCase();
    const decoded = Buffer.from(input.bytesBase64, "base64");
    if (decoded.length === 0 || decoded.length > MAX_UPLOAD_BYTES) {
      throw new Error("P05 upload must be between 1 byte and 16 MiB");
    }
    const canonical = decoded.toString("base64").replace(/=+$/u, "");
    if (canonical !== input.bytesBase64.replace(/\s+/gu, "").replace(/=+$/u, "")) {
      throw new Error("P05 upload base64 encoding is invalid");
    }
    const digest = sha256(decoded);
    const uploadId = `upload_${randomUUID()}`;
    const root = path.join(this.projectRoot, ".local", input.rootName);
    const destination = path.join(root, input.scopeId, uploadId, fileName);
    const resolvedRoot = path.resolve(root);
    const resolvedDestination = path.resolve(destination);
    if (!resolvedDestination.startsWith(`${resolvedRoot}${path.sep}`)) {
      throw new Error("P05 upload destination escaped its workspace");
    }
    await mkdir(path.dirname(destination), { recursive: true });
    const temporary = `${destination}.${process.pid}.tmp`;
    await writeFile(temporary, decoded, { mode: 0o600 });
    await rename(temporary, destination);
    const parsed = await runP05UploadParser({
      projectRoot: this.projectRoot,
      request: {
        action: "parse",
        workspace_root: root,
        path: destination,
        file_name: fileName,
        declared_media_type: input.declaredMediaType,
        sha256: digest,
      },
    });
    if (parsed.exitCode !== 0 || parsed.stdout.status === "FAILED") {
      throw new Error(`P05 upload parse failed: ${String(parsed.stdout.message ?? "unknown")}`);
    }
    const security = parsed.stdout.security as JsonObject;
    return {
      fileName,
      extension,
      decoded,
      digest,
      relativePath: path.relative(this.projectRoot, destination),
      parsed,
      security,
      riskLevel: String(security.risk_level) as "LOW" | "MEDIUM" | "HIGH",
    };
  }

  async upload(command: P05UploadCommand): Promise<P05UploadSummary> {
    this.repository.assertConversationWritable(command.conversationId);
    const prepared = await this.prepareUpload({
      fileName: command.fileName,
      declaredMediaType: command.declaredMediaType,
      bytesBase64: command.bytesBase64,
      rootName: path.join("p05", "uploads"),
      scopeId: command.conversationId,
    });
    const { fileName, extension, decoded, digest, parsed, security, riskLevel } = prepared;
    const upload = this.p05Repository.addUpload({
      conversationId: command.conversationId,
      fileName,
      extension,
      declaredMediaType: command.declaredMediaType || "application/octet-stream",
      detectedMediaType: String(parsed.stdout.detected_media_type),
      byteSize: decoded.length,
      sha256: digest,
      relativePath: prepared.relativePath,
      parser: String(parsed.stdout.parser),
      riskLevel,
      quarantined: security.quarantined === true,
      parseResult: parsed.stdout,
    });
    let parsedArtifactSha256: string | null = null;
    if (!upload.quarantined) {
      const parsedArtifact = await storeArtifact({
        root: this.artifactRoot,
        data: new TextEncoder().encode(`${JSON.stringify({
          schemaVersion: "qf.p06.validation-upload.v1",
          uploadId: upload.uploadId,
          fileName: upload.fileName,
          rawSha256: upload.sha256,
          parsed: parsed.stdout,
          trust: "untrusted-data-only",
          formalTestSealed: true,
        }, null, 2)}\n`),
        mediaType: "application/vnd.qf.validation-data+json",
        producer: "qf-upload-parser.p06-compatible",
        parentHashes: [upload.sha256],
      });
      parsedArtifactSha256 = this.repository.registerArtifact(
        command.conversationId,
        parsedArtifact,
      ).sha256;
    }
    const event = this.repository.appendEvent({
      conversationId: command.conversationId,
      type: security.quarantined === true ? "security.quarantined" : "artifact.created",
      payload: {
        summary: security.quarantined === true
          ? `${fileName} 检出间接提示词注入并已隔离`
          : `${fileName} 已作为不可信数据安全解析`,
        uploadId: upload.uploadId,
        fileName,
        sha256: digest,
        artifactSha256: parsedArtifactSha256,
        artifactHashes: parsedArtifactSha256 === null ? [] : [parsedArtifactSha256],
        resultKind: upload.quarantined ? "warning" : "data_table",
        parser: upload.parser,
        detectedMediaType: upload.detectedMediaType,
        riskLevel,
        quarantined: upload.quarantined,
        stdout: parsed.stdout,
        stderr: parsed.stderr,
        process: {
          module: "qf_finance_worker.upload_parser",
          exitCode: parsed.exitCode,
          durationSeconds: parsed.durationSeconds,
        },
        systemPromptMutated: false,
        capabilitiesGranted: [],
      },
    });
    this.publish(event);
    return upload;
  }

  async uploadProjectSource(
    projectId: string,
    command: ProjectSourceCommand,
  ): Promise<ProjectSourceSummary> {
    this.repository.getProject(projectId);
    if (command.conversationId) {
      const conversation = this.repository.assertConversationWritable(command.conversationId);
      if (conversation.projectId !== projectId) {
        throw new Error("project source conversation must belong to the selected project");
      }
    }
    const prepared = await this.prepareUpload({
      fileName: command.fileName,
      declaredMediaType: command.declaredMediaType,
      bytesBase64: command.bytesBase64,
      rootName: "project-sources",
      scopeId: projectId,
    });
    const { fileName, extension, decoded, digest, parsed, security, riskLevel } = prepared;
    const source = this.p05Repository.addProjectSource({
      projectId,
      ...(command.conversationId ? { conversationId: command.conversationId } : {}),
      fileName,
      extension,
      declaredMediaType: command.declaredMediaType || "application/octet-stream",
      detectedMediaType: String(parsed.stdout.detected_media_type),
      byteSize: decoded.length,
      sha256: digest,
      relativePath: prepared.relativePath,
      parser: String(parsed.stdout.parser),
      riskLevel,
      quarantined: security.quarantined === true,
      parseResult: parsed.stdout,
    });
    if (command.conversationId) {
      let parsedArtifactSha256: string | null = null;
      if (!source.quarantined) {
        const parsedArtifact = await storeArtifact({
          root: this.artifactRoot,
          data: new TextEncoder().encode(`${JSON.stringify({
            schemaVersion: "qf.project-source.v1",
            sourceId: source.sourceId,
            projectId,
            fileName: source.fileName,
            rawSha256: source.sha256,
            parsed: parsed.stdout,
            trust: "untrusted-data-only",
            formalTestSealed: true,
          }, null, 2)}\n`),
          mediaType: "application/vnd.qf.project-source+json",
          producer: "qf-project-source-parser",
          parentHashes: [source.sha256],
        });
        parsedArtifactSha256 = this.repository.registerArtifact(
          command.conversationId,
          parsedArtifact,
        ).sha256;
      }
      const event = this.repository.appendEvent({
        conversationId: command.conversationId,
        type: source.quarantined ? "security.quarantined" : "artifact.created",
        payload: {
          summary: source.quarantined
            ? `${fileName} 已加入项目来源，但因间接提示词注入被隔离`
            : `${fileName} 已安全解析并加入项目来源`,
          sourceId: source.sourceId,
          projectId,
          fileName,
          sha256: digest,
          artifactSha256: parsedArtifactSha256,
          artifactHashes: parsedArtifactSha256 === null ? [] : [parsedArtifactSha256],
          resultKind: source.quarantined ? "warning" : "data_table",
          parser: source.parser,
          detectedMediaType: source.detectedMediaType,
          riskLevel,
          quarantined: source.quarantined,
          stdout: parsed.stdout,
          stderr: parsed.stderr,
          process: {
            module: "qf_finance_worker.upload_parser",
            exitCode: parsed.exitCode,
            durationSeconds: parsed.durationSeconds,
          },
          systemPromptMutated: false,
          capabilitiesGranted: [],
        },
      });
      this.publish(event);
    }
    return source;
  }

  listProjectSources(projectId: string): ProjectSourceSummary[] {
    this.repository.getProject(projectId);
    return this.p05Repository.listProjectSources(projectId);
  }

  archiveProjectSource(projectId: string, sourceId: string): void {
    this.repository.getProject(projectId);
    const source = this.p05Repository.getProjectSource(projectId, sourceId);
    if (source.addedFromConversationId) {
      this.repository.assertConversationWritable(source.addedFromConversationId);
    }
    this.p05Repository.archiveProjectSource(projectId, sourceId);
  }

  private async deadline(): Promise<{ task_started_at: string; hard_deadline_at: string }> {
    const file = path.join(this.projectRoot, ".runtime", "p05_execution_deadline.json");
    const value = JSON.parse(await readFile(file, "utf8")) as {
      task_started_at?: unknown;
      hard_deadline_at?: unknown;
    };
    if (typeof value.task_started_at !== "string" || typeof value.hard_deadline_at !== "string") {
      throw new Error("P05 persisted deadline file is invalid");
    }
    return {
      task_started_at: value.task_started_at,
      hard_deadline_at: value.hard_deadline_at,
    };
  }

  async launch(command: P05LaunchCommand): Promise<P05TaskDetail> {
    const conversation = this.repository.assertConversationWritable(command.conversationId);
    assertHardwareMutationAuthorized(this.hardwarePolicy, {
      operation: "P05 Campaign launch",
      jobCount: 50,
      shotsPerJob: 100,
      target: "tianyan176",
    });
    if (
      conversation.mode !== "OPENHANDS"
      || conversation.provider !== "openai"
      || conversation.modelId !== "gpt-5.6"
    ) {
      throw new Error("P05 requires an OpenHands conversation locked to openai/getoken/gpt-5.6");
    }
    if (!objectiveIsSafe(command.objective)) {
      throw new Error("P05 objective was quarantined by the independent instruction route");
    }
    const uploads = this.p05Repository.listUploads(command.conversationId);
    const safeExtensions = new Set(
      uploads.filter((upload) => !upload.quarantined).map((upload) => upload.extension),
    );
    const missing = REQUIRED_EXTENSIONS.filter((extension) => {
      if (extension === ".hdf5") return !safeExtensions.has(".hdf5") && !safeExtensions.has(".h5");
      return !safeExtensions.has(extension);
    });
    if (missing.length > 0) {
      throw new Error(`P05 launch requires safe browser uploads for: ${missing.join(", ")}`);
    }
    if (!uploads.some((upload) => upload.quarantined && upload.riskLevel === "HIGH")) {
      throw new Error("P05 launch requires a real quarantined prompt-injection fixture");
    }
    const timing = await this.deadline();
    if (Date.now() >= Date.parse(timing.hard_deadline_at)) {
      throw new Error("P05 six-hour hard deadline has already elapsed");
    }
    const p05TaskId = this.p05Repository.createTask({
      conversationId: command.conversationId,
      taskId: conversation.taskId,
      objective: command.objective,
      taskStartedAt: timing.task_started_at,
      hardDeadlineAt: timing.hard_deadline_at,
    });
    this.orchestrator.start(p05TaskId);
    return this.p05Repository.getTask(p05TaskId);
  }

  getTask(p05TaskId: string): P05TaskDetail {
    return this.p05Repository.getTask(p05TaskId);
  }

  listTasks(): P05TaskDetail[] {
    return this.p05Repository.listTasks();
  }

  listUploads(conversationId: string): P05UploadSummary[] {
    this.repository.getConversation(conversationId);
    return this.p05Repository.listUploads(conversationId);
  }

  resume(p05TaskId: string): P05TaskDetail {
    const task = this.p05Repository.getTask(p05TaskId);
    this.repository.assertConversationWritable(task.conversationId);
    assertHardwareMutationAuthorized(this.hardwarePolicy, {
      operation: "P05 Campaign resume",
      jobCount: 50,
      shotsPerJob: 100,
      target: "tianyan176",
    });
    if (Date.now() >= Date.parse(task.hardDeadlineAt)) {
      this.p05Repository.updateTask({
        p05TaskId,
        status: "TIME_BUDGET_EXHAUSTED",
        stage: "deadline",
      });
      return this.p05Repository.getTask(p05TaskId);
    }
    if (task.status === "COMPLETED") return task;
    this.p05Repository.updateTask({
      p05TaskId,
      status: "RUNNING",
      stage: "resuming_from_checkpoint",
      error: null,
    });
    this.orchestrator.start(p05TaskId);
    return this.p05Repository.getTask(p05TaskId);
  }
}
