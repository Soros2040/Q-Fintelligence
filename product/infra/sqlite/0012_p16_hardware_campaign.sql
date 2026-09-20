-- Authorship category: supervisor_infrastructure
-- P16 hardware Campaign/Generation/Batch durability and idempotency state.

CREATE TABLE p16_hardware_campaigns (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  conversation_id TEXT NOT NULL UNIQUE REFERENCES conversations(id),
  task_id TEXT NOT NULL REFERENCES tasks(id),
  provider TEXT NOT NULL CHECK (provider = 'deepseek/getoken'),
  model_id TEXT NOT NULL CHECK (model_id = 'deepseek-v4-pro'),
  status TEXT NOT NULL CHECK (status IN (
    'PREPARING', 'READY', 'RUNNING', 'WAITING_HARDWARE',
    'COMPLETED', 'BLOCKED', 'NEGATIVE_RESULT', 'FAILED'
  )),
  stage TEXT NOT NULL,
  authorization_basis TEXT NOT NULL CHECK (authorization_basis = 'P16_USER_REQUEST_20260725'),
  formal_test_sealed INTEGER NOT NULL DEFAULT 1 CHECK (formal_test_sealed = 1),
  protocol_sha256 TEXT NOT NULL,
  search_space_sha256 TEXT NOT NULL,
  qubo_sha256 TEXT NOT NULL,
  calibration_snapshot_id TEXT NOT NULL REFERENCES p15_calibration_snapshots(id),
  new_external_calls_allowed INTEGER NOT NULL DEFAULT 1 CHECK (new_external_calls_allowed IN (0, 1)),
  checkpoint_json TEXT NOT NULL DEFAULT '{}',
  error_json TEXT,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  heartbeat_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE p16_hardware_generations (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES p16_hardware_campaigns(id),
  generation_index INTEGER NOT NULL CHECK (generation_index >= 0),
  strategy TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN (
    'PREPARED', 'SUBMITTED', 'QUERYING', 'COMPLETED',
    'UNKNOWN', 'BLOCKED', 'FAILED'
  )),
  candidate_count INTEGER NOT NULL CHECK (candidate_count BETWEEN 1 AND 50),
  batch_id TEXT,
  created_at TEXT NOT NULL,
  completed_at TEXT,
  UNIQUE(campaign_id, generation_index)
);

CREATE TABLE p16_hardware_batches (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES p16_hardware_campaigns(id),
  generation_id TEXT NOT NULL UNIQUE REFERENCES p16_hardware_generations(id),
  batch_index INTEGER NOT NULL CHECK (batch_index >= 0),
  purpose TEXT NOT NULL CHECK (purpose = 'p16_quantum_advantage_validation'),
  machine_name TEXT NOT NULL CHECK (machine_name = 'tianyan176'),
  shots INTEGER NOT NULL CHECK (shots = 100),
  calibration_snapshot_id TEXT NOT NULL REFERENCES p15_calibration_snapshots(id),
  approval_id TEXT NOT NULL REFERENCES approval_requests(id),
  authorization_basis TEXT NOT NULL CHECK (authorization_basis = 'P16_USER_REQUEST_20260725'),
  idempotency_key TEXT NOT NULL UNIQUE,
  request_sha256 TEXT NOT NULL UNIQUE,
  request_artifact_sha256 TEXT NOT NULL,
  circuit_sha256s_json TEXT NOT NULL,
  mapping_report_sha256 TEXT NOT NULL,
  validation_report_sha256 TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN (
    'PREPARED', 'COMMITTING', 'SUBMITTED', 'QUERYING',
    'COMPLETED', 'UNKNOWN', 'BLOCKED', 'FAILED'
  )),
  query_ids_json TEXT NOT NULL DEFAULT '[]',
  submission_artifact_sha256 TEXT,
  raw_result_sha256 TEXT,
  corrected_result_sha256 TEXT,
  error_json TEXT,
  submit_attempts INTEGER NOT NULL DEFAULT 0 CHECK (submit_attempts BETWEEN 0 AND 1),
  submitted_at TEXT,
  completed_at TEXT,
  heartbeat_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(campaign_id, batch_index)
);

CREATE INDEX p16_hardware_batch_status_idx
ON p16_hardware_batches(status, heartbeat_at, id);

INSERT INTO schema_migrations(version, applied_at)
VALUES ('0012_p16_hardware_campaign', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
