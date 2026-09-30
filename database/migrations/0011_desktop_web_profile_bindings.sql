PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;
CREATE TABLE desktop_web_profile_bindings (
    job_id TEXT PRIMARY KEY NOT NULL REFERENCES desktop_tasks(job_id),
    run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id),
    profile_sha256 TEXT NOT NULL CHECK(length(profile_sha256)=64 AND profile_sha256 NOT GLOB '*[^0-9a-f]*'),
    task_key TEXT NOT NULL CHECK(task_key IN ('synthetic-local-navigation','synthetic-staging-workflow')),
    pin_json TEXT NOT NULL CHECK(json_valid(pin_json)),
    pin_sha256 TEXT NOT NULL CHECK(length(pin_sha256)=64 AND pin_sha256 NOT GLOB '*[^0-9a-f]*'),
    browser_runtime_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK(json_type(pin_json,'$.profile_sha256') IS 'text'
          AND json_extract(pin_json,'$.profile_sha256') IS profile_sha256),
    CHECK(json_type(pin_json,'$.task_key') IS 'text'
          AND json_extract(pin_json,'$.task_key') IS task_key),
    CHECK(json_type(pin_json,'$.network_mode') IS 'text'
          AND json_extract(pin_json,'$.network_mode') IS 'none'),
    CHECK(json_type(pin_json,'$.collection_authorized') IS 'false')
);
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
CREATE TRIGGER desktop_web_profile_bindings_no_update BEFORE UPDATE ON desktop_web_profile_bindings BEGIN
    SELECT RAISE(ABORT,'desktop web profile bindings are append-only');
END;
CREATE TRIGGER desktop_web_profile_bindings_no_delete BEFORE DELETE ON desktop_web_profile_bindings BEGIN
    SELECT RAISE(ABORT,'desktop web profile bindings are append-only');
END;
CREATE TRIGGER desktop_web_profile_bindings_no_replace BEFORE INSERT ON desktop_web_profile_bindings WHEN
    EXISTS(SELECT 1 FROM desktop_web_profile_bindings WHERE job_id=NEW.job_id OR run_id=NEW.run_id)
BEGIN
    SELECT RAISE(ABORT,'desktop web profile binding replacement forbidden');
END;
CREATE TRIGGER desktop_web_profile_bindings_job_immutable BEFORE UPDATE OF run_id,runtime_id,kind ON desktop_tasks WHEN
    EXISTS(SELECT 1 FROM desktop_web_profile_bindings WHERE job_id=OLD.job_id)
    AND (NEW.run_id IS NOT OLD.run_id OR NEW.runtime_id IS NOT OLD.runtime_id OR NEW.kind IS NOT OLD.kind)
BEGIN
    SELECT RAISE(ABORT,'bound desktop job identity is immutable');
END;
INSERT INTO schema_migrations VALUES(11,'desktop_web_profile_bindings',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
