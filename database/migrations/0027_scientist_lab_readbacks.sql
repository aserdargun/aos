PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;
CREATE TABLE scientist_lab_readbacks (
    readback_id TEXT PRIMARY KEY,
    local_run_id TEXT NOT NULL REFERENCES scientist_lab_jobs(run_id),
    session_id TEXT NOT NULL REFERENCES desktop_sessions(session_id),
    remote_run_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    authority_url TEXT NOT NULL,
    original_request_sha256 TEXT NOT NULL CHECK(length(original_request_sha256)=64 AND original_request_sha256 NOT GLOB '*[^0-9a-f]*'),
    current_binding_json TEXT NOT NULL CHECK(json_valid(current_binding_json)),
    report_sha256 TEXT NOT NULL CHECK(length(report_sha256)=64 AND report_sha256 NOT GLOB '*[^0-9a-f]*'),
    result_sha256 TEXT NOT NULL CHECK(length(result_sha256)=64 AND result_sha256 NOT GLOB '*[^0-9a-f]*'),
    record_json TEXT NOT NULL CHECK(json_valid(record_json) AND length(CAST(record_json AS BLOB))<=524288),
    record_sha256 TEXT NOT NULL CHECK(length(record_sha256)=64 AND record_sha256 NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL,
    UNIQUE(local_run_id,report_sha256),
    CHECK(json_extract(record_json,'$.schema_version') IS '1.0'),
    CHECK(json_extract(record_json,'$.retention_source') IS 'explicit_console_request'),
    CHECK(json_extract(record_json,'$.readback_id') IS readback_id),
    CHECK(json_extract(record_json,'$.local_run_id') IS local_run_id),
    CHECK(json_extract(record_json,'$.session_id') IS session_id),
    CHECK(json_extract(record_json,'$.remote_run_id') IS remote_run_id),
    CHECK(json_extract(record_json,'$.principal_id') IS principal_id),
    CHECK(json_extract(record_json,'$.authority_url') IS authority_url),
    CHECK(json_extract(record_json,'$.original_request_sha256') IS original_request_sha256),
    CHECK(json_extract(record_json,'$.current_binding') IS current_binding_json),
    CHECK(json_extract(record_json,'$.report_sha256') IS report_sha256),
    CHECK(json_extract(record_json,'$.result_sha256') IS result_sha256),
    CHECK(json_extract(record_json,'$.report.run_id') IS remote_run_id),
    CHECK(json_extract(record_json,'$.report.report_sha256') IS report_sha256),
    CHECK(json_extract(record_json,'$.report.report.run_id') IS remote_run_id),
    CHECK(json_extract(record_json,'$.report.report.status') IN ('completed','stopped','failed')),
    CHECK(json_extract(record_json,'$.created_at') IS created_at),
    CHECK(json_extract(current_binding_json,'$.session_id') IS session_id)
);
CREATE TRIGGER scientist_lab_readback_original BEFORE INSERT ON scientist_lab_readbacks
WHEN NOT EXISTS (
    SELECT 1 FROM scientist_lab_jobs job JOIN desktop_sessions session USING(session_id)
    WHERE job.run_id=NEW.local_run_id AND job.session_id=NEW.session_id AND job.lab_run_id=NEW.remote_run_id
      AND json_extract(job.task_json,'$.authority_url') IS NEW.authority_url
      AND json_extract(job.task_json,'$.principal_id') IS NEW.principal_id
      AND json_extract(job.task_json,'$.request.external_run_id') IS NEW.local_run_id
      AND json_extract(job.task_json,'$.binding.runtime_id') IS json_extract(NEW.current_binding_json,'$.runtime_id')
      AND json_extract(job.task_json,'$.binding.authorization_context_sha256') IS json_extract(NEW.current_binding_json,'$.authorization_context_sha256')
      AND json_extract(NEW.current_binding_json,'$.generation')>=json_extract(job.task_json,'$.binding.generation')
      AND session.status='running'
      AND session.runtime_id=json_extract(NEW.current_binding_json,'$.runtime_id')
      AND session.lease_id=json_extract(NEW.current_binding_json,'$.lease_id')
      AND session.generation=json_extract(NEW.current_binding_json,'$.generation')
      AND session.owner=json_extract(NEW.current_binding_json,'$.owner')
)
BEGIN SELECT RAISE(ABORT,'Saved Lab report requires its exact original job and current binding'); END;
CREATE TRIGGER scientist_lab_readback_bound BEFORE INSERT ON scientist_lab_readbacks
WHEN (SELECT count(*) FROM scientist_lab_readbacks WHERE local_run_id=NEW.local_run_id)>=32
BEGIN SELECT RAISE(ABORT,'Saved Lab report history reached its per-job bound'); END;
CREATE TRIGGER scientist_lab_readback_no_replace BEFORE INSERT ON scientist_lab_readbacks
WHEN EXISTS (SELECT 1 FROM scientist_lab_readbacks
             WHERE readback_id=NEW.readback_id
                OR (local_run_id=NEW.local_run_id AND report_sha256=NEW.report_sha256))
BEGIN SELECT RAISE(ABORT,'Saved Lab reports cannot replace an immutable record'); END;
CREATE TRIGGER scientist_lab_readback_no_update BEFORE UPDATE ON scientist_lab_readbacks
BEGIN SELECT RAISE(ABORT,'Saved Lab reports are append-only'); END;
CREATE TRIGGER scientist_lab_readback_no_delete BEFORE DELETE ON scientist_lab_readbacks
BEGIN SELECT RAISE(ABORT,'Saved Lab reports are append-only'); END;
INSERT INTO schema_migrations VALUES(27,'scientist_lab_readbacks',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
