ALTER TABLE conversations ADD COLUMN archived_at TEXT;

CREATE INDEX idx_conversations_project_archive_activity
ON conversations(project_id, archived_at, last_activity_at DESC);

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES ('0010_history_archive', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
