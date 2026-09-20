CREATE TABLE p07_campaigns (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id),
  conversation_id TEXT NOT NULL UNIQUE REFERENCES conversations(id),
  objective TEXT NOT NULL,
  provider TEXT NOT NULL CHECK (provider = 'deepseek/getoken'),
  model_id TEXT NOT NULL CHECK (model_id = 'deepseek-v4-pro'),
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
  updated_at TEXT NOT NULL
);

CREATE TABLE p07_roles (
  campaign_id TEXT NOT NULL REFERENCES p07_campaigns(id),
  role TEXT NOT NULL CHECK (role IN (
    'input_security_router','research_supervisor','data_agent','classical_algorithm_agent',
    'quantum_algorithm_agent','circuit_compiler_agent','tool_builder_agent',
    'experiment_runner','scientific_critic','archive_release_gate'
  )),
  conversation_id TEXT NOT NULL UNIQUE REFERENCES conversations(id),
  prompt_version TEXT NOT NULL,
  prompt_hash TEXT NOT NULL,
  tool_names_json TEXT NOT NULL,
  state TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (campaign_id, role)
);

CREATE TABLE p07_runs (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES p07_campaigns(id),
  lane TEXT NOT NULL CHECK (lane IN (
    'data_quality','classical_algorithms','quantum_circuits','capability_validation',
    'deepseek_agents','visualization_archive'
  )),
  status TEXT NOT NULL CHECK (status IN ('CREATED','RUNNING','RECOVERING','BLOCKED','FAILED','COMPLETED')),
  worker_id TEXT,
  process_id INTEGER,
  lease_acquired_at TEXT,
  heartbeat_at TEXT,
  lease_expires_at TEXT,
  released_at TEXT,
  recovery_count INTEGER NOT NULL DEFAULT 0 CHECK (recovery_count >= 0),
  checkpoint_json TEXT NOT NULL DEFAULT '{}',
  started_at TEXT,
  completed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (campaign_id, lane)
);

