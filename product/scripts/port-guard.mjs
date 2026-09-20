import { inspectQfPorts, RESERVED_PORTS } from "./port-registry.mjs";

if (process.env.QF_PROCESS_NAMESPACE && process.env.QF_PROCESS_NAMESPACE !== "qfintelligence") {
  throw new Error("QF_PROCESS_NAMESPACE must be qfintelligence");
}

const results = await inspectQfPorts();
const busy = results.filter((result) => !result.available);
const payload = {
  project: "q-fintelligence",
  qfPorts: results,
  xhReservedPorts: RESERVED_PORTS,
  status: busy.length === 0 ? "PASS" : "BLOCKED",
};

process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
if (busy.length > 0) process.exitCode = 2;
