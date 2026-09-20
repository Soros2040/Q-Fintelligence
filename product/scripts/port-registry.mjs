import net from "node:net";

export const EXPECTED_WSL_ROOT = process.cwd();
export const PROCESS_NAMESPACE = "qfintelligence";

export const RESERVED_PORTS = Object.freeze([43_187, 43_188, 43_189, 43_190, 43_191, 43_192]);

const EXPECTED_QF_PORTS = Object.freeze({
  web: 27_871,
  api: 27_872,
  workerHealth: 27_873,
  debug: 27_874,
  preview: 27_875,
});

const ENV_BY_SERVICE = Object.freeze({
  web: "QF_WEB_PORT",
  api: "QF_API_PORT",
  workerHealth: "QF_WORKER_HEALTH_PORT",
  debug: "QF_DEBUG_PORT",
  preview: "QF_PREVIEW_PORT",
});

export function getPortRegistry(env = process.env) {
  const services = Object.fromEntries(
    Object.entries(EXPECTED_QF_PORTS).map(([name, expected]) => {
      const variable = ENV_BY_SERVICE[name];
      const actual = Number(env[variable] ?? expected);
      if (!Number.isInteger(actual) || actual < 1024 || actual > 65_535) {
        throw new Error(`${variable} must be an integer between 1024 and 65535`);
      }
      if (actual !== expected) {
        throw new Error(`${variable} is fixed at ${expected}; automatic or local port drift is forbidden`);
      }
      if (RESERVED_PORTS.includes(actual)) {
        throw new Error(`${variable}=${actual} conflicts with local runtime`);
      }
      return [name, actual];
    }),
  );

  const ports = Object.values(services);
  if (new Set(ports).size !== ports.length) {
    throw new Error("q-fintelligence ports must be unique");
  }
  return services;
}

export function checkPortAvailable(port, host = "127.0.0.1") {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.unref();
    server.once("error", (error) => resolve({ port, available: false, error: error.code ?? error.message }));
    server.listen({ host, port, exclusive: true }, () => {
      server.close(() => resolve({ port, available: true }));
    });
  });
}

export async function inspectQfPorts(env = process.env) {
  const services = getPortRegistry(env);
  return Promise.all(
    Object.entries(services).map(async ([service, port]) => ({
      service,
      ...(await checkPortAvailable(port)),
    })),
  );
}
