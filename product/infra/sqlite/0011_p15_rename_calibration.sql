CREATE TABLE rename_events (
  id TEXT PRIMARY KEY,
  scope TEXT NOT NULL CHECK (scope IN ('PROJECT', 'CONVERSATION')),
  project_id TEXT NOT NULL REFERENCES projects(id),
  conversation_id TEXT REFERENCES conversations(id),
  old_name TEXT NOT NULL,
  new_name TEXT NOT NULL,
  actor TEXT NOT NULL CHECK (actor IN ('HUMAN', 'AGENT')),
  provider TEXT,
  model_id TEXT,
  context_sha256 TEXT,
  prompt_tokens INTEGER NOT NULL DEFAULT 0 CHECK (prompt_tokens >= 0),
  completion_tokens INTEGER NOT NULL DEFAULT 0 CHECK (completion_tokens >= 0),
  status TEXT NOT NULL CHECK (status IN ('SUGGESTED', 'APPLIED', 'SUPERSEDED')),
  undo_of_event_id TEXT REFERENCES rename_events(id),
  created_at TEXT NOT NULL,
  applied_at TEXT
);

CREATE INDEX rename_events_project_created_idx
ON rename_events(project_id, created_at DESC, id);

CREATE INDEX rename_events_conversation_created_idx
ON rename_events(conversation_id, created_at DESC, id);

CREATE TABLE p15_calibration_snapshots (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  conversation_id TEXT NOT NULL REFERENCES conversations(id),
  machine_name TEXT NOT NULL CHECK (machine_name = 'tianyan176'),
  machine_status TEXT NOT NULL,
  retrieved_at TEXT NOT NULL,
  calibration_at TEXT,
  cqlib_version TEXT NOT NULL,
  raw_sha256 TEXT NOT NULL,
  normalized_sha256 TEXT NOT NULL,
  manifest_sha256 TEXT NOT NULL,
  diff_sha256 TEXT NOT NULL,
  csv_sha256 TEXT NOT NULL,
  topology_sha256 TEXT NOT NULL,
  completeness TEXT NOT NULL CHECK (completeness IN ('COMPLETE', 'PARTIAL', 'UNAVAILABLE')),
  missing_fields_json TEXT NOT NULL,
  warnings_json TEXT NOT NULL,
  source_ids_json TEXT NOT NULL,
  previous_snapshot_id TEXT REFERENCES p15_calibration_snapshots(id),
  created_at TEXT NOT NULL,
  UNIQUE(project_id, raw_sha256)
);

CREATE INDEX p15_calibration_project_created_idx
ON p15_calibration_snapshots(project_id, created_at DESC, id);

CREATE TABLE p15_campaigns (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  conversation_id TEXT NOT NULL UNIQUE REFERENCES conversations(id),
  task_id TEXT NOT NULL REFERENCES tasks(id),
  provider TEXT NOT NULL CHECK (provider = 'deepseek/getoken'),
  model_id TEXT NOT NULL CHECK (model_id = 'deepseek-v4-pro'),
  status TEXT NOT NULL CHECK (status IN (
    'PROBING', 'AWAITING_AUTHORIZATION', 'RUNNING', 'WAITING_HARDWARE',
    'COMPLETED', 'BLOCKED', 'NEGATIVE_RESULT', 'FAILED'
  )),
  stage TEXT NOT NULL,
  formal_test_sealed INTEGER NOT NULL DEFAULT 1 CHECK (formal_test_sealed = 1),
  qubo_sha256 TEXT,
  classical_result_sha256 TEXT,
  best_candidate_sha256 TEXT,
  target_objective REAL,
  best_quantum_objective REAL,
  confirmation_batches INTEGER NOT NULL DEFAULT 0 CHECK (confirmation_batches >= 0),
  new_external_calls_allowed INTEGER NOT NULL DEFAULT 1 CHECK (new_external_calls_allowed IN (0, 1)),
  started_at TEXT NOT NULL,
  completed_at TEXT,
  heartbeat_at TEXT NOT NULL,
  checkpoint_json TEXT NOT NULL DEFAULT '{}',
  error_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE p15_generations (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES p15_campaigns(id),
  generation_index INTEGER NOT NULL CHECK (generation_index >= 0),
  strategy TEXT NOT NULL,
  calibration_snapshot_id TEXT NOT NULL REFERENCES p15_calibration_snapshots(id),
  status TEXT NOT NULL CHECK (status IN (
    'GENERATING', 'READY_FOR_APPROVAL', 'SUBMITTED', 'QUERYING',
    'COMPLETED', 'BLOCKED', 'FAILED'
  )),
  candidate_count INTEGER NOT NULL CHECK (candidate_count BETWEEN 1 AND 50),
  top5_json TEXT NOT NULL DEFAULT '[]',
  approval_id TEXT REFERENCES approval_requests(id),
  batch_id TEXT,
  metrics_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  completed_at TEXT,
  UNIQUE(campaign_id, generation_index)
);

CREATE TABLE p15_candidates (
  id TEXT PRIMARY KEY,
  generation_id TEXT NOT NULL REFERENCES p15_generations(id),
  candidate_index INTEGER NOT NULL CHECK (candidate_index >= 0),
  family TEXT NOT NULL,
  parameters_json TEXT NOT NULL,
  qcis TEXT NOT NULL,
  qcis_sha256 TEXT NOT NULL UNIQUE,
  circuit_artifact_sha256 TEXT NOT NULL,
  mapped_qubits_json TEXT NOT NULL,
  local_metrics_json TEXT NOT NULL,
  query_id TEXT UNIQUE,
  terminal_status TEXT,
  raw_result_sha256 TEXT,
  corrected_result_sha256 TEXT,
  hardware_metrics_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(generation_id, candidate_index)
);

CREATE TABLE p15_hardware_batches (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES p15_campaigns(id),
  generation_id TEXT NOT NULL UNIQUE REFERENCES p15_generations(id),
  calibration_snapshot_id TEXT NOT NULL REFERENCES p15_calibration_snapshots(id),
  approval_id TEXT NOT NULL REFERENCES approval_requests(id),
  lease_key TEXT NOT NULL UNIQUE,
  request_sha256 TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK (status IN (
    'PREPARED', 'COMMITTING', 'SUBMITTED', 'QUERYING',
    'COMPLETED', 'UNKNOWN', 'BLOCKED', 'FAILED'
  )),
  query_ids_json TEXT NOT NULL DEFAULT '[]',
  submitted_at TEXT,
  completed_at TEXT,
  heartbeat_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

INSERT INTO schema_migrations(version, applied_at)
VALUES ('0011_p15_rename_calibration', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
