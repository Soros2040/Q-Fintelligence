CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0,1)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
  title TEXT NOT NULL,
  mode TEXT NOT NULL CHECK (mode IN ('MOCK','PI')),
  status TEXT NOT NULL CHECK (status IN ('DRAFT','RUNNING','PAUSED','BLOCKED','FAILED','COMPLETED','ABORTED')),
  provider TEXT,
  model_id TEXT,
  last_activity_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  CHECK (mode = 'MOCK' OR (provider IS NOT NULL AND model_id IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS agent_sessions (
  conversation_id TEXT PRIMARY KEY REFERENCES conversations(id),
  session_id TEXT NOT NULL UNIQUE,
  relative_session_file TEXT,
  provider TEXT NOT NULL,
  model_id TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  prompt_hash TEXT NOT NULL,
  lifecycle_state TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id),
  role TEXT NOT NULL CHECK (role IN ('USER','ASSISTANT','SYSTEM')),
  content TEXT NOT NULL,
  provider TEXT,
  model_id TEXT,
  pi_message_id TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(conversation_id, pi_message_id)
);

CREATE TABLE IF NOT EXISTS conversation_artifacts (
  conversation_id TEXT NOT NULL REFERENCES conversations(id),
  sha256 TEXT NOT NULL REFERENCES artifacts(sha256),
  linked_at TEXT NOT NULL,
  PRIMARY KEY (conversation_id, sha256)
);

CREATE TABLE IF NOT EXISTS approval_requests (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id),
  conversation_id TEXT NOT NULL REFERENCES conversations(id),
  action TEXT NOT NULL CHECK (action IN ('FREEZE_PROTOCOL','NEXT_ITERATION','UNSEAL_TEST','SUBMIT_HARDWARE')),
  subject_hash TEXT NOT NULL,
  rationale TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('PENDING','APPROVED','REJECTED')),
  requested_by TEXT NOT NULL CHECK (requested_by IN ('AGENT','USER')),
  requested_at TEXT NOT NULL,
  decided_at TEXT,
  UNIQUE(conversation_id, action, subject_hash)
);

ALTER TABLE events ADD COLUMN conversation_id TEXT REFERENCES conversations(id);

CREATE INDEX IF NOT EXISTS idx_conversations_project_activity
  ON conversations(project_id, last_activity_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_conversation_time
  ON messages(conversation_id, created_at, id);
CREATE INDEX IF NOT EXISTS idx_events_conversation_sequence
  ON events(conversation_id, sequence);
CREATE INDEX IF NOT EXISTS idx_approval_requests_task_status
  ON approval_requests(task_id, status, requested_at);

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES ('0002_agent_workspace', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
