import { randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import path from "node:path";

import type { JsonObject, P03QueueRunSummary } from "@q-fintelligence/contracts";

import { loadRuntimeConfig } from "../config.js";
import { openDatabase } from "../db/migrations.js";
import { WorkspaceRepository } from "../db/repository.js";
import { runP03Tianyan176QueueStep } from "./p03-operations.js";
import { CampaignRepository } from "./repository.js";

const projectRoot = process.cwd();
if (existsSync(path.join(projectRoot, ".env"))) process.loadEnvFile(path.join(projectRoot, ".env"));
if (process.env.QF_PROCESS_NAMESPACE !== "qfintelligence") throw new Error("P03 queue worker namespace is invalid");
const campaignId = process.argv[2] ?? process.env.QF_CAMPAIGN_ID;
if (!campaignId) throw new Error("P03 queue worker requires a Campaign ID");
const config = loadRuntimeConfig();
const migration = openDatabase(path.resolve(projectRoot, config.sqlitePath), path.join(projectRoot, "infra", "sqlite"));
const workspaceRepository = new WorkspaceRepository(migration.database);
const campaignRepository = new CampaignRepository(migration.database);
const workerId = `p03_queue_worker_${randomUUID()}`;
campaignRepository.acquireLease(campaignId, workerId, 180);
const heartbeat = setInterval(() => campaignRepository.heartbeat(campaignId, workerId, 180), 30_000);
heartbeat.unref();

try {
  while (true) {
    const result = await runP03Tianyan176QueueStep({
      projectRoot,
      campaignId,
      config,
      campaignRepository,
      workspaceRepository,
    });
    process.stdout.write(`${JSON.stringify(result)}\n`);
    const run = result.run as JsonObject | undefined as P03QueueRunSummary | undefined;
    if (result.foregroundStop === true || !run) break;
    const nextAt = run.nextQueryAt ? Date.parse(run.nextQueryAt) : Date.now() + 15_000;
    const delay = Math.max(1_000, Math.min(120_000, nextAt - Date.now()));
    await new Promise((resolve) => setTimeout(resolve, delay));
    campaignRepository.heartbeat(campaignId, workerId, 180);
  }
} finally {
  clearInterval(heartbeat);
  campaignRepository.releaseLease(campaignId, workerId);
  workspaceRepository.close();
}
