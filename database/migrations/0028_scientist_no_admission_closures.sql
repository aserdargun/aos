PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;
CREATE TABLE scientist_no_admission_closures (
    request_id TEXT PRIMARY KEY REFERENCES scientist_turn_intents(request_id),
    session_id TEXT NOT NULL REFERENCES desktop_sessions(session_id),
    request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64 AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
    admission_record_sha256 TEXT NOT NULL REFERENCES scientist_admission_history(record_sha256),
    intent_snapshot_json TEXT NOT NULL CHECK(json_valid(intent_snapshot_json)),
    observation_json TEXT NOT NULL CHECK(json_valid(observation_json)),
    observation_sha256 TEXT NOT NULL UNIQUE CHECK(length(observation_sha256)=64 AND observation_sha256 NOT GLOB '*[^0-9a-f]*'),
    schema_sha256 TEXT NOT NULL CHECK(schema_sha256='92791f45ef6a319a27a5aade363832b737978080826537b8a1ffa7eef700cd63'),
    recovery_scope_json TEXT NOT NULL CHECK(json_valid(recovery_scope_json)),
    created_at TEXT NOT NULL,
    CHECK(json_extract(observation_json,'$.schema')='aos-scientist-no-admission-observation.v1'),
    CHECK(json_extract(observation_json,'$.version')=1),
    CHECK(json_extract(observation_json,'$.outcome')='never_received'),
    CHECK(json_type(observation_json,'$.admission_budget')='null'),
    CHECK(json_extract(observation_json,'$.target.request_id')=request_id),
    CHECK(json_extract(observation_json,'$.target.request_sha256')=request_sha256),
    CHECK(json_extract(observation_json,'$.observation_sha256')=observation_sha256),
    CHECK(json_extract(observation_json,'$.original.admission_record_sha256')=admission_record_sha256),
    CHECK(json_extract(recovery_scope_json,'$.session_id')=session_id)
);
CREATE TRIGGER scientist_no_admission_closures_no_update
BEFORE UPDATE ON scientist_no_admission_closures
BEGIN
    SELECT RAISE(ABORT,'Scientist no-admission closure is immutable');
END;
CREATE TRIGGER scientist_no_admission_closures_no_replace
BEFORE INSERT ON scientist_no_admission_closures
WHEN EXISTS(SELECT 1 FROM scientist_no_admission_closures
            WHERE request_id=NEW.request_id OR observation_sha256=NEW.observation_sha256)
BEGIN
    SELECT RAISE(ABORT,'Scientist no-admission closure cannot be replaced');
END;
CREATE TRIGGER scientist_no_admission_closures_target
BEFORE INSERT ON scientist_no_admission_closures
WHEN NOT EXISTS (
    SELECT 1 FROM scientist_turn_intents intent
    JOIN scientist_admission_history history ON history.request_id=intent.request_id
    JOIN desktop_sessions session ON session.session_id=intent.session_id
    WHERE intent.request_id=NEW.request_id AND intent.session_id=NEW.session_id
      AND intent.request_sha256=NEW.request_sha256 AND intent.state='pending' AND intent.receipt_json IS NULL
      AND history.record_sha256=NEW.admission_record_sha256
      AND json_extract(NEW.intent_snapshot_json,'$.request_json') IS intent.request_json
      AND json_extract(NEW.intent_snapshot_json,'$.binding_json') IS intent.binding_json
      AND json_extract(NEW.observation_json,'$.cleanup_scope.session_id') IS session.session_id
      AND json_extract(NEW.observation_json,'$.cleanup_scope.runtime_id') IS session.runtime_id
      AND json_extract(NEW.observation_json,'$.cleanup_scope.current_generation') IS session.generation
      AND json_extract(NEW.observation_json,'$.cleanup_scope.lease_id') IS session.lease_id
      AND session.owner='PAUSED' AND session.status='stopped'
)
BEGIN SELECT RAISE(ABORT,'Scientist observation closure target or current recovery scope differs'); END;
CREATE TRIGGER scientist_no_admission_closures_pending_control
BEFORE INSERT ON scientist_no_admission_closures
WHEN EXISTS (
    SELECT 1 FROM scientist_evidence_controls control
    LEFT JOIN scientist_evidence_responses response ON response.control_id=control.control_id
    WHERE control.request_id=NEW.request_id AND response.control_id IS NULL
)
BEGIN SELECT RAISE(ABORT,'Scientist observation closure has unresolved control evidence'); END;
CREATE TRIGGER scientist_observed_intent_immutable BEFORE UPDATE ON scientist_turn_intents
WHEN EXISTS (SELECT 1 FROM scientist_no_admission_closures WHERE request_id=OLD.request_id)
BEGIN SELECT RAISE(ABORT,'Observed Scientist original intent is immutable'); END;
DROP TRIGGER scientist_one_unresolved_turn;
CREATE TRIGGER scientist_one_unresolved_turn BEFORE INSERT ON scientist_turn_intents
WHEN EXISTS (
    SELECT 1 FROM scientist_turn_intents intent
    LEFT JOIN scientist_turn_resolutions resolution ON resolution.request_id=intent.request_id
    LEFT JOIN scientist_no_admission_closures closure ON closure.request_id=intent.request_id
    WHERE intent.session_id=NEW.session_id AND resolution.request_id IS NULL AND closure.request_id IS NULL
)
BEGIN SELECT RAISE(ABORT,'Scientist session requires trusted resolution before new work'); END;
CREATE TRIGGER scientist_no_admission_closures_no_delete
BEFORE DELETE ON scientist_no_admission_closures
BEGIN
    SELECT RAISE(ABORT,'Scientist no-admission closure is immutable');
END;
INSERT INTO schema_migrations(version,name,applied_at)
VALUES(28,'scientist_no_admission_closures',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
