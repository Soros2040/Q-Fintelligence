CREATE TABLE IF NOT EXISTS p03_queue_runs (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES campaigns(id),
  ordinal INTEGER NOT NULL CHECK (ordinal BETWEEN 1 AND 2),
  stage_id TEXT NOT NULL CHECK (stage_id = 'P03'),
  authorization_hash TEXT NOT NULL,
  backend TEXT NOT NULL CHECK (backend = 'tianyan176'),
  shots INTEGER NOT NULL CHECK (shots = 100),
  purpose TEXT NOT NULL CHECK (purpose IN (
    'queue_state_machine_validation','queue_state_machine_regression'
  )),
  lifecycle_state TEXT NOT NULL CHECK (lifecycle_state IN (
    'DISCOVERING','BACKEND_UNAVAILABLE','READY_TO_SUBMIT','SUBMITTING',
    'QUEUED','RUNNING','COMPLETED','FAILED','CANCELLED','UNKNOWN'
  )),
  circuit_hash TEXT,
  request_hash TEXT,
  idempotency_key TEXT UNIQUE,
  external_request_id TEXT UNIQUE REFERENCES external_requests(id),
  quantum_job_id TEXT UNIQUE REFERENCES quantum_jobs(id),
  reserved_execution_seconds REAL NOT NULL DEFAULT 0 CHECK (reserved_execution_seconds BETWEEN 0 AND 600),
  query_id TEXT,
  provider_status TEXT,
  machine_status TEXT,
  queue_position INTEGER,
  estimated_start_at TEXT,
  last_queried_at TEXT,
  next_query_at TEXT,
  poll_attempts INTEGER NOT NULL DEFAULT 0 CHECK (poll_attempts >= 0),
  queue_entered_at TEXT,
  running_entered_at TEXT,
  terminal_at TEXT,
  checkpoint_30m_at TEXT,
  unknown_submission INTEGER NOT NULL DEFAULT 0 CHECK (unknown_submission IN (0,1)),
  discovery_artifact_sha256 TEXT,
  state_artifact_sha256 TEXT,
  raw_result_artifact_sha256 TEXT,
  second_job_evidence_sha256 TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(campaign_id, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_p03_queue_campaign_state
ON p03_queue_runs(campaign_id, lifecycle_state, updated_at);

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES ('0004_p03_queue', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
