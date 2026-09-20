import { existsSync } from "node:fs";
import path from "node:path";

import { createApplication } from "./app.js";
import { loadRuntimeConfig, resolveProjectRoot } from "./config.js";

process.title = "qfint-control-plane";
if (process.env.QF_PROCESS_NAMESPACE !== "qfintelligence") {
  process.env.QF_PROCESS_NAMESPACE = "qfintelligence";
}

const projectRoot = resolveProjectRoot();
const envPath = path.join(projectRoot, ".env");
if (existsSync(envPath)) process.loadEnvFile(envPath);
const config = loadRuntimeConfig();
const { app } = createApplication({ config, projectRoot });

const shutdown = async (signal: string) => {
  app.log.info({ signal }, "shutting down q-fintelligence control plane");
  await app.close();
  process.exit(0);
};

process.once("SIGINT", () => void shutdown("SIGINT"));
process.once("SIGTERM", () => void shutdown("SIGTERM"));

try {
  await app.listen({ host: config.apiHost, port: config.apiPort });
} catch (error) {
  app.log.error(error);
  process.exit(1);
}
