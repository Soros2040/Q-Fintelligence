-- qf:migration-foreign-keys-off

CREATE TABLE p07_campaigns_v2 (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id),
  conversation_id TEXT NOT NULL UNIQUE REFERENCES conversations(id),
  predecessor_campaign_id TEXT REFERENCES p07_campaigns_v2(id),
  objective TEXT NOT NULL,
  provider TEXT NOT NULL CHECK (provider IN ('deepseek/getoken','openai/getoken')),
  model_id TEXT NOT NULL CHECK (model_id IN ('deepseek-v4-pro','gpt-5.6')),
  status TEXT NOT NULL CHECK (status IN (
    'AWAITING_AUTHORIZATION','RUNNING','BLOCKED','FAILED','COMPLETED'
  )),
  stage TEXT NOT NULL,
  authorization_hash TEXT UNIQUE,
  formal_test_sealed INTEGER NOT NULL DEFAULT 1 CHECK (formal_test_sealed = 1),
  minimum_runtime_seconds INTEGER NOT NULL DEFAULT 21600 CHECK (minimum_runtime_seconds >= 21600),
  minimum_verified_tokens INTEGER NOT NULL DEFAULT 20000000 CHECK (minimum_verified_tokens >= 20000000),
  target_verified_tokens INTEGER NOT NULL DEFAULT 50000000 CHECK (target_verified_tokens >= minimum_verified_tokens),
  verified_prompt_tokens INTEGER NOT NULL DEFAULT 0 CHECK (verified_prompt_tokens >= 0),
  verified_completion_tokens INTEGER NOT NULL DEFAULT 0 CHECK (verified_completion_tokens >= 0),
  verified_total_tokens INTEGER NOT NULL DEFAULT 0 CHECK (verified_total_tokens >= 0),
  stable_concurrency INTEGER NOT NULL DEFAULT 0 CHECK (stable_concurrency BETWEEN 0 AND 64),
  new_external_calls_allowed INTEGER NOT NULL DEFAULT 0 CHECK (new_external_calls_allowed IN (0,1)),
  browser_gate TEXT NOT NULL DEFAULT 'PENDING' CHECK (browser_gate IN ('PENDING','PASS','FAIL')),
  engineering_gate TEXT NOT NULL DEFAULT 'PENDING' CHECK (engineering_gate IN ('PENDING','PASS','FAIL')),
  checkpoint_json TEXT NOT NULL DEFAULT '{}',
  error_json TEXT,
  started_at TEXT,
  completed_at TEXT,
  heartbeat_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK (
    (provider = 'deepseek/getoken' AND model_id = 'deepseek-v4-pro')
    OR (provider = 'openai/getoken' AND model_id = 'gpt-5.6')
  )
);

INSERT INTO p07_campaigns_v2(
  id, task_id, conversation_id, predecessor_campaign_id, objective, provider, model_id,
  status, stage, authorization_hash, formal_test_sealed, minimum_runtime_seconds,
  minimum_verified_tokens, target_verified_tokens, verified_prompt_tokens,
  verified_completion_tokens, verified_total_tokens, stable_concurrency,
  new_external_calls_allowed, browser_gate, engineering_gate, checkpoint_json,
  error_json, started_at, completed_at, heartbeat_at, created_at, updated_at
)
SELECT
  id, task_id, conversation_id, NULL, objective, provider, model_id,
  status, stage, authorization_hash, formal_test_sealed, minimum_runtime_seconds,
  minimum_verified_tokens, target_verified_tokens, verified_prompt_tokens,
  verified_completion_tokens, verified_total_tokens, stable_concurrency,
  new_external_calls_allowed, browser_gate, engineering_gate, checkpoint_json,
  error_json, started_at, completed_at, heartbeat_at, created_at, updated_at
FROM p07_campaigns;

DROP TABLE p07_campaigns;
ALTER TABLE p07_campaigns_v2 RENAME TO p07_campaigns;

CREATE TABLE p07_token_ledger_v2 (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES p07_campaigns(id),
  run_id TEXT NOT NULL REFERENCES p07_runs(id),
  role TEXT NOT NULL,
  purpose TEXT NOT NULL,
  provider TEXT NOT NULL CHECK (provider IN ('deepseek/getoken','openai/getoken')),
  model_id TEXT NOT NULL CHECK (model_id IN ('deepseek-v4-pro','gpt-5.6')),
  idempotency_key TEXT NOT NULL UNIQUE,
  prompt_sha256 TEXT NOT NULL,
  response_sha256 TEXT,
  status TEXT NOT NULL CHECK (status IN ('STARTED','COMPLETED','FAILED','UNKNOWN','REUSED')),
  prompt_tokens INTEGER NOT NULL DEFAULT 0 CHECK (prompt_tokens >= 0),
  completion_tokens INTEGER NOT NULL DEFAULT 0 CHECK (completion_tokens >= 0),
  cached_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cached_tokens >= 0),
  total_tokens INTEGER NOT NULL DEFAULT 0 CHECK (total_tokens >= 0),
  response_artifact_sha256 TEXT REFERENCES artifacts(sha256),
  error_code TEXT,
  retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
  started_at TEXT NOT NULL,
  completed_at TEXT,
  CHECK (
    (provider = 'deepseek/getoken' AND model_id = 'deepseek-v4-pro')
    OR (provider = 'openai/getoken' AND model_id = 'gpt-5.6')
  )
);

INSERT INTO p07_token_ledger_v2
SELECT * FROM p07_token_ledger;

DROP TABLE p07_token_ledger;
ALTER TABLE p07_token_ledger_v2 RENAME TO p07_token_ledger;

ALTER TABLE p07_hardware_batches
ADD COLUMN reuse_of_batch_id TEXT REFERENCES p07_hardware_batches(id);

CREATE INDEX idx_p07_campaign_status ON p07_campaigns(status, updated_at);
CREATE INDEX idx_p07_ledger_campaign_status ON p07_token_ledger(campaign_id, status, started_at);

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES ('0008_p07_model_continuation', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
