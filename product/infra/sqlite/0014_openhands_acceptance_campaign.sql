CREATE TABLE IF NOT EXISTS openhands_acceptance_campaigns (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  conversation_id TEXT NOT NULL UNIQUE REFERENCES conversations(id),
  task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
  status TEXT NOT NULL CHECK (status IN ('CREATED','RUNNING','RECOVERING','COMPLETED','FAILED','BLOCKED','CANCELLED')),
  fixed_provider_model TEXT NOT NULL CHECK (fixed_provider_model = 'deepseek/deepseek-v4-pro'),
  authorization_hash TEXT NOT NULL,
  system_prompt_hash TEXT NOT NULL,
  runtime_config_hash TEXT,
  minimum_wall_clock_seconds INTEGER NOT NULL CHECK (minimum_wall_clock_seconds >= 7200),
  minimum_overlap_seconds INTEGER NOT NULL CHECK (minimum_overlap_seconds >= 6900),
  hardware_mode TEXT NOT NULL CHECK (hardware_mode = 'READ_ONLY'),
  hardware_target TEXT NOT NULL CHECK (hardware_target = 'tianyan176'),
  max_new_hardware_jobs INTEGER NOT NULL CHECK (max_new_hardware_jobs = 0),
  shots_per_job INTEGER NOT NULL CHECK (shots_per_job = 0),
  formal_test_sealed INTEGER NOT NULL CHECK (formal_test_sealed = 1),
  provider_prompt_tokens INTEGER NOT NULL DEFAULT 0 CHECK (provider_prompt_tokens >= 0),
  provider_completion_tokens INTEGER NOT NULL DEFAULT 0 CHECK (provider_completion_tokens >= 0),
  provider_calls INTEGER NOT NULL DEFAULT 0 CHECK (provider_calls >= 0),
  hardware_jobs_created INTEGER NOT NULL DEFAULT 0 CHECK (hardware_jobs_created = 0),
  started_at TEXT,
  completed_at TEXT,
  frozen_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS openhands_acceptance_runs (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES openhands_acceptance_campaigns(id),
  lane TEXT NOT NULL CHECK (lane IN ('openhands_orchestration','quantum_science','evidence_audit')),
  status TEXT NOT NULL CHECK (status IN ('CREATED','RUNNING','RECOVERING','COMPLETED','FAILED','BLOCKED','CANCELLED')),
  relative_workspace TEXT NOT NULL UNIQUE,
  worker_id TEXT,
  process_id INTEGER,
  recovery_count INTEGER NOT NULL DEFAULT 0 CHECK (recovery_count >= 0),
  active_seconds REAL NOT NULL DEFAULT 0 CHECK (active_seconds >= 0),
  external_wait_seconds REAL NOT NULL DEFAULT 0 CHECK (external_wait_seconds >= 0),
  started_at TEXT,
  completed_at TEXT,
  last_heartbeat_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(campaign_id, lane)
);

CREATE TABLE IF NOT EXISTS openhands_acceptance_events (
  run_id TEXT NOT NULL REFERENCES openhands_acceptance_runs(id),
  sequence INTEGER NOT NULL CHECK (sequence > 0),
  event_type TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (run_id, sequence)
);

CREATE TABLE IF NOT EXISTS openhands_acceptance_checkpoints (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES openhands_acceptance_campaigns(id),
  run_id TEXT REFERENCES openhands_acceptance_runs(id),
  stage TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  payload_hash TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS openhands_acceptance_artifact_claims (
  run_id TEXT NOT NULL REFERENCES openhands_acceptance_runs(id),
  artifact_sha256 TEXT NOT NULL REFERENCES artifacts(sha256),
  logical_name TEXT NOT NULL,
  relative_output_path TEXT NOT NULL,
  parent_hashes_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (run_id, artifact_sha256),
  UNIQUE(run_id, logical_name),
  UNIQUE(relative_output_path)
);

CREATE TABLE IF NOT EXISTS openhands_acceptance_faults (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES openhands_acceptance_campaigns(id),
  run_id TEXT NOT NULL REFERENCES openhands_acceptance_runs(id),
  kind TEXT NOT NULL CHECK (kind IN ('OPENHANDS_PROCESS_TERMINATION','SCIENCE_WORKER_TERMINATION')),
  target_process_id INTEGER NOT NULL CHECK (target_process_id > 1),
  state TEXT NOT NULL CHECK (state IN ('PLANNED','INJECTED','RECOVERED','FAILED')),
  before_checkpoint_hash TEXT NOT NULL,
  after_checkpoint_hash TEXT,
  injected_at TEXT,
  recovered_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(campaign_id, kind)
);

CREATE INDEX IF NOT EXISTS idx_openhands_acceptance_campaign_status
ON openhands_acceptance_campaigns(status, updated_at);

CREATE INDEX IF NOT EXISTS idx_openhands_acceptance_run_campaign
ON openhands_acceptance_runs(campaign_id, lane, status);

CREATE INDEX IF NOT EXISTS idx_openhands_acceptance_event_sequence
ON openhands_acceptance_events(run_id, sequence);

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES ('0014_openhands_acceptance_campaign', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
