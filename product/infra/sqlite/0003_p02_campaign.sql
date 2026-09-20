CREATE TABLE IF NOT EXISTS campaigns (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id),
  conversation_id TEXT NOT NULL REFERENCES conversations(id),
  authorization_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN (
    'CREATED','PREFLIGHT','RUNNING','PAUSED','BLOCKED','FAILED','COMPLETED','CANCELLED'
  )),
  current_stage TEXT NOT NULL,
  minimum_runtime_seconds INTEGER NOT NULL CHECK (minimum_runtime_seconds >= 7200),
  maximum_runtime_seconds INTEGER NOT NULL CHECK (maximum_runtime_seconds >= minimum_runtime_seconds),
  active_compute_seconds REAL NOT NULL DEFAULT 0 CHECK (active_compute_seconds >= 0),
  external_wait_seconds REAL NOT NULL DEFAULT 0 CHECK (external_wait_seconds >= 0),
  human_wait_seconds REAL NOT NULL DEFAULT 0 CHECK (human_wait_seconds >= 0),
  llm_calls INTEGER NOT NULL DEFAULT 0 CHECK (llm_calls BETWEEN 0 AND 120),
  hardware_jobs INTEGER NOT NULL DEFAULT 0 CHECK (hardware_jobs BETWEEN 0 AND 2),
  hardware_execution_seconds REAL NOT NULL DEFAULT 0 CHECK (hardware_execution_seconds BETWEEN 0 AND 600),
  highest_chain_level TEXT NOT NULL DEFAULT 'L0' CHECK (highest_chain_level IN ('L0','L1','L2','L3','L4')),
  blocker_category TEXT,
  blocker_artifact_sha256 TEXT,
  started_at TEXT,
  completed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(task_id, authorization_hash)
);

CREATE TABLE IF NOT EXISTS campaign_runs (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES campaigns(id),
  run_number INTEGER NOT NULL CHECK (run_number > 0),
  status TEXT NOT NULL,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  UNIQUE(campaign_id, run_number)
);

CREATE TABLE IF NOT EXISTS campaign_agents (
  campaign_id TEXT NOT NULL REFERENCES campaigns(id),
  role TEXT NOT NULL CHECK (role IN (
    'intake_guard','supervisor_router','data_steward','scientific_builder','quantum_executor','evidence_verifier'
  )),
  provider TEXT NOT NULL,
  model_id TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  prompt_hash TEXT NOT NULL,
  tool_names_json TEXT NOT NULL,
  state TEXT NOT NULL,
  PRIMARY KEY (campaign_id, role)
);

CREATE TABLE IF NOT EXISTS campaign_agent_sessions (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES campaigns(id),
  role TEXT NOT NULL,
  session_file TEXT NOT NULL,
  state TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(campaign_id, role),
  FOREIGN KEY (campaign_id, role) REFERENCES campaign_agents(campaign_id, role)
);

CREATE TABLE IF NOT EXISTS campaign_leases (
  campaign_id TEXT PRIMARY KEY REFERENCES campaigns(id),
  worker_id TEXT NOT NULL,
  acquired_at TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  released_at TEXT
);

CREATE TABLE IF NOT EXISTS campaign_checkpoints (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES campaigns(id),
  stage TEXT NOT NULL,
  state_hash TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaign_actions (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES campaigns(id),
  stage TEXT NOT NULL,
  action_type TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  input_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  expected_evidence TEXT NOT NULL,
  output_artifact_sha256 TEXT,
  error_json TEXT,
  started_at TEXT NOT NULL,
  completed_at TEXT
);

CREATE TABLE IF NOT EXISTS gate_decisions (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES campaigns(id),
  gate_name TEXT NOT NULL,
  subject_hash TEXT NOT NULL,
  decision TEXT NOT NULL CHECK (decision IN ('ALLOW','BLOCK','REVISE')),
  reason TEXT NOT NULL,
  evidence_artifact_sha256 TEXT,
  decided_at TEXT NOT NULL,
  UNIQUE(campaign_id, gate_name, subject_hash)
);

CREATE TABLE IF NOT EXISTS resource_authorizations (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES campaigns(id),
  authorization_hash TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('USER_STANDING','HUMAN_API')),
  scope_json TEXT NOT NULL,
  approved_at TEXT NOT NULL,
  UNIQUE(campaign_id, authorization_hash)
);

CREATE TABLE IF NOT EXISTS budget_ledger (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES campaigns(id),
  resource_kind TEXT NOT NULL,
  action_key TEXT NOT NULL,
  calls INTEGER NOT NULL DEFAULT 0,
  shots INTEGER NOT NULL DEFAULT 0,
  execution_seconds REAL NOT NULL DEFAULT 0,
  expected_evidence TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(campaign_id, resource_kind, action_key)
);

CREATE TABLE IF NOT EXISTS science_workspaces (
  campaign_id TEXT PRIMARY KEY REFERENCES campaigns(id),
  relative_root TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS science_code_revisions (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES campaigns(id),
  parent_id TEXT REFERENCES science_code_revisions(id),
  code_hash TEXT NOT NULL,
  patch_hash TEXT,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_result_envelopes (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES campaigns(id),
  action_id TEXT REFERENCES campaign_actions(id),
  schema_version TEXT NOT NULL,
  result_hash TEXT NOT NULL,
  artifact_sha256 TEXT NOT NULL,
  status TEXT NOT NULL,
  warnings_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(campaign_id, result_hash)
);

CREATE TABLE IF NOT EXISTS external_requests (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES campaigns(id),
  provider TEXT NOT NULL,
  request_kind TEXT NOT NULL,
  target TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  approval_hash TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  external_id TEXT,
  response_artifact_sha256 TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quantum_jobs (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES campaigns(id),
  external_request_id TEXT NOT NULL UNIQUE REFERENCES external_requests(id),
  purpose TEXT NOT NULL CHECK (purpose IN ('cloud_simulator','representative_qgnn_subcircuit','representative_financial_qaoa')),
  backend TEXT NOT NULL,
  target_type TEXT NOT NULL CHECK (target_type IN ('SIMULATOR','HARDWARE')),
  circuit_hash TEXT NOT NULL,
  shots INTEGER NOT NULL CHECK (shots > 0),
  estimated_execution_seconds REAL,
  actual_execution_seconds REAL,
  query_id TEXT,
  terminal_status TEXT,
  raw_result_artifact_sha256 TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS failure_cards (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES campaigns(id),
  stage TEXT NOT NULL,
  category TEXT NOT NULL,
  summary TEXT NOT NULL,
  evidence_artifact_sha256 TEXT NOT NULL,
  attempts INTEGER NOT NULL,
  recovery_command TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS release_decisions (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES campaigns(id),
  highest_chain_level TEXT NOT NULL,
  decision TEXT NOT NULL CHECK (decision IN ('DELIVER','BLOCK','REVIEW')),
  evidence_json TEXT NOT NULL,
  decided_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_campaigns_status_updated ON campaigns(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_campaign_actions_campaign_stage ON campaign_actions(campaign_id, stage, started_at);
CREATE INDEX IF NOT EXISTS idx_checkpoints_campaign_time ON campaign_checkpoints(campaign_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_external_requests_campaign_status ON external_requests(campaign_id, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_quantum_jobs_campaign_type ON quantum_jobs(campaign_id, target_type, created_at);

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES ('0003_p02_campaign', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
