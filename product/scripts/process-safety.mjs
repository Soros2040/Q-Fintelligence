import { readFile, readlink } from "node:fs/promises";

import { PROCESS_NAMESPACE } from "./port-registry.mjs";

export async function inspectOwnedProcess(pid, expectedRoot, expected = {}) {
  if (!Number.isInteger(pid) || pid <= 1) return { owned: false, reason: "invalid-pid" };
  try {
    const [cwd, environment, stat, bootId] = await Promise.all([
      readlink(`/proc/${pid}/cwd`),
      readFile(`/proc/${pid}/environ`, "utf8"),
      readFile(`/proc/${pid}/stat`, "utf8"),
      readFile("/proc/sys/kernel/random/boot_id", "utf8"),
    ]);
    const commandEnd = stat.lastIndexOf(")");
    const startTimeTicks = commandEnd < 0
      ? ""
      : stat.slice(commandEnd + 2).trim().split(/\s+/u)[19] ?? "";
    const namespacePresent = environment
      .split("\0")
      .includes(`QF_PROCESS_NAMESPACE=${PROCESS_NAMESPACE}`);
    if (expected.bootId !== undefined && bootId.trim() !== expected.bootId) {
      return { owned: false, reason: "boot-id-mismatch" };
    }
    if (expected.startTimeTicks !== undefined && startTimeTicks !== expected.startTimeTicks) {
      return { owned: false, reason: "start-time-mismatch" };
    }
    if (cwd !== expectedRoot) return { owned: false, reason: `cwd=${cwd}` };
    if (!namespacePresent) return { owned: false, reason: "namespace-missing" };
    return { owned: true, reason: "validated", startTimeTicks, bootId: bootId.trim() };
  } catch (error) {
    return { owned: false, reason: error.code ?? String(error) };
  }
}
