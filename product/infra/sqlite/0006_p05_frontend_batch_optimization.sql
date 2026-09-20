CREATE TABLE p05_tasks (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL UNIQUE REFERENCES conversations(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id) ON DELETE CASCADE,
    provider TEXT NOT NULL CHECK (provider = 'openai/getoken'),
    model_id TEXT NOT NULL CHECK (model_id = 'gpt-5.6-sol'),
    status TEXT NOT NULL CHECK (status IN (
        'CREATED', 'RUNNING', 'WAITING_PROVIDER', 'WAITING_HARDWARE',
        'COMPLETED', 'BLOCKED', 'FAILED', 'TIME_BUDGET_EXHAUSTED'
    )),
    stage TEXT NOT NULL,
    objective TEXT NOT NULL,
    task_started_at TEXT NOT NULL,
    hard_deadline_at TEXT NOT NULL,
    formal_test_sealed INTEGER NOT NULL DEFAULT 1 CHECK (formal_test_sealed = 1),
    verified_total_tokens INTEGER NOT NULL DEFAULT 0 CHECK (verified_total_tokens >= 0),
    prompt_tokens INTEGER NOT NULL DEFAULT 0 CHECK (prompt_tokens >= 0),
    completion_tokens INTEGER NOT NULL DEFAULT 0 CHECK (completion_tokens >= 0),
    cached_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cached_tokens >= 0),
    stable_concurrency INTEGER NOT NULL DEFAULT 0 CHECK (stable_concurrency >= 0),
    next_event_sequence INTEGER NOT NULL DEFAULT 1 CHECK (next_event_sequence >= 1),
    last_artifact_path TEXT,
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    error_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE p05_uploads (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    p05_task_id TEXT REFERENCES p05_tasks(id) ON DELETE CASCADE,
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
    created_at TEXT NOT NULL,
    UNIQUE(conversation_id, sha256, file_name)
);

CREATE TABLE p05_events (
    id TEXT PRIMARY KEY,
    p05_task_id TEXT NOT NULL REFERENCES p05_tasks(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    run_id TEXT,
    agent_id TEXT,
    strategy TEXT,
    summary TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(p05_task_id, sequence)
);

CREATE TABLE p05_model_calls (
    id TEXT PRIMARY KEY,
    p05_task_id TEXT NOT NULL REFERENCES p05_tasks(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    strategy TEXT NOT NULL,
    purpose TEXT NOT NULL,
    prompt_sha256 TEXT NOT NULL,
    response_sha256 TEXT,
    status TEXT NOT NULL CHECK (status IN ('STARTED', 'COMPLETED', 'FAILED')),
    http_status INTEGER,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms REAL,
    artifact_path TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(p05_task_id, prompt_sha256)
);

CREATE TABLE p05_tools (
    id TEXT PRIMARY KEY,
    p05_task_id TEXT NOT NULL REFERENCES p05_tasks(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    code_sha256 TEXT NOT NULL,
    test_sha256 TEXT NOT NULL,
    validation_json TEXT NOT NULL,
    invocation_json TEXT NOT NULL,
    relative_workspace TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(p05_task_id, name)
);

CREATE TABLE p05_vendor_candidates (
    id TEXT PRIMARY KEY,
    p05_task_id TEXT REFERENCES p05_tasks(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    repository_url TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    license_spdx TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('ACCEPT', 'REJECT', 'AUDIT_ONLY')),
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(name, commit_sha)
);

CREATE TABLE p05_batches (
    id TEXT PRIMARY KEY,
    p05_task_id TEXT NOT NULL REFERENCES p05_tasks(id) ON DELETE CASCADE,
    batch_index INTEGER NOT NULL CHECK (batch_index >= 1),
    batch_kind TEXT NOT NULL CHECK (batch_kind IN ('BASELINE_EXPLORATION', 'OPTIMIZATION', 'INDEPENDENT_CONFIRMATION')),
    status TEXT NOT NULL CHECK (status IN ('REGISTERED', 'SUBMITTING', 'SUBMITTED', 'QUERYING', 'COMPLETED', 'PARTIAL', 'FAILED')),
    shots INTEGER NOT NULL CHECK (shots > 0),
    backend TEXT NOT NULL CHECK (backend = 'tianyan176'),
    lease_key TEXT NOT NULL UNIQUE,
    receipt_json TEXT,
    metrics_json TEXT,
    created_at TEXT NOT NULL,
    submitted_at TEXT,
    completed_at TEXT,
    UNIQUE(p05_task_id, batch_index)
);

CREATE TABLE p05_circuits (
    id TEXT PRIMARY KEY,
    p05_batch_id TEXT NOT NULL REFERENCES p05_batches(id) ON DELETE CASCADE,
    circuit_index INTEGER NOT NULL CHECK (circuit_index BETWEEN 0 AND 49),
    family TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    qcis TEXT NOT NULL,
    qcis_sha256 TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    query_id TEXT UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('REGISTERED', 'SUBMITTED', 'COMPLETED', 'UNKNOWN', 'FAILED')),
    receipt_json TEXT,
    raw_result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(p05_batch_id, circuit_index),
    UNIQUE(p05_batch_id, qcis_sha256)
);

CREATE INDEX idx_p05_events_task_sequence ON p05_events(p05_task_id, sequence);
CREATE INDEX idx_p05_calls_task_status ON p05_model_calls(p05_task_id, status);
CREATE INDEX idx_p05_circuits_batch_state ON p05_circuits(p05_batch_id, state);

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES ('0006_p05_frontend_batch_optimization', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
