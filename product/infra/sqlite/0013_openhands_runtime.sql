-- qf:migration-foreign-keys-off

CREATE TABLE conversations_openhands_migration (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
  title TEXT NOT NULL,
  mode TEXT NOT NULL CHECK (mode IN ('MOCK','OPENHANDS','PI')),
  status TEXT NOT NULL CHECK (status IN ('DRAFT','RUNNING','PAUSED','BLOCKED','FAILED','COMPLETED','ABORTED')),
  provider TEXT,
  model_id TEXT,
  last_activity_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  archived_at TEXT,
  CHECK (mode = 'MOCK' OR (provider IS NOT NULL AND model_id IS NOT NULL))
);

INSERT INTO conversations_openhands_migration(
  id, project_id, task_id, title, mode, status, provider, model_id,
  last_activity_at, created_at, archived_at
)
SELECT
  id, project_id, task_id, title, mode, status, provider, model_id,
  last_activity_at, created_at, archived_at
FROM conversations;

DROP TABLE conversations;
ALTER TABLE conversations_openhands_migration RENAME TO conversations;

CREATE INDEX idx_conversations_project_activity
  ON conversations(project_id, last_activity_at DESC);
CREATE INDEX idx_conversations_project_archive_activity
  ON conversations(project_id, archived_at, last_activity_at DESC);

ALTER TABLE agent_sessions ADD COLUMN runtime_kind TEXT NOT NULL DEFAULT 'PI'
  CHECK (runtime_kind IN ('MOCK','OPENHANDS','PI'));
ALTER TABLE agent_sessions ADD COLUMN runtime_revision TEXT NOT NULL DEFAULT 'legacy-unversioned';
ALTER TABLE agent_sessions ADD COLUMN config_hash TEXT NOT NULL
  DEFAULT '0000000000000000000000000000000000000000000000000000000000000000'
  CHECK (length(config_hash) = 64);
ALTER TABLE agent_sessions ADD COLUMN recovery_cursor INTEGER NOT NULL DEFAULT 0
  CHECK (recovery_cursor >= 0);

UPDATE agent_sessions
SET runtime_kind = COALESCE(
      (SELECT conversations.mode FROM conversations WHERE conversations.id = agent_sessions.conversation_id),
      'PI'
    ),
    runtime_revision = CASE COALESCE(
      (SELECT conversations.mode FROM conversations WHERE conversations.id = agent_sessions.conversation_id),
      'PI'
    )
      WHEN 'MOCK' THEN 'qf-mock-v1'
      WHEN 'OPENHANDS' THEN 'openhands-unknown'
      ELSE 'pi-agent-session-legacy'
    END,
    config_hash = CASE
      WHEN length(prompt_hash) = 64 AND prompt_hash NOT GLOB '*[^0-9a-f]*' THEN prompt_hash
      ELSE '0000000000000000000000000000000000000000000000000000000000000000'
    END;

CREATE TABLE openhands_tool_call_inbox (
  conversation_id TEXT NOT NULL REFERENCES conversations(id),
  runtime_session_id TEXT NOT NULL,
  tool_call_id TEXT NOT NULL,
  tool_name TEXT NOT NULL,
  request_hash TEXT NOT NULL CHECK (
    length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'
  ),
  status TEXT NOT NULL CHECK (status IN ('RUNNING','UNKNOWN','COMPLETED','FAILED')),
  result_json TEXT,
  error_json TEXT,
  claimed_at TEXT NOT NULL,
  finished_at TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (conversation_id, runtime_session_id, tool_call_id),
  CHECK (
    (status IN ('RUNNING','UNKNOWN') AND result_json IS NULL AND error_json IS NULL AND finished_at IS NULL)
    OR (status = 'COMPLETED' AND result_json IS NOT NULL AND error_json IS NULL AND finished_at IS NOT NULL)
    OR (status = 'FAILED' AND result_json IS NULL AND error_json IS NOT NULL AND finished_at IS NOT NULL)
  )
);

CREATE INDEX idx_openhands_tool_call_inbox_recovery
  ON openhands_tool_call_inbox(conversation_id, runtime_session_id, status, updated_at);

INSERT INTO schema_migrations(version, applied_at)
VALUES ('0013_openhands_runtime', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