CREATE TABLE p07_events (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL UNIQUE,
  campaign_id TEXT NOT NULL REFERENCES p07_campaigns(id),
  lane TEXT,
  event_type TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  payload_hash TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE p07_token_ledger (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES p07_campaigns(id),
  run_id TEXT NOT NULL REFERENCES p07_runs(id),
  role TEXT NOT NULL,
  purpose TEXT NOT NULL,
  provider TEXT NOT NULL CHECK (provider = 'deepseek/getoken'),
  model_id TEXT NOT NULL CHECK (model_id = 'deepseek-v4-pro'),
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
  completed_at TEXT
);

CREATE TABLE p07_sources (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES p07_campaigns(id),
  kind TEXT NOT NULL CHECK (kind IN ('GITHUB','PAPER','DATA','PLATFORM_DOC')),
  title TEXT NOT NULL,
  url TEXT NOT NULL,
  domain TEXT NOT NULL,
  version TEXT NOT NULL,
  commit_hash TEXT,
  license TEXT NOT NULL,
  maintenance_status TEXT NOT NULL,
  retrieved_at TEXT NOT NULL,
  review_artifact_sha256 TEXT REFERENCES artifacts(sha256),
  UNIQUE (campaign_id, url, version)
);

CREATE TABLE p07_capability_packages (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES p07_campaigns(id),
  name TEXT NOT NULL,
  capability_kind TEXT NOT NULL CHECK (capability_kind IN ('CLASSICAL','QUANTUM_CIRCUIT')),
  version TEXT NOT NULL,
  source_id TEXT NOT NULL REFERENCES p07_sources(id),
  status TEXT NOT NULL CHECK (status IN ('REVIEWING','APPROVED','REJECTED')),
  adapter_hash TEXT NOT NULL,
  schema_hash TEXT NOT NULL,
  benchmark_artifact_sha256 TEXT REFERENCES artifacts(sha256),
  knowledge_artifact_sha256 TEXT REFERENCES artifacts(sha256),
  rejection_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (campaign_id, name, version)
);

CREATE TABLE p07_circuits (
  circuit_hash TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES p07_campaigns(id),
  family TEXT NOT NULL,
  depth INTEGER NOT NULL CHECK (depth >= 0),
  shots INTEGER NOT NULL CHECK (shots > 0),
  mapping_json TEXT NOT NULL,
  circuit_ir_artifact_sha256 TEXT NOT NULL REFERENCES artifacts(sha256),
  qcis_artifact_sha256 TEXT NOT NULL REFERENCES artifacts(sha256),
  query_id TEXT UNIQUE,
  terminal_state TEXT,
  raw_result_artifact_sha256 TEXT REFERENCES artifacts(sha256),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE p07_hardware_batches (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES p07_campaigns(id),
  backend TEXT NOT NULL CHECK (backend = 'tianyan176'),
  idempotency_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK (status IN ('REGISTERED','COMMITTING','SUBMITTED','QUERYING','COMPLETED','UNKNOWN','FAILED')),
  circuit_count INTEGER NOT NULL CHECK (circuit_count BETWEEN 1 AND 50),
  shots INTEGER NOT NULL CHECK (shots > 0),
  circuit_hashes_json TEXT NOT NULL,
  query_ids_json TEXT NOT NULL DEFAULT '[]',
  result_artifact_sha256 TEXT REFERENCES artifacts(sha256),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT
);

CREATE TABLE p07_hardware_lease (
  backend TEXT PRIMARY KEY CHECK (backend = 'tianyan176'),
  campaign_id TEXT NOT NULL REFERENCES p07_campaigns(id),
  batch_id TEXT NOT NULL REFERENCES p07_hardware_batches(id),
  worker_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  released_at TEXT
);

CREATE TABLE p07_history_entities (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES p07_campaigns(id),
  entity_type TEXT NOT NULL CHECK (entity_type IN ('A2A','P2A','REVIEW','RUN','EVENT','SOURCE','CIRCUIT','ARTIFACT')),
  logical_key TEXT NOT NULL,
  version INTEGER NOT NULL CHECK (version > 0),
  sha256 TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  parent_id TEXT REFERENCES p07_history_entities(id),
  immutable INTEGER NOT NULL DEFAULT 1 CHECK (immutable = 1),
  created_at TEXT NOT NULL,
  UNIQUE (campaign_id, entity_type, logical_key, version)
);

CREATE TABLE p07_fault_injections (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES p07_campaigns(id),
  kind TEXT NOT NULL CHECK (kind IN (
    'WORKER_CRASH','SQLITE_BUSY','PROVIDER_RATE_LIMIT','NETWORK_INTERRUPTION','ARTIFACT_WRITE_FAILURE'
  )),
  status TEXT NOT NULL CHECK (status IN ('PLANNED','INJECTED','RECOVERED','FAILED')),
  duplicate_model_calls INTEGER NOT NULL DEFAULT 0 CHECK (duplicate_model_calls >= 0),
  duplicate_hardware_submissions INTEGER NOT NULL DEFAULT 0 CHECK (duplicate_hardware_submissions >= 0),
  evidence_artifact_sha256 TEXT REFERENCES artifacts(sha256),
  created_at TEXT NOT NULL,
  injected_at TEXT,
  recovered_at TEXT,
  UNIQUE (campaign_id, kind)
);

CREATE INDEX idx_p07_campaign_status ON p07_campaigns(status, updated_at);
CREATE INDEX idx_p07_events_campaign_sequence ON p07_events(campaign_id, sequence);
CREATE INDEX idx_p07_ledger_campaign_status ON p07_token_ledger(campaign_id, status, started_at);
CREATE INDEX idx_p07_runs_campaign_status ON p07_runs(campaign_id, status, updated_at);
CREATE INDEX idx_p07_history_lookup ON p07_history_entities(campaign_id, entity_type, logical_key);

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES ('0007_p07_long_campaign', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
