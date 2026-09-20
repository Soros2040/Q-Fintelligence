import { copyFile, mkdir, mkdtemp, readdir, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import type { DatabaseSync } from "node:sqlite";

import { afterEach, describe, expect, it } from "vitest";

import { openDatabase } from "../../apps/control-plane/src/db/migrations.js";

const roots: string[] = [];
const migrationSource = path.resolve("infra/sqlite");
const frozenAt = "2026-07-29T00:00:00.000Z";

afterEach(async () => {
  await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
});

async function copyMigrations(target: string, minimum: number, maximum: number): Promise<void> {
  await mkdir(target, { recursive: true });
  const files = (await readdir(migrationSource))
    .filter((file) => /^\d{4}_[a-z0-9_]+[.]sql$/u.test(file))
    .filter((file) => {
      const ordinal = Number(file.slice(0, 4));
      return ordinal >= minimum && ordinal <= maximum;
    });
  await Promise.all(files.map((file) => copyFile(path.join(migrationSource, file), path.join(target, file))));
}

function seedSubject(database: DatabaseSync, suffix: string): { taskId: string; conversationId: string } {
  const taskId = `task_${suffix}`;
  const conversationId = `conversation_${suffix}`;
  database.prepare(
    `INSERT INTO tasks(id, selected_provider, selected_model, state, created_at, updated_at)
     VALUES (?, 'deepseek/getoken', 'deepseek-v4-pro', 'DELIVERED', ?, ?)`,
  ).run(taskId, frozenAt, frozenAt);
  database.prepare(
    `INSERT INTO conversations(
       id, project_id, task_id, title, mode, status, provider, model_id,
       last_activity_at, created_at
     ) VALUES (?, 'project_history', ?, ?, 'PI', 'COMPLETED',
       'deepseek/getoken', 'deepseek-v4-pro', ?, ?)`,
  ).run(conversationId, taskId, `历史 ${suffix}`, frozenAt, frozenAt);
  return { taskId, conversationId };
}

function seedHistoricalFacts(database: DatabaseSync): void {
  database.prepare(
    `INSERT INTO projects(id, name, description, archived, created_at, updated_at)
     VALUES ('project_history', '历史迁移回放', 'P04/P07/P15/P16 immutable regression', 1, ?, ?)`,
  ).run(frozenAt, frozenAt);
  const p04 = seedSubject(database, "p04");
  const p07 = seedSubject(database, "p07");
  const p15 = seedSubject(database, "p15");
  const p16 = seedSubject(database, "p16");

  database.prepare(
    `INSERT INTO campaigns(
       id, task_id, conversation_id, authorization_hash, status, current_stage,
       minimum_runtime_seconds, maximum_runtime_seconds, active_compute_seconds,
       external_wait_seconds, human_wait_seconds, llm_calls, hardware_jobs,
       hardware_execution_seconds, highest_chain_level, started_at, completed_at,
       created_at, updated_at
     ) VALUES (
       'redacted-run-id', ?, ?, 'p04-auth-frozen',
       'COMPLETED', 'FINAL_AUDIT', 21600, 28800, 21879.335, 0, 0, 1, 1, 240,
       'L4', ?, ?, ?, ?
     )`,
  ).run(p04.taskId, p04.conversationId, frozenAt, frozenAt, frozenAt, frozenAt);
  database.prepare(
    `INSERT INTO external_requests(
       id, campaign_id, provider, request_kind, target, idempotency_key,
       approval_hash, request_hash, status, external_id, created_at, updated_at
     ) VALUES (
       'external_p04_frozen', 'redacted-run-id',
       'tianyan', 'HARDWARE_SUBMIT', 'tianyan176', 'p04-submit-frozen',
       'p04-approval-frozen', 'p04-request-frozen', 'COMPLETED',
       '2079613577553281025', ?, ?
     )`,
  ).run(frozenAt, frozenAt);
  database.prepare(
    `INSERT INTO quantum_jobs(
       id, campaign_id, external_request_id, purpose, backend, target_type,
       circuit_hash, shots, actual_execution_seconds, query_id, terminal_status,
       created_at, updated_at
     ) VALUES (
       'job_p04_frozen', 'redacted-run-id',
       'external_p04_frozen', 'representative_financial_qaoa', 'tianyan176',
       'HARDWARE', 'p04-circuit-frozen', 100, 240, '2079613577553281025',
       'COMPLETED', ?, ?
     )`,
  ).run(frozenAt, frozenAt);

  database.prepare(
    `INSERT INTO p07_campaigns(
       id, task_id, conversation_id, objective, provider, model_id, status, stage,
       authorization_hash, formal_test_sealed, minimum_runtime_seconds,
       minimum_verified_tokens, target_verified_tokens, verified_prompt_tokens,
       verified_completion_tokens, verified_total_tokens, stable_concurrency,
       new_external_calls_allowed, browser_gate, engineering_gate, checkpoint_json,
       error_json, started_at, completed_at, heartbeat_at, created_at, updated_at
     ) VALUES (
       'p07_redacted-run-id', ?, ?,
       'P07 frozen provider-failure fact', 'deepseek/getoken', 'deepseek-v4-pro',
       'BLOCKED', 'PROVIDER_EXHAUSTED', 'p07-auth-frozen', 1, 21600, 20000000,
       50000000, 12000000, 710331, 12710331, 1, 0, 'PASS', 'PASS',
       '{"wallClockSeconds":4358.739,"resumeSameProvider":true}',
       '{"httpStatus":402,"fallbackUsed":false}', ?, ?, ?, ?, ?
     )`,
  ).run(p07.taskId, p07.conversationId, frozenAt, frozenAt, frozenAt, frozenAt, frozenAt);

  const hashes = Array.from({ length: 7 }, (_, index) => String(index + 1).repeat(64));
  database.prepare(
    `INSERT INTO p15_calibration_snapshots(
       id, project_id, conversation_id, machine_name, machine_status, retrieved_at,
       calibration_at, cqlib_version, raw_sha256, normalized_sha256, manifest_sha256,
       diff_sha256, csv_sha256, topology_sha256, completeness, missing_fields_json,
       warnings_json, source_ids_json, created_at
     ) VALUES (
       'calibration_c1cc3d95-31e9-4eb3-8a03-fa18fed69bbc', 'project_history', ?,
       'tianyan176', 'running', ?, ?, '1.3.11', ?, ?, ?, ?, ?, ?, 'COMPLETE',
       '[]', '[]', '[]', ?
     )`,
  ).run(p15.conversationId, frozenAt, frozenAt, ...hashes.slice(0, 6), frozenAt);
  database.prepare(
    `INSERT INTO p15_campaigns(
       id, project_id, conversation_id, task_id, provider, model_id, status, stage,
       formal_test_sealed, confirmation_batches, new_external_calls_allowed,
       started_at, heartbeat_at, checkpoint_json, error_json, created_at, updated_at
     ) VALUES (
       'p15_redacted-run-id', 'project_history', ?, ?,
       'deepseek/getoken', 'deepseek-v4-pro', 'BLOCKED', 'UNKNOWN_SUBMISSION', 1,
       2, 0, ?, ?,
       '{"confirmationBatches":2,"required":3,"queryOnly":true}',
       '{"unknownBatchId":"p15_batch_50409e09-91d7-4842-938b-fd3fdd1e297c","queryId":null}',
       ?, ?
     )`,
  ).run(p15.conversationId, p15.taskId, frozenAt, frozenAt, frozenAt, frozenAt);
  database.prepare(
    `INSERT INTO approval_requests(
       id, task_id, conversation_id, action, subject_hash, rationale, status,
       requested_by, requested_at, decided_at
     ) VALUES ('approval_p15_frozen', ?, ?, 'SUBMIT_HARDWARE', 'p15-subject-frozen',
       'historical approval only', 'APPROVED', 'USER', ?, ?)`,
  ).run(p15.taskId, p15.conversationId, frozenAt, frozenAt);
  database.prepare(
    `INSERT INTO p15_generations(
       id, campaign_id, generation_index, strategy, calibration_snapshot_id,
       status, candidate_count, approval_id, batch_id, created_at
     ) VALUES (
       'generation_p15_8', 'p15_redacted-run-id',
       8, 'frozen', 'calibration_c1cc3d95-31e9-4eb3-8a03-fa18fed69bbc',
       'BLOCKED', 50, 'approval_p15_frozen',
       'p15_batch_50409e09-91d7-4842-938b-fd3fdd1e297c', ?
     )`,
  ).run(frozenAt);
  database.prepare(
    `INSERT INTO p15_hardware_batches(
       id, campaign_id, generation_id, calibration_snapshot_id, approval_id,
       lease_key, request_sha256, status, query_ids_json, heartbeat_at, created_at, updated_at
     ) VALUES (
       'p15_batch_50409e09-91d7-4842-938b-fd3fdd1e297c',
       'p15_redacted-run-id', 'generation_p15_8',
       'calibration_c1cc3d95-31e9-4eb3-8a03-fa18fed69bbc', 'approval_p15_frozen',
       'p15-lease-frozen', ?, 'UNKNOWN', '[]', ?, ?, ?
     )`,
  ).run(hashes[6], frozenAt, frozenAt, frozenAt);

  database.prepare(
    `INSERT INTO p16_hardware_campaigns(
       id, project_id, conversation_id, task_id, provider, model_id, status, stage,
       authorization_basis, formal_test_sealed, protocol_sha256, search_space_sha256,
       qubo_sha256, calibration_snapshot_id, new_external_calls_allowed,
       checkpoint_json, started_at, completed_at, heartbeat_at, created_at, updated_at
     ) VALUES (
       'p16_campaign_frozen', 'project_history', ?, ?, 'deepseek/getoken',
       'deepseek-v4-pro', 'COMPLETED', 'NEGATIVE_RESULT_RETAINED',
       'P16_USER_REQUEST_20260725', 1, ?, ?, ?,
       'calibration_c1cc3d95-31e9-4eb3-8a03-fa18fed69bbc', 0,
       '{"scientificVerdict":"NEGATIVE","formalTest":"SEALED"}',
       ?, ?, ?, ?, ?
     )`,
  ).run(p16.conversationId, p16.taskId, "8".repeat(64), "9".repeat(64), "a".repeat(64),
    frozenAt, frozenAt, frozenAt, frozenAt, frozenAt);
  const queryIds = ["2081080994530803713", "2081220012597788674", "2081221570739314690"];
  for (const [index, queryId] of queryIds.entries()) {
    const approvalId = `approval_p16_${index}`;
    const generationId = `generation_p16_${index}`;
    const batchId = `batch_p16_${index}`;
    database.prepare(
      `INSERT INTO approval_requests(
         id, task_id, conversation_id, action, subject_hash, rationale, status,
         requested_by, requested_at, decided_at
       ) VALUES (?, ?, ?, 'SUBMIT_HARDWARE', ?, 'historical P16 approval',
         'APPROVED', 'USER', ?, ?)`,
    ).run(approvalId, p16.taskId, p16.conversationId, `p16-subject-${index}`, frozenAt, frozenAt);
    database.prepare(
      `INSERT INTO p16_hardware_generations(
         id, campaign_id, generation_index, strategy, status, candidate_count,
         batch_id, created_at, completed_at
       ) VALUES (?, 'p16_campaign_frozen', ?, 'frozen', 'COMPLETED', 1, ?, ?, ?)`,
    ).run(generationId, index, batchId, frozenAt, frozenAt);
    database.prepare(
      `INSERT INTO p16_hardware_batches(
         id, campaign_id, generation_id, batch_index, purpose, machine_name, shots,
         calibration_snapshot_id, approval_id, authorization_basis, idempotency_key,
         request_sha256, request_artifact_sha256, circuit_sha256s_json,
         mapping_report_sha256, validation_report_sha256, status, query_ids_json,
         submit_attempts, submitted_at, completed_at, heartbeat_at, created_at, updated_at
       ) VALUES (
         ?, 'p16_campaign_frozen', ?, ?, 'p16_quantum_advantage_validation',
         'tianyan176', 100, 'calibration_c1cc3d95-31e9-4eb3-8a03-fa18fed69bbc', ?,
         'P16_USER_REQUEST_20260725', ?, ?, ?, ?, ?, ?, 'COMPLETED', ?, 1,
         ?, ?, ?, ?, ?
       )`,
    ).run(
      batchId,
      generationId,
      index,
      approvalId,
      `p16-key-${index}`,
      `request-${index}`,
      `request-artifact-${index}`,
      JSON.stringify([`circuit-${index}`]),
      `mapping-${index}`,
      `validation-${index}`,
      JSON.stringify([queryId]),
      frozenAt,
      frozenAt,
      frozenAt,
      frozenAt,
      frozenAt,
    );
  }
}

function frozenRows(database: DatabaseSync): Record<string, unknown[]> {
  const tables = [
    "campaigns",
    "external_requests",
    "quantum_jobs",
    "p07_campaigns",
    "p15_campaigns",
    "p15_generations",
    "p15_hardware_batches",
    "p16_hardware_campaigns",
    "p16_hardware_generations",
    "p16_hardware_batches",
  ];
  return Object.fromEntries(tables.map((table) => [
    table,
    database.prepare(`SELECT * FROM ${table} ORDER BY id`).all(),
  ]));
}

describe("OpenHands migration historical compatibility", () => {
  it("preserves frozen P04/P07/P15/P16 states, Query IDs, and query-only boundaries", async () => {
    const root = await mkdtemp(path.join(os.tmpdir(), "qf-openhands-history-"));
    roots.push(root);
    const migrations = path.join(root, "migrations");
    const databasePath = path.join(root, "state.sqlite3");
    await copyMigrations(migrations, 1, 12);
    const beforeMigration = openDatabase(databasePath, migrations);
    seedHistoricalFacts(beforeMigration.database);
    const before = frozenRows(beforeMigration.database);
    beforeMigration.database.close();

    await copyMigrations(migrations, 13, 99);
    const afterMigration = openDatabase(databasePath, migrations);
    const after = frozenRows(afterMigration.database);
    const foreignKeyViolations = afterMigration.database.prepare("PRAGMA foreign_key_check").all();
    afterMigration.database.close();

    expect(after).toEqual(before);
    expect(foreignKeyViolations).toEqual([]);
    expect(after["p15_hardware_batches"]?.[0]).toMatchObject({ status: "UNKNOWN", query_ids_json: "[]" });
    expect((after["p16_hardware_batches"] as Array<{ query_ids_json: string }>).map((row) => row.query_ids_json)).toEqual([
      '["2081080994530803713"]',
      '["2081220012597788674"]',
      '["2081221570739314690"]',
    ]);
  });
});
