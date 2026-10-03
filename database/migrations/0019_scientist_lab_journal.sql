PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;
CREATE TABLE scientist_lab_jobs (
    run_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES desktop_sessions(session_id),
    task_json TEXT NOT NULL CHECK(json_valid(task_json)),
    lab_run_id TEXT UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE scientist_lab_actions (
    action_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES scientist_lab_jobs(run_id),
    task_json TEXT NOT NULL CHECK(json_valid(task_json)),
    action_json TEXT NOT NULL CHECK(json_valid(action_json)),
    body TEXT NOT NULL CHECK(json_valid(body)),
    expires_at REAL NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending','approved','rejected','intent','acknowledged')),
    approver TEXT,
    result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
    created_at TEXT NOT NULL,
    CHECK((state='pending' AND approver IS NULL AND result_json IS NULL) OR
          (state IN ('approved','rejected','intent') AND approver IS NOT NULL AND result_json IS NULL) OR
          (state='acknowledged' AND approver IS NOT NULL AND result_json IS NOT NULL))
);
CREATE UNIQUE INDEX scientist_lab_one_open_action ON scientist_lab_actions(run_id)
    WHERE state IN ('pending','approved','intent');
CREATE TRIGGER scientist_lab_action_immutable BEFORE UPDATE ON scientist_lab_actions
WHEN NEW.action_id IS NOT OLD.action_id OR NEW.run_id IS NOT OLD.run_id
  OR NEW.task_json IS NOT OLD.task_json OR NEW.action_json IS NOT OLD.action_json
  OR NEW.body IS NOT OLD.body OR NEW.expires_at IS NOT OLD.expires_at
  OR NEW.created_at IS NOT OLD.created_at
  OR NOT ((OLD.state='pending' AND NEW.state IN ('approved','rejected'))
       OR (OLD.state='approved' AND NEW.state='intent' AND NEW.approver IS OLD.approver)
       OR (OLD.state='intent' AND NEW.state='acknowledged' AND NEW.approver IS OLD.approver))
BEGIN SELECT RAISE(ABORT,'Lab action identity or transition differs'); END;
CREATE TRIGGER scientist_lab_action_no_delete BEFORE DELETE ON scientist_lab_actions
BEGIN SELECT RAISE(ABORT,'Lab action audit cannot be deleted'); END;
CREATE TRIGGER scientist_lab_job_immutable BEFORE UPDATE ON scientist_lab_jobs
WHEN NEW.run_id IS NOT OLD.run_id OR NEW.session_id IS NOT OLD.session_id
  OR NEW.task_json IS NOT OLD.task_json OR NEW.created_at IS NOT OLD.created_at
  OR OLD.lab_run_id IS NOT NULL OR NEW.lab_run_id IS NULL
BEGIN SELECT RAISE(ABORT,'Lab remote binding is immutable'); END;
CREATE TRIGGER scientist_lab_job_no_delete BEFORE DELETE ON scientist_lab_jobs
BEGIN SELECT RAISE(ABORT,'Lab job audit cannot be deleted'); END;
INSERT INTO schema_migrations VALUES(19,'scientist_lab_journal',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
