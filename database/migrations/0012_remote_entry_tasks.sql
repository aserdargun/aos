PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;
DROP TRIGGER desktop_web_profile_bindings_job_match;
DROP TRIGGER desktop_web_profile_bindings_job_immutable;
CREATE TABLE desktop_tasks_new (
    job_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES desktop_sessions(session_id),
    run_id TEXT UNIQUE REFERENCES runs(run_id), kind TEXT NOT NULL CHECK(kind IN ('hello','browser_form','vision_canvas','browser_local_navigation','browser_staging_workflow','browser_remote_entry')),
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
CREATE TRIGGER desktop_web_profile_bindings_job_match BEFORE INSERT ON desktop_web_profile_bindings WHEN NOT EXISTS (
    SELECT 1 FROM desktop_tasks AS job JOIN runs AS run ON run.run_id=NEW.run_id
    WHERE job.job_id=NEW.job_id AND job.run_id=NEW.run_id
      AND job.runtime_id=NEW.browser_runtime_id
      AND ((job.kind='browser_local_navigation' AND NEW.task_key='synthetic-local-navigation'
            AND run.policy_version='browser-local-navigation-policy-v1')
        OR (job.kind='browser_staging_workflow' AND NEW.task_key='synthetic-staging-workflow'
            AND run.policy_version='browser-staging-workflow-policy-v1'))
)
BEGIN
    SELECT RAISE(ABORT,'desktop web profile run binding differs');
END;
CREATE TRIGGER desktop_web_profile_bindings_job_immutable BEFORE UPDATE OF run_id,runtime_id,kind ON desktop_tasks WHEN
    EXISTS(SELECT 1 FROM desktop_web_profile_bindings WHERE job_id=OLD.job_id)
    AND (NEW.run_id IS NOT OLD.run_id OR NEW.runtime_id IS NOT OLD.runtime_id OR NEW.kind IS NOT OLD.kind)
BEGIN
    SELECT RAISE(ABORT,'bound desktop job identity is immutable');
END;
CREATE TABLE desktop_remote_entry_bindings (
    job_id TEXT PRIMARY KEY NOT NULL REFERENCES desktop_tasks(job_id),
    run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id),
    profile_sha256 TEXT NOT NULL CHECK(length(profile_sha256)=64 AND profile_sha256 NOT GLOB '*[^0-9a-f]*'),
    binding_sha256 TEXT NOT NULL CHECK(length(binding_sha256)=64 AND binding_sha256 NOT GLOB '*[^0-9a-f]*'),
    runtime_sha256 TEXT NOT NULL CHECK(length(runtime_sha256)=64 AND runtime_sha256 NOT GLOB '*[^0-9a-f]*'),
    draft_json TEXT NOT NULL CHECK(json_valid(draft_json)),
    browser_runtime_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK(json_type(draft_json,'$.profile_sha256') IS 'text'
          AND json_extract(draft_json,'$.profile_sha256') IS profile_sha256),
    CHECK(json_type(draft_json,'$.binding_sha256') IS 'text'
          AND json_extract(draft_json,'$.binding_sha256') IS binding_sha256),
    CHECK(json_type(draft_json,'$.runtime_sha256') IS 'text'
          AND json_extract(draft_json,'$.runtime_sha256') IS runtime_sha256),
    CHECK(json_type(draft_json,'$.runtime.runtime_id') IS 'text'
          AND json_extract(draft_json,'$.runtime.runtime_id') IS browser_runtime_id),
    CHECK(json_type(draft_json,'$.status') IS 'text'
          AND json_extract(draft_json,'$.status') IS 'draft'),
    CHECK(json_type(draft_json,'$.execution_authorized') IS 'false'
          AND json_type(draft_json,'$.collection_authorized') IS 'false')
);
CREATE TRIGGER desktop_remote_entry_bindings_job_match BEFORE INSERT ON desktop_remote_entry_bindings WHEN NOT EXISTS (
    SELECT 1 FROM desktop_tasks AS job JOIN runs AS run ON run.run_id=NEW.run_id
    WHERE job.job_id=NEW.job_id AND job.run_id=NEW.run_id
      AND job.runtime_id=NEW.browser_runtime_id
      AND job.kind='browser_remote_entry' AND run.policy_version='browser-remote-entry-policy-v1'
)
BEGIN
    SELECT RAISE(ABORT,'remote entry job or run binding differs');
END;
CREATE TRIGGER desktop_remote_entry_bindings_no_update BEFORE UPDATE ON desktop_remote_entry_bindings BEGIN
    SELECT RAISE(ABORT,'remote entry bindings are append-only');
END;
CREATE TRIGGER desktop_remote_entry_bindings_no_delete BEFORE DELETE ON desktop_remote_entry_bindings BEGIN
    SELECT RAISE(ABORT,'remote entry bindings are append-only');
END;
CREATE TRIGGER desktop_remote_entry_bindings_no_replace BEFORE INSERT ON desktop_remote_entry_bindings WHEN
    EXISTS(SELECT 1 FROM desktop_remote_entry_bindings WHERE job_id=NEW.job_id OR run_id=NEW.run_id)
BEGIN
    SELECT RAISE(ABORT,'remote entry binding replacement forbidden');
END;
CREATE TRIGGER desktop_remote_entry_bindings_job_immutable BEFORE UPDATE OF run_id,runtime_id,kind ON desktop_tasks WHEN
    EXISTS(SELECT 1 FROM desktop_remote_entry_bindings WHERE job_id=OLD.job_id)
    AND (NEW.run_id IS NOT OLD.run_id OR NEW.runtime_id IS NOT OLD.runtime_id OR NEW.kind IS NOT OLD.kind)
BEGIN
    SELECT RAISE(ABORT,'bound remote entry job identity is immutable');
END;
INSERT INTO schema_migrations VALUES(12,'remote_entry_tasks',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
PRAGMA foreign_keys = ON;
