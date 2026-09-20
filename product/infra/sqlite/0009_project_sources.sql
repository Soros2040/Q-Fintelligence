CREATE TABLE project_sources (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    added_from_conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
    file_name TEXT NOT NULL,
    extension TEXT NOT NULL,
    declared_media_type TEXT NOT NULL,
    detected_media_type TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    sha256 TEXT NOT NULL,
    relative_path TEXT NOT NULL UNIQUE,
    parser TEXT NOT NULL,
    risk_level TEXT NOT NULL CHECK (risk_level IN ('LOW', 'MEDIUM', 'HIGH')),
    quarantined INTEGER NOT NULL CHECK (quarantined IN (0, 1)),
    parse_result_json TEXT NOT NULL,
    archived_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id, sha256, file_name)
);

CREATE INDEX project_sources_project_created_idx
ON project_sources(project_id, archived_at, created_at DESC);

INSERT OR IGNORE INTO project_sources(
    id, project_id, added_from_conversation_id, file_name, extension,
    declared_media_type, detected_media_type, byte_size, sha256, relative_path,
    parser, risk_level, quarantined, parse_result_json, archived_at, created_at, updated_at
)
SELECT
    'project_source_legacy_' || upload.id,
    conversation.project_id,
    upload.conversation_id,
    upload.file_name,
    upload.extension,
    upload.declared_media_type,
    upload.detected_media_type,
    upload.byte_size,
    upload.sha256,
    upload.relative_path,
    upload.parser,
    upload.risk_level,
    upload.quarantined,
    upload.parse_result_json,
    NULL,
    upload.created_at,
    upload.created_at
FROM p05_uploads upload
JOIN conversations conversation ON conversation.id = upload.conversation_id;

INSERT INTO schema_migrations(version, applied_at)
VALUES ('0009_project_sources', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
