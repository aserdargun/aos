PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;
CREATE TABLE desktop_tasks (
    job_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES desktop_sessions(session_id),
    run_id TEXT UNIQUE REFERENCES runs(run_id), kind TEXT NOT NULL CHECK(kind='hello'),
    lease_id TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation>=0),
    status TEXT NOT NULL CHECK(status IN ('queued','running','waiting_approval','waiting_human','succeeded','failed','cancelled')),
    real_model INTEGER NOT NULL CHECK(real_model IN (0,1)),
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX desktop_one_active_task ON desktop_tasks(session_id)
    WHERE status IN ('queued','running','waiting_approval');
CREATE TABLE desktop_approvals (
    approval_id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE REFERENCES desktop_tasks(job_id),
    envelope_json TEXT NOT NULL CHECK(json_valid(envelope_json)),
    action_sha256 TEXT NOT NULL CHECK(length(action_sha256)=64), expires_at REAL NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','approved','consumed','rejected','expired','revoked')),
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
INSERT INTO schema_migrations VALUES(5,'desktop_tasks',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
