PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;
CREATE TABLE desktop_tasks_new (
    job_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES desktop_sessions(session_id),
    run_id TEXT UNIQUE REFERENCES runs(run_id), kind TEXT NOT NULL CHECK(kind IN ('hello','browser_form','vision_canvas')),
    lease_id TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation>=0),
    status TEXT NOT NULL CHECK(status IN ('queued','running','waiting_approval','waiting_human','succeeded','failed','cancelled')),
    real_model INTEGER NOT NULL CHECK(real_model IN (0,1)),
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    runtime_id TEXT
);
INSERT INTO desktop_tasks_new SELECT desktop_tasks.*,NULL FROM desktop_tasks;
DROP TABLE desktop_tasks;
ALTER TABLE desktop_tasks_new RENAME TO desktop_tasks;
CREATE UNIQUE INDEX desktop_one_active_task ON desktop_tasks(session_id)
    WHERE status IN ('queued','running','waiting_approval');
CREATE TABLE desktop_approvals_new (
    approval_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES desktop_tasks(job_id),
    envelope_json TEXT NOT NULL CHECK(json_valid(envelope_json)),
    action_sha256 TEXT NOT NULL CHECK(length(action_sha256)=64), expires_at REAL NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','approved','consumed','rejected','expired','revoked')),
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(job_id,action_sha256)
);
INSERT INTO desktop_approvals_new SELECT * FROM desktop_approvals;
DROP TABLE desktop_approvals;
ALTER TABLE desktop_approvals_new RENAME TO desktop_approvals;
CREATE UNIQUE INDEX desktop_one_pending_approval ON desktop_approvals(job_id)
    WHERE status IN ('pending','approved');
INSERT INTO schema_migrations VALUES(6,'scheduled_browser_vision',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
PRAGMA foreign_keys = ON;
