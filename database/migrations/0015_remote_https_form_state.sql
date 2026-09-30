BEGIN IMMEDIATE;
CREATE TABLE desktop_remote_form_state_bindings (
    job_id TEXT PRIMARY KEY NOT NULL REFERENCES desktop_remote_form_bindings(job_id),
    run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id),
    form_plan_sha256 TEXT NOT NULL CHECK(length(form_plan_sha256)=64 AND form_plan_sha256 NOT GLOB '*[^0-9a-f]*'),
    state_plan_sha256 TEXT NOT NULL CHECK(length(state_plan_sha256)=64 AND state_plan_sha256 NOT GLOB '*[^0-9a-f]*'),
    state_plan_json TEXT NOT NULL CHECK(json_valid(state_plan_json)),
    browser_runtime_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK(json_extract(state_plan_json,'$.form_plan_sha256') IS form_plan_sha256),
    CHECK(json_extract(state_plan_json,'$.status') IS 'draft'),
    CHECK(json_type(state_plan_json,'$.execution_authorized') IS 'false'
          AND json_type(state_plan_json,'$.collection_authorized') IS 'false')
);
CREATE TRIGGER desktop_remote_form_state_bindings_job_match BEFORE INSERT ON desktop_remote_form_state_bindings WHEN NOT EXISTS (
    SELECT 1 FROM desktop_remote_form_bindings AS form
    JOIN desktop_tasks AS job ON job.job_id=form.job_id
    WHERE form.job_id=NEW.job_id AND form.run_id=NEW.run_id
      AND form.plan_sha256=NEW.form_plan_sha256
      AND form.browser_runtime_id=NEW.browser_runtime_id
      AND job.kind='browser_remote_form' AND job.run_id=NEW.run_id
) BEGIN SELECT RAISE(ABORT,'remote form state binding differs'); END;
CREATE TRIGGER desktop_remote_form_state_bindings_no_update BEFORE UPDATE ON desktop_remote_form_state_bindings
BEGIN SELECT RAISE(ABORT,'remote form state bindings are append-only'); END;
CREATE TRIGGER desktop_remote_form_state_bindings_no_delete BEFORE DELETE ON desktop_remote_form_state_bindings
BEGIN SELECT RAISE(ABORT,'remote form state bindings are append-only'); END;
CREATE TRIGGER desktop_remote_form_state_bindings_no_replace BEFORE INSERT ON desktop_remote_form_state_bindings WHEN
    EXISTS(SELECT 1 FROM desktop_remote_form_state_bindings WHERE job_id=NEW.job_id OR run_id=NEW.run_id)
BEGIN SELECT RAISE(ABORT,'remote form state binding replacement forbidden'); END;
INSERT INTO schema_migrations VALUES(15,'remote_https_form_state',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
