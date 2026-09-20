CREATE TABLE openhands_workspace_snapshots (
  conversation_id TEXT NOT NULL REFERENCES conversations(id),
  runtime_session_id TEXT NOT NULL,
  manifest_hash TEXT NOT NULL CHECK (
    length(manifest_hash) = 64 AND manifest_hash NOT GLOB '*[^0-9a-f]*'
  ),
  backend TEXT NOT NULL CHECK (backend = 'hardened-docker-agent-server'),
  workspace_id TEXT NOT NULL,
  snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json)),
  image_reference TEXT NOT NULL,
  image_digest TEXT NOT NULL CHECK (
    length(image_digest) = 71
    AND substr(image_digest, 1, 7) = 'sha256:'
    AND substr(image_digest, 8) NOT GLOB '*[^0-9a-f]*'
  ),
  initial_container_id TEXT NOT NULL,
  server_reused_at_creation INTEGER NOT NULL CHECK (server_reused_at_creation IN (0, 1)),
  secret_environment_names_json TEXT NOT NULL CHECK (
    json_valid(secret_environment_names_json)
    AND json_type(secret_environment_names_json) = 'array'
  ),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (conversation_id, runtime_session_id)
);

CREATE INDEX idx_openhands_workspace_runtime
ON openhands_workspace_snapshots(runtime_session_id, updated_at);

INSERT INTO schema_migrations(version, applied_at)
VALUES ('0016_openhands_workspace', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
