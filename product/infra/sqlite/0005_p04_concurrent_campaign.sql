CREATE TABLE IF NOT EXISTS p04_runs (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES campaigns(id),
  lane TEXT NOT NULL CHECK (lane IN ('finance','tool_factory','evidence_audit')),
  status TEXT NOT NULL CHECK (status IN ('CREATED','RUNNING','RECOVERING','COMPLETED','BLOCKED','FAILED')),
  relative_workspace TEXT NOT NULL UNIQUE,
  worker_id TEXT,
  process_id INTEGER,
  recovery_count INTEGER NOT NULL DEFAULT 0 CHECK (recovery_count >= 0),
  started_at TEXT,
  completed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(campaign_id, lane)
);

CREATE TABLE IF NOT EXISTS p04_run_leases (
  run_id TEXT PRIMARY KEY REFERENCES p04_runs(id),
  worker_id TEXT NOT NULL,
  process_id INTEGER NOT NULL CHECK (process_id > 1),
  acquired_at TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  released_at TEXT
);

CREATE TABLE IF NOT EXISTS p04_run_events (
  run_id TEXT NOT NULL REFERENCES p04_runs(id),
  sequence INTEGER NOT NULL CHECK (sequence > 0),
  event_type TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (run_id, sequence)
);

CREATE TABLE IF NOT EXISTS p04_artifact_claims (
  run_id TEXT NOT NULL REFERENCES p04_runs(id),
  artifact_sha256 TEXT NOT NULL REFERENCES artifacts(sha256),
  logical_name TEXT NOT NULL,
  relative_output_path TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (run_id, artifact_sha256),
  UNIQUE(run_id, logical_name),
  UNIQUE(relative_output_path)
);

CREATE TABLE IF NOT EXISTS p04_generated_tools (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES campaigns(id),
  run_id TEXT NOT NULL REFERENCES p04_runs(id),
  name TEXT NOT NULL CHECK (name IN ('financial_result_diagnostics','quantum_result_diagnostics')),
  status TEXT NOT NULL CHECK (status IN ('GENERATED','VALIDATED','REGISTERED','REJECTED')),
  spec_hash TEXT NOT NULL,
  code_hash TEXT NOT NULL,
  test_hash TEXT NOT NULL,
  source_artifact_sha256 TEXT REFERENCES artifacts(sha256),
  test_artifact_sha256 TEXT REFERENCES artifacts(sha256),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(campaign_id, name)
);

CREATE TABLE IF NOT EXISTS p04_tool_invocations (
  id TEXT PRIMARY KEY,
  tool_id TEXT NOT NULL REFERENCES p04_generated_tools(id),
  run_id TEXT NOT NULL REFERENCES p04_runs(id),
  idempotency_key TEXT NOT NULL UNIQUE,
  input_hash TEXT NOT NULL,
  output_artifact_sha256 TEXT REFERENCES artifacts(sha256),
  status TEXT NOT NULL CHECK (status IN ('RUNNING','COMPLETED','FAILED')),
  exit_code INTEGER,
  duration_seconds REAL,
  created_at TEXT NOT NULL,
  completed_at TEXT
);

CREATE TABLE IF NOT EXISTS p04_fault_injections (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES campaigns(id),
  run_id TEXT NOT NULL REFERENCES p04_runs(id),
  kind TEXT NOT NULL CHECK (kind IN ('SCIENCE_PROCESS_TERMINATION','EXTERNAL_QUERY_PROCESS_TERMINATION')),
  target_process_id INTEGER NOT NULL CHECK (target_process_id > 1),
  state TEXT NOT NULL CHECK (state IN ('PLANNED','INJECTED','RECOVERED','FAILED')),
  checkpoint_artifact_sha256 TEXT REFERENCES artifacts(sha256),
  recovery_artifact_sha256 TEXT REFERENCES artifacts(sha256),
  created_at TEXT NOT NULL,
  injected_at TEXT,
  recovered_at TEXT,
  updated_at TEXT NOT NULL,
  UNIQUE(campaign_id, kind)
);

CREATE TABLE IF NOT EXISTS p04_hardware_lease (
  backend TEXT PRIMARY KEY CHECK (backend = 'tianyan176'),
  campaign_id TEXT NOT NULL REFERENCES campaigns(id),
  run_id TEXT NOT NULL REFERENCES p04_runs(id),
  worker_id TEXT NOT NULL,
  state TEXT NOT NULL,
  query_id TEXT,
  acquired_at TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  released_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_p04_runs_campaign_status
ON p04_runs(campaign_id, status, updated_at);

CREATE INDEX IF NOT EXISTS idx_p04_events_run_sequence
ON p04_run_events(run_id, sequence);

CREATE INDEX IF NOT EXISTS idx_p04_tool_invocations_tool
ON p04_tool_invocations(tool_id, created_at);

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES ('0005_p04_concurrent_campaign', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
