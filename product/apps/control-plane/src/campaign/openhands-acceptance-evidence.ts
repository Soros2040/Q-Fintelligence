import type { JsonObject, JsonValue } from "@q-fintelligence/contracts";

export interface AcceptanceConversationEvent {
  sequence: number | null;
  type: string;
  payload: JsonObject;
  createdAt: string;
}

export interface AcceptanceArtifactEvidence {
  sha256: string;
  mediaType: string;
  producer: string;
  parentHashes: string[];
  content: JsonValue | null;
}

interface CompletedTool {
  toolCallId: string;
  toolName: string;
  startedSequence: number;
  completedSequence: number;
  arguments: JsonObject;
  result: JsonValue;
}

function object(value: JsonValue | undefined): JsonObject | null {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? value : null;
}

function parseResult(value: JsonValue | undefined): JsonValue {
  if (typeof value !== "string") return value ?? null;
  try {
    return JSON.parse(value) as JsonValue;
  } catch {
    return value;
  }
}

function collectKeyValues(value: JsonValue | null, keyNames: Set<string>, values: JsonValue[] = []): JsonValue[] {
  if (Array.isArray(value)) {
    for (const item of value) collectKeyValues(item, keyNames, values);
    return values;
  }
  const record = object(value ?? undefined);
  if (!record) return values;
  for (const [key, item] of Object.entries(record)) {
    if (keyNames.has(key.toLowerCase())) values.push(item);
    collectKeyValues(item, keyNames, values);
  }
  return values;
}

function containsNumericField(value: JsonValue | null, keys: string[], expected: number): boolean {
  return collectKeyValues(value, new Set(keys.map((key) => key.toLowerCase())))
    .some((item) => Number(item) === expected);
}

function containsNonEmptyField(value: JsonValue | null, keys: string[]): boolean {
  return collectKeyValues(value, new Set(keys.map((key) => key.toLowerCase()))).some((item) => {
    if (typeof item === "string") return item.trim().length > 0;
    if (Array.isArray(item)) return item.length > 0;
    return typeof item === "object" && item !== null;
  });
}

function extractHashes(value: JsonValue | null, hashes = new Set<string>()): Set<string> {
  if (typeof value === "string") {
    for (const match of value.matchAll(/\b[a-f0-9]{64}\b/gu)) hashes.add(match[0]);
    return hashes;
  }
  if (Array.isArray(value)) {
    for (const item of value) extractHashes(item, hashes);
    return hashes;
  }
  const record = object(value ?? undefined);
  if (record) for (const item of Object.values(record)) extractHashes(item, hashes);
  return hashes;
}

function toolPairs(events: AcceptanceConversationEvent[]): { completed: CompletedTool[]; valid: boolean } {
  const starts = new Map<string, { toolName: string; sequence: number; arguments: JsonObject }>();
  const completed: CompletedTool[] = [];
  let valid = true;
  for (const event of events) {
    if (event.sequence === null) continue;
    const toolCallId = typeof event.payload.toolCallId === "string" ? event.payload.toolCallId : "";
    const toolName = typeof event.payload.toolName === "string" ? event.payload.toolName : "";
    if (event.type === "tool.started") {
      const argumentsValue = object(event.payload.arguments) ?? {};
      if (!toolCallId || !toolName || starts.has(toolCallId)) valid = false;
      else starts.set(toolCallId, { toolName, sequence: event.sequence, arguments: argumentsValue });
    }
    if (event.type === "tool.completed" && event.payload.isError !== true) {
      const started = starts.get(toolCallId);
      if (!started || started.toolName !== toolName || started.sequence >= event.sequence) {
        valid = false;
        continue;
      }
      completed.push({
        toolCallId,
        toolName,
        startedSequence: started.sequence,
        completedSequence: event.sequence,
        arguments: started.arguments,
        result: parseResult(event.payload.result),
      });
    }
  }
  return { completed: completed.toSorted((left, right) => left.completedSequence - right.completedSequence), valid };
}

function findToolAfter(tools: CompletedTool[], names: string[], afterSequence = 0): CompletedTool | null {
  return tools.find((tool) => names.includes(tool.toolName) && tool.completedSequence > afterSequence) ?? null;
}

function artifactsForTool(tool: CompletedTool | null, artifacts: AcceptanceArtifactEvidence[]): AcceptanceArtifactEvidence[] {
  if (!tool) return [];
  const hashes = extractHashes(tool.result);
  return artifacts.filter((artifact) => hashes.has(artifact.sha256));
}

