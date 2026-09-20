PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version TEXT PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  selected_provider TEXT NOT NULL,
  selected_model TEXT NOT NULL,
  state TEXT NOT NULL CHECK (
    state IN ('DRAFT','SPECIFIED','APPROVED','RUNNING','PAUSED','BLOCKED','FAILED','VALIDATED','DELIVERED')
  ),
  protocol_hash TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS spec_versions (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id),
  kind TEXT NOT NULL CHECK (kind IN ('TASK','PROTOCOL')),
  version INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  frozen_at TEXT,
  payload_json TEXT NOT NULL,
  UNIQUE(task_id, kind, version)
);

CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id),
  parent_run_id TEXT REFERENCES runs(id),
  iteration INTEGER NOT NULL CHECK (iteration BETWEEN 0 AND 5),
  code_revision TEXT NOT NULL,
  environment_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT
);

CREATE TABLE IF NOT EXISTS steps (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  name TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL,
  error_category TEXT,
  started_at TEXT,
  finished_at TEXT
);

CREATE TABLE IF NOT EXISTS artifacts (
  sha256 TEXT PRIMARY KEY,
  media_type TEXT NOT NULL,
  bytes INTEGER NOT NULL CHECK (bytes >= 0),
  relative_path TEXT NOT NULL UNIQUE,
  producer TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifact_edges (
  child_sha256 TEXT NOT NULL REFERENCES artifacts(sha256),
  parent_sha256 TEXT NOT NULL REFERENCES artifacts(sha256),
  PRIMARY KEY (child_sha256, parent_sha256)
);

CREATE TABLE IF NOT EXISTS events (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL REFERENCES tasks(id),
  run_id TEXT REFERENCES runs(id),
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id),
  action TEXT NOT NULL CHECK (action IN ('FREEZE_PROTOCOL','NEXT_ITERATION','UNSEAL_TEST','SUBMIT_HARDWARE')),
  subject_hash TEXT NOT NULL,
  approved_by TEXT NOT NULL,
  approved_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_capabilities (
  id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  model_id TEXT NOT NULL,
  supports_streaming INTEGER NOT NULL CHECK (supports_streaming IN (0,1)),
  supports_tools INTEGER NOT NULL CHECK (supports_tools IN (0,1)),
  supports_json_schema INTEGER NOT NULL CHECK (supports_json_schema IN (0,1)),
  result_json TEXT NOT NULL,
  checked_at TEXT NOT NULL
);

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES ('0001_initial', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
