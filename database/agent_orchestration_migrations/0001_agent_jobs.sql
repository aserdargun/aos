PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;
CREATE TABLE agent_orchestration_meta (
    version INTEGER PRIMARY KEY CHECK(version = 1),
    migration_sha256 TEXT NOT NULL CHECK(length(migration_sha256) = 64)
);
CREATE TABLE agent_registrations (
    agent_id TEXT NOT NULL,
    version TEXT NOT NULL,
    registration_json TEXT NOT NULL CHECK(json_valid(registration_json)),
    registration_sha256 TEXT NOT NULL CHECK(length(registration_sha256) = 64),
    PRIMARY KEY(agent_id, version)
);
CREATE TABLE agent_jobs (
    job_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    agent_version TEXT NOT NULL,
    request_json TEXT NOT NULL CHECK(json_valid(request_json)),
    request_sha256 TEXT NOT NULL CHECK(length(request_sha256) = 64),
    registration_sha256 TEXT NOT NULL CHECK(length(registration_sha256) = 64),
    state TEXT NOT NULL CHECK(state IN ('queued','prepare_intent','waiting_approval','prepared',
        'dispatch_intent','running','cancel_intent','cancel_requested','verifying','uncertain',
        'succeeded','failed','cancelled')),
    prepared_json TEXT CHECK(prepared_json IS NULL OR json_valid(prepared_json)),
    handle_json TEXT CHECK(handle_json IS NULL OR json_valid(handle_json)),
    observation_json TEXT CHECK(observation_json IS NULL OR json_valid(observation_json)),
    result_proof_json TEXT CHECK(result_proof_json IS NULL OR json_valid(result_proof_json)),
    cleanup_proof_json TEXT CHECK(cleanup_proof_json IS NULL OR json_valid(cleanup_proof_json)),
    reservation_held INTEGER NOT NULL CHECK(reservation_held IN (0,1)),
    revision INTEGER NOT NULL CHECK(revision >= 0),
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(agent_id, agent_version) REFERENCES agent_registrations(agent_id, version),
    CHECK(state != 'succeeded' OR (result_proof_json IS NOT NULL AND cleanup_proof_json IS NOT NULL
        AND json_extract(result_proof_json,'$.verified') = 1
        AND json_extract(result_proof_json,'$.outcome') = 'succeeded'
        AND json_extract(cleanup_proof_json,'$.verified') = 1 AND reservation_held = 0)),
    CHECK(reservation_held = 1 OR state IN ('queued','succeeded','failed','cancelled'))
);
CREATE TABLE agent_job_events (
    event_id INTEGER PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES agent_jobs(job_id),
    revision INTEGER NOT NULL,
    state TEXT NOT NULL,
    status_json TEXT NOT NULL CHECK(json_valid(status_json)),
    observed_at TEXT NOT NULL,
    UNIQUE(job_id, revision)
);
CREATE TRIGGER agent_registration_no_update BEFORE UPDATE ON agent_registrations
BEGIN SELECT RAISE(ABORT,'Agent registration is immutable'); END;
CREATE TRIGGER agent_registration_no_replace BEFORE INSERT ON agent_registrations
WHEN EXISTS(SELECT 1 FROM agent_registrations WHERE agent_id=NEW.agent_id AND version=NEW.version)
BEGIN SELECT RAISE(ABORT,'Agent registration cannot be replaced'); END;
CREATE TRIGGER agent_registration_no_delete BEFORE DELETE ON agent_registrations
BEGIN SELECT RAISE(ABORT,'Agent registration cannot be deleted'); END;
CREATE TRIGGER agent_job_identity BEFORE UPDATE ON agent_jobs
WHEN NEW.job_id IS NOT OLD.job_id OR NEW.agent_id IS NOT OLD.agent_id
    OR NEW.agent_version IS NOT OLD.agent_version OR NEW.request_json IS NOT OLD.request_json
    OR NEW.request_sha256 IS NOT OLD.request_sha256
    OR NEW.registration_sha256 IS NOT OLD.registration_sha256
    OR NEW.created_at IS NOT OLD.created_at OR NEW.revision != OLD.revision + 1
    OR OLD.state IN ('succeeded','failed','cancelled')