function generatedClassicalStructure(artifact: AcceptanceArtifactEvidence): boolean {
  const content = object(artifact.content ?? undefined);
  if (!content) return false;
  const diagnostics = object(content.diagnostics);
  return content.schema_version === "qf.generated-tool-result.v1"
    && content.tool_name === "portfolio_qubo_pipeline"
    && content.status === "COMPLETED"
    && diagnostics !== null
    && containsNumericField(content, ["n", "asset_count", "num_assets", "qubits"], 6)
    && containsNumericField(content, ["k", "selection_count", "cardinality", "selected_assets"], 3)
    && (containsNumericField(content, ["combination_count", "search_space_size", "enumerated_combinations"], 20)
      || collectKeyValues(content, new Set(["combinations"])).some((value) => Array.isArray(value) && value.length === 20))
    && containsNonEmptyField(content, ["qubo", "qubo_matrix", "qubo_coefficients"])
    && containsNonEmptyField(content, ["exact_enumeration", "classical_optimum", "best_bitstring"]);
}

function controlledCircuitStructure(artifact: AcceptanceArtifactEvidence): boolean {
  const content = object(artifact.content ?? undefined);
  if (!content) return false;
  const manifest = object(content.manifest);
  const hasPositiveDepth = collectKeyValues(content, new Set(["depth"])).some((value) => (
    typeof value === "number" && Number.isFinite(value) && value > 0
  ));
  const legacyStructure = containsNumericField(content, ["qubits", "n", "logical_qubits"], 6)
    && containsNumericField(content, ["k", "selection_count", "cardinality"], 3)
    && containsNonEmptyField(content, ["qcis"])
    && containsNonEmptyField(content, ["local_simulation", "statevector", "probabilities", "best_probability"])
    && containsNonEmptyField(content, ["seed"])
    && hasPositiveDepth;
  const controlledP06Structure = content.schema_version === "qf.p06.controlled-qaoa.v1"
    && content.status === "COMPLETED"
    && manifest !== null
    && manifest.backend === "tianyan176"
    && containsNumericField(manifest, ["qubits"], 6)
    && containsNumericField(manifest, ["selected_count"], 3)
    && containsNonEmptyField(content, ["qcis"])
    && containsNonEmptyField(manifest, ["local_preflight"])
    && containsNonEmptyField(manifest, ["warm_start"])
    && hasPositiveDepth;
  return legacyStructure || controlledP06Structure;
}

function mappedCircuitStructure(artifact: AcceptanceArtifactEvidence): boolean {
  const content = object(artifact.content ?? undefined);
  if (!content) return false;
  const physicalQubits = collectKeyValues(content, new Set(["physical_qubits", "physicalqubits"]));
  const compatibility = collectKeyValues(content, new Set(["hardware_compatibility", "hardwarecompatibility"]));
  return physicalQubits.some((value) => Array.isArray(value) && value.length === 6)
    && compatibility.some((value) => value === "PASS")
    && containsNonEmptyField(content, ["qcis"])
    && containsNonEmptyField(content, ["mapping"])
    && containsNonEmptyField(content, ["local_simulation"]);
}

