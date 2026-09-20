ALTER TABLE openhands_acceptance_campaigns
ADD COLUMN baseline_event_sequence INTEGER;

ALTER TABLE openhands_acceptance_campaigns
ADD COLUMN baseline_snapshot_hash TEXT;

ALTER TABLE openhands_acceptance_campaigns
ADD COLUMN baseline_snapshot_json TEXT;

ALTER TABLE openhands_acceptance_runs
ADD COLUMN lease_generation INTEGER NOT NULL DEFAULT 0;

ALTER TABLE openhands_acceptance_runs
ADD COLUMN lease_expires_at TEXT;

ALTER TABLE openhands_acceptance_faults
ADD COLUMN target_process_key TEXT;

ALTER TABLE openhands_acceptance_faults
ADD COLUMN before_snapshot_json TEXT;

ALTER TABLE openhands_acceptance_faults
ADD COLUMN after_snapshot_json TEXT;

ALTER TABLE openhands_acceptance_faults
ADD COLUMN verification_json TEXT;

CREATE TABLE openhands_acceptance_activity_intervals (
  run_id TEXT NOT NULL REFERENCES openhands_acceptance_runs(id),
  lease_generation INTEGER NOT NULL CHECK (lease_generation > 0),
  sequence INTEGER NOT NULL CHECK (sequence > 0),
  worker_id TEXT NOT NULL,
  activity_kind TEXT NOT NULL CHECK (activity_kind IN (
    'OPENHANDS_STATE_AUDIT',
    'QUANTUM_REPRODUCIBILITY_AUDIT',
    'EVIDENCE_INTEGRITY_AUDIT'
  )),
  started_at TEXT NOT NULL,
  ended_at TEXT NOT NULL,
  active_seconds REAL NOT NULL CHECK (active_seconds > 0),
  evidence_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (run_id, lease_generation, sequence)
);

CREATE INDEX idx_openhands_acceptance_activity_run_time
ON openhands_acceptance_activity_intervals(run_id, started_at, ended_at);

INSERT INTO schema_migrations(version, applied_at)
VALUES ('0015_openhands_acceptance_hardening', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