BEGIN SELECT RAISE(ABORT,'Agent job identity or revision is immutable'); END;
CREATE TRIGGER agent_job_no_delete BEFORE DELETE ON agent_jobs
BEGIN SELECT RAISE(ABORT,'Agent job cannot be deleted'); END;
CREATE TRIGGER agent_job_no_replace BEFORE INSERT ON agent_jobs
WHEN EXISTS(SELECT 1 FROM agent_jobs WHERE job_id=NEW.job_id)
BEGIN SELECT RAISE(ABORT,'Agent job cannot be replaced'); END;
CREATE TRIGGER agent_job_transition BEFORE UPDATE ON agent_jobs
WHEN NOT (
    (OLD.state='queued' AND NEW.state IN ('prepare_intent','cancelled'))
    OR (OLD.state='prepare_intent' AND NEW.state IN ('prepared','waiting_approval','uncertain'))
    OR (OLD.state='waiting_approval' AND NEW.state IN ('waiting_approval','prepared','cancel_intent','verifying','uncertain'))
    OR (OLD.state='prepared' AND NEW.state IN ('dispatch_intent','cancel_intent'))
    OR (OLD.state='dispatch_intent' AND NEW.state IN ('running','uncertain'))
    OR (OLD.state='running' AND NEW.state IN ('running','cancel_intent','verifying','uncertain'))
    OR (OLD.state='cancel_intent' AND NEW.state IN ('cancel_requested','uncertain'))
    OR (OLD.state='cancel_requested' AND NEW.state IN ('cancel_requested','cancel_intent','verifying','uncertain'))
    OR (OLD.state='verifying' AND NEW.state IN ('verifying','succeeded','failed','cancelled','uncertain'))
    OR (OLD.state='uncertain' AND NEW.state IN ('uncertain','verifying'))
)
BEGIN SELECT RAISE(ABORT,'Agent job transition is not allowed'); END;
CREATE TRIGGER agent_job_release BEFORE UPDATE ON agent_jobs
WHEN OLD.reservation_held=1 AND NEW.reservation_held=0 AND NOT (
    NEW.state IN ('succeeded','failed','cancelled')
    AND NEW.result_proof_json IS NOT NULL AND NEW.cleanup_proof_json IS NOT NULL
    AND json_extract(NEW.result_proof_json,'$.verified')=1
    AND json_extract(NEW.cleanup_proof_json,'$.verified')=1
    AND json_extract(NEW.result_proof_json,'$.outcome')=NEW.state
    AND json_extract(NEW.result_proof_json,'$.handle')=json(NEW.handle_json)
    AND json_extract(NEW.cleanup_proof_json,'$.handle')=json(NEW.handle_json)
)
BEGIN SELECT RAISE(ABORT,'Reservation release requires matching result and cleanup proofs'); END;
CREATE TRIGGER agent_job_event_no_update BEFORE UPDATE ON agent_job_events
BEGIN SELECT RAISE(ABORT,'Agent audit event is immutable'); END;
CREATE TRIGGER agent_job_event_no_replace BEFORE INSERT ON agent_job_events
WHEN EXISTS(SELECT 1 FROM agent_job_events WHERE event_id=NEW.event_id OR (job_id=NEW.job_id AND revision=NEW.revision))
BEGIN SELECT RAISE(ABORT,'Agent audit event cannot be replaced'); END;
CREATE TRIGGER agent_job_event_no_delete BEFORE DELETE ON agent_job_events
BEGIN SELECT RAISE(ABORT,'Agent audit event cannot be deleted'); END;
COMMIT;