export function analyzeAcceptanceEvidence(input: {
  events: AcceptanceConversationEvent[];
  artifacts: AcceptanceArtifactEvidence[];
  baselineEventSequence: number;
  hardwareStable: boolean;
  historyStable: boolean;
  formalTestSealed: boolean;
}): JsonObject {
  const events = input.events
    .filter((event) => event.sequence !== null && event.sequence > input.baselineEventSequence)
    .toSorted((left, right) => Number(left.sequence) - Number(right.sequence));
  const pairs = toolPairs(events);
  const source = findToolAfter(pairs.completed, ["list_project_sources"]);
  const fetched = findToolAfter(pairs.completed, ["fetch_tushare_six_stock_bundle"], source?.completedSequence ?? 0);
  const inspected = findToolAfter(
    pairs.completed,
    ["inspect_validation_dataset"],
    Math.max(source?.completedSequence ?? 0, fetched?.completedSequence ?? 0),
  );
  const classical = findToolAfter(pairs.completed, ["execute_controlled_python"], inspected?.completedSequence ?? 0);
  const controlled = findToolAfter(pairs.completed, ["generate_controlled_qaoa_circuit"], classical?.completedSequence ?? 0);
  const calibration = findToolAfter(pairs.completed, ["fetch_tianyan176_calibration_snapshot"], controlled?.completedSequence ?? 0);
  const mapped = findToolAfter(
    pairs.completed,
    ["generate_noise_aware_qaoa_circuit", "transpile_tianyan176_qcis_artifacts", "validate_tianyan176_qcis_artifacts"],
    calibration?.completedSequence ?? 0,
  );
  const hardwareStatus = findToolAfter(pairs.completed, ["get_p16_hardware_status"], mapped?.completedSequence ?? 0);
  const analysis = findToolAfter(pairs.completed, ["record_analysis"], hardwareStatus?.completedSequence ?? 0);
  const stageArtifacts = {
    A: artifactsForTool(inspected, input.artifacts),
    B: artifactsForTool(classical, input.artifacts),
    C: artifactsForTool(controlled, input.artifacts),
    D: [...artifactsForTool(calibration, input.artifacts), ...artifactsForTool(mapped, input.artifacts)],
    F: artifactsForTool(analysis, input.artifacts),
  };
  const datasetStructured = stageArtifacts.A.some((artifact) => artifact.mediaType === "application/vnd.qf.data-quality+json"
    && containsNonEmptyField(artifact.content, ["dataset_manifest", "source_artifact_sha256", "quality_gate"]));
  const classicalStructured = stageArtifacts.B.some(generatedClassicalStructure);
  const controlledStructured = stageArtifacts.C.some(controlledCircuitStructure);
  const calibrationStructured = stageArtifacts.D.some((artifact) => artifact.mediaType.includes("machine-calibration"));
  const mappedStructured = stageArtifacts.D.some(mappedCircuitStructure);
  const noMutationTools = !pairs.completed.some((tool) => [
    "prepare_p16_hardware_batch",
    "submit_p16_hardware_batch",
    "query_p16_hardware_batch",
    "submit_tianyan176_100_shot",
    "start_p15_portfolio_campaign",
  ].includes(tool.toolName));
  const analysisParents = new Set(stageArtifacts.F.flatMap((artifact) => artifact.parentHashes));
  const precedingArtifactGroups = [stageArtifacts.A, stageArtifacts.B, stageArtifacts.C, stageArtifacts.D];
  const finalLineageComplete = precedingArtifactGroups.every((group) => group.length > 0
    && group.some((artifact) => analysisParents.has(artifact.sha256)));
  const bInputHashes = classical ? extractHashes(classical.arguments) : new Set<string>();
  const cInputHashes = controlled ? extractHashes(controlled.arguments) : new Set<string>();
  const dInputHashes = mapped ? extractHashes(mapped.arguments) : new Set<string>();
  const artifactDagValid = inspected !== null
    && classical !== null
    && controlled !== null
    && calibration !== null
    && mapped !== null
    && analysis !== null
    && (stageArtifacts.A.some((artifact) => bInputHashes.has(artifact.sha256)) || bInputHashes.size > 0)
    && (stageArtifacts.B.some((artifact) => cInputHashes.has(artifact.sha256))
      || stageArtifacts.A.some((artifact) => cInputHashes.has(artifact.sha256)))
    && stageArtifacts.D.some((artifact) => dInputHashes.has(artifact.sha256) || artifact.mediaType.includes("machine-calibration"))
    && finalLineageComplete;
  const sequences = [source, inspected, classical, controlled, calibration, mapped, hardwareStatus, analysis]
    .map((tool) => tool?.completedSequence ?? 0);
  const toolOrderValid = sequences.every((sequence, index) => sequence > 0 && (index === 0 || sequence > sequences[index - 1]!));
  const negativeText = analysis ? JSON.stringify({ arguments: analysis.arguments, result: analysis.result }) : "";
  const scientificVerdict = /(?:negative|阴性|不支持.{0,12}量子优势|劣于经典)/iu.test(negativeText)
    ? "NEGATIVE"
    : "INCONCLUSIVE";
  return {
    schemaVersion: "qf.openhands-acceptance-scientific-evidence.v2",
    baselineEventSequence: input.baselineEventSequence,
    postBaselineOnly: events.every((event) => Number(event.sequence) > input.baselineEventSequence),
    postBaselineEventCount: events.length,
    toolPairsValid: pairs.valid,
    toolOrderValid,
    artifactDagValid,
    structuredEvidenceValid: datasetStructured && classicalStructured && controlledStructured
      && calibrationStructured && mappedStructured && finalLineageComplete,
    readOnlyHardwareBoundary: input.hardwareStable && noMutationTools,
    historyStable: input.historyStable,
    formalTestSealed: input.formalTestSealed,
    scientificVerdict,
    stages: {
      A: { complete: source !== null && inspected !== null && datasetStructured, sequence: inspected?.completedSequence ?? null },
      B: { complete: classical !== null && classicalStructured, sequence: classical?.completedSequence ?? null },
      C: { complete: controlled !== null && controlledStructured, sequence: controlled?.completedSequence ?? null },
      D: { complete: calibration !== null && mapped !== null && calibrationStructured && mappedStructured, sequence: mapped?.completedSequence ?? null },
      E: { complete: hardwareStatus !== null && input.hardwareStable && noMutationTools, sequence: hardwareStatus?.completedSequence ?? null, verdict: "NOT_AUTHORIZED" },
      F: { complete: analysis !== null && finalLineageComplete, sequence: analysis?.completedSequence ?? null },
    },
    toolTimeline: pairs.completed.map((tool) => ({
      toolCallId: tool.toolCallId,
      toolName: tool.toolName,
      startedSequence: tool.startedSequence,
      completedSequence: tool.completedSequence,
    })),
    evidenceArtifacts: Object.fromEntries(Object.entries(stageArtifacts).map(([stage, values]) => [
      stage,
      values.map((artifact) => artifact.sha256),
    ])),
  };
}
