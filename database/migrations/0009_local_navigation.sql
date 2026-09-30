PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;
CREATE TABLE desktop_tasks_new (
    job_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES desktop_sessions(session_id),
    run_id TEXT UNIQUE REFERENCES runs(run_id), kind TEXT NOT NULL CHECK(kind IN ('hello','browser_form','vision_canvas','browser_local_navigation')),
    lease_id TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation>=0),
    status TEXT NOT NULL CHECK(status IN ('queued','running','waiting_approval','paused','waiting_human','succeeded','failed','cancelled')),
    real_model INTEGER NOT NULL CHECK(real_model IN (0,1)),
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    runtime_id TEXT
);
INSERT INTO desktop_tasks_new SELECT * FROM desktop_tasks;
DROP TABLE desktop_tasks;
ALTER TABLE desktop_tasks_new RENAME TO desktop_tasks;
CREATE UNIQUE INDEX desktop_one_active_task ON desktop_tasks(session_id)
    WHERE status IN ('queued','running','waiting_approval','paused');
INSERT INTO schema_migrations VALUES(9,'local_navigation',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
PRAGMA foreign_keys = ON;
