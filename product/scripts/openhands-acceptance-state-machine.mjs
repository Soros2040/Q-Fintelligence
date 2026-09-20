export function plannedFaultProcessAction(fault, registered) {
  if (!fault || typeof fault !== "object" || fault.state !== "PLANNED"
    || !Number.isSafeInteger(fault.targetProcessId) || fault.targetProcessId <= 1
    || typeof fault.targetProcessKey !== "string" || !fault.targetProcessKey) {
    throw new Error("planned fault identity is invalid");
  }
  if (registered === undefined || registered === null) return "CONFIRM_PLANNED_TARGET_DEAD";
  if (typeof registered !== "object" || registered.processKey !== fault.targetProcessKey
    || !Number.isSafeInteger(registered.pid) || registered.pid <= 1) {
    throw new Error("registered fault candidate identity is invalid");
  }
  return registered.pid === fault.targetProcessId
    ? "TERMINATE_PLANNED_TARGET"
    : "CONFIRM_PLANNED_TARGET_DEAD";
}

const ACCEPTANCE_LANES = ["openhands_orchestration", "quantum_science", "evidence_audit"];

function laneProcessKey(campaignId, run) {
  return run.lane === "quantum_science"
    ? `openhands-acceptance-science:${campaignId}:${run.runId}`
    : `openhands-acceptance-lane:${campaignId}:${run.runId}`;
}

function startupWorkersMatch(campaign, registered) {
  if (!Array.isArray(campaign.runs) || campaign.runs.length !== ACCEPTANCE_LANES.length
    || !Array.isArray(registered) || registered.length !== ACCEPTANCE_LANES.length) return false;
  return ACCEPTANCE_LANES.every((lane) => {
    const run = campaign.runs.find((candidate) => candidate?.lane === lane);
    if (!run || run.status !== "RUNNING" || typeof run.workerId !== "string" || !run.workerId
      || !Number.isSafeInteger(run.processId) || run.processId <= 1) return false;
    const matches = registered.filter((entry) => entry?.runId === run.runId && entry.lane === lane);
    return matches.length === 1
      && matches[0].live === true
      && matches[0].pid === run.processId
      && matches[0].processKey === laneProcessKey(campaign.campaignId, run);
  });
}

export function startupProcessAction(campaign, registered, startupComplete) {
  if (!campaign || typeof campaign !== "object" || typeof campaign.campaignId !== "string"
    || !campaign.campaignId || typeof startupComplete !== "boolean") {
    throw new Error("acceptance startup state is invalid");
  }
  const workersMatch = startupWorkersMatch(campaign, registered);
  if (startupComplete) return campaign.status === "RUNNING" && workersMatch ? "REUSE_COMPLETE" : "REFUSE_COMPLETE";
  if (campaign.status === "CREATED") return "START_CREATED";
  if (campaign.status === "RECOVERING") return "RESUME_COMPENSATED";
  if (campaign.status === "RUNNING") return workersMatch ? "MARK_COMPLETE" : "COMPENSATE_PARTIAL";
  return "REFUSE_START";
}

export function scienceFaultRunAction(fault, run) {
  if (!fault || typeof fault !== "object"
    || !["PLANNED", "INJECTED", "RECOVERED"].includes(fault.state)
    || !Number.isSafeInteger(fault.targetProcessId) || fault.targetProcessId <= 1
    || !run || typeof run !== "object" || !Number.isSafeInteger(run.processId) || run.processId <= 1
    || !Number.isSafeInteger(run.leaseGeneration) || run.leaseGeneration < 1
    || typeof run.workerId !== "string" || !run.workerId) {
    throw new Error("science fault Run state is invalid");
  }
  if (fault.state === "RECOVERED") return "ALREADY_RECOVERED";
  if (run.status === "RUNNING" && run.processId === fault.targetProcessId) return "PREPARE_OLD_RUN_RECOVERY";
  if (run.status === "RECOVERING" && run.processId === fault.targetProcessId) return "MARK_RECOVERING";
  if (run.status === "RUNNING" && run.processId !== fault.targetProcessId) return "MARK_WITH_REPLACEMENT";
  throw new Error(`science fault cannot converge with Run ${run.status}`);
}

export function laneResumeRegistrationAction(run, registered, registeredLive) {
  if (!run || typeof run !== "object" || !registered || typeof registered !== "object"
    || typeof registeredLive !== "boolean" || !Number.isSafeInteger(registered.pid) || registered.pid <= 1) {
    throw new Error("lane resume registration state is invalid");
  }
  if (run.status === "RUNNING" && run.processId === registered.pid && registeredLive) return "REUSE_ATTACHED";
  if (run.status === "RECOVERING" && !registeredLive) return "REPLACE_DEAD_REGISTRATION";
  throw new Error(`lane resume registration cannot converge from ${run.status}`);
}

export function laneRegistrationAnchor(campaignId, run, registered) {
  if (typeof campaignId !== "string" || !campaignId || !run || typeof run !== "object"
    || !Number.isSafeInteger(run.processId) || run.processId <= 1
    || !Number.isSafeInteger(run.leaseGeneration) || run.leaseGeneration < 1
    || !registered || typeof registered !== "object") {
    throw new Error("recoverable lane registry anchor input is invalid");
  }
  const expectedKind = run.lane === "quantum_science"
    ? "OPENHANDS_ACCEPTANCE_SCIENCE_WORKER"
    : "OPENHANDS_ACCEPTANCE_LANE";
  if (registered.processKey !== laneProcessKey(campaignId, run)
    || registered.kind !== expectedKind || registered.campaignId !== campaignId
    || registered.runId !== run.runId || registered.lane !== run.lane) {
    throw new Error("recoverable lane registry metadata does not match the Run");
  }
  const anchor = registered.pid === run.processId ? registered : registered.replacementAnchor;
  if (!anchor || anchor.pid !== run.processId || anchor.processKey !== registered.processKey
    || anchor.kind !== registered.kind || anchor.campaignId !== registered.campaignId
    || anchor.runId !== registered.runId || anchor.lane !== registered.lane
    || anchor.expectedLeaseGeneration !== run.leaseGeneration - 1) {
    throw new Error("recoverable lane registry replacement anchor does not match the DB lease");
  }
  return anchor;
}
