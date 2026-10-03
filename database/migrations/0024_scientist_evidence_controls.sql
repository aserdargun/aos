PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;
CREATE TABLE scientist_evidence_controls (
    control_id TEXT PRIMARY KEY CHECK(length(control_id)=32 AND control_id NOT GLOB '*[^0-9a-f]*'),
    request_id TEXT NOT NULL REFERENCES scientist_turn_intents(request_id),
    session_id TEXT NOT NULL REFERENCES desktop_sessions(session_id),
    admission_record_sha256 TEXT NOT NULL REFERENCES scientist_admission_history(record_sha256),
    current_binding_json TEXT NOT NULL CHECK(json_valid(current_binding_json)),
    request_json TEXT NOT NULL CHECK(json_valid(request_json)),
    request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64 AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
    broker_peer_json TEXT NOT NULL CHECK(json_valid(broker_peer_json)),
    deadline REAL NOT NULL CHECK(deadline>0),
    created_at TEXT NOT NULL,
    CHECK(json_extract(request_json,'$.control_id') IS control_id),
    CHECK(json_extract(request_json,'$.target.request_id') IS request_id),
    CHECK(json_extract(request_json,'$.schema') IS 'aos-scientist-control-evidence.v2'),
    CHECK(json_type(request_json,'$.version') IS 'integer' AND json_extract(request_json,'$.version')=2),
    CHECK(json_extract(request_json,'$.op') IN ('capability','reconcile')),
    CHECK(json_extract(current_binding_json,'$.session_id') IS session_id),
    CHECK(json_extract(current_binding_json,'$.owner') IN ('AGENT','HUMAN'))
);
CREATE TABLE scientist_evidence_responses (
    control_id TEXT PRIMARY KEY REFERENCES scientist_evidence_controls(control_id),
    request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64 AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
    response_json TEXT NOT NULL CHECK(json_valid(response_json)),
    response_sha256 TEXT NOT NULL CHECK(length(response_sha256)=64 AND response_sha256 NOT GLOB '*[^0-9a-f]*'),
    broker_peer_json TEXT NOT NULL CHECK(json_valid(broker_peer_json)),
    created_at TEXT NOT NULL,
    CHECK(json_extract(response_json,'$.control_id') IS control_id),
    CHECK(json_extract(response_json,'$.schema') IS 'aos-scientist-control-evidence.v2'),
    CHECK(json_type(response_json,'$.version') IS 'integer' AND json_extract(response_json,'$.version')=2)
);
CREATE INDEX scientist_evidence_target_controls ON scientist_evidence_controls(request_id);
CREATE TRIGGER scientist_evidence_original_target BEFORE INSERT ON scientist_evidence_controls
WHEN NOT EXISTS (
    SELECT 1 FROM scientist_turn_intents original
    JOIN scientist_admission_history admission ON admission.request_id=original.request_id
    WHERE original.request_id=NEW.request_id AND original.session_id=NEW.session_id
      AND admission.record_sha256=NEW.admission_record_sha256
      AND json_extract(admission.record_json,'$.schema_version') IS '2.0'
      AND json_extract(NEW.request_json,'$.target.request_sha256') IS original.request_sha256
      AND json_extract(NEW.request_json,'$.profile_id') IS json_extract(original.request_json,'$.profile_id')
      AND json_extract(NEW.request_json,'$.deployment_digest') IS json_extract(original.request_json,'$.deployment_digest')
      AND json_extract(NEW.current_binding_json,'$.runtime_id') IS json_extract(original.binding_json,'$.runtime_id')
)
BEGIN SELECT RAISE(ABORT,'Evidence control requires the original admission2.0 target'); END;
CREATE TRIGGER scientist_evidence_current_session BEFORE INSERT ON scientist_evidence_controls
WHEN NOT EXISTS (
    SELECT 1 FROM desktop_sessions WHERE session_id=NEW.session_id AND status='running'
      AND runtime_id=json_extract(NEW.current_binding_json,'$.runtime_id')
      AND lease_id=json_extract(NEW.current_binding_json,'$.lease_id')
      AND generation=json_extract(NEW.current_binding_json,'$.generation')
      AND owner=json_extract(NEW.current_binding_json,'$.owner')
)
BEGIN SELECT RAISE(ABORT,'Evidence control current session fence differs'); END;
CREATE TRIGGER scientist_evidence_pending_target BEFORE INSERT ON scientist_evidence_controls
WHEN EXISTS (
    SELECT 1 FROM scientist_evidence_controls control
    LEFT JOIN scientist_evidence_responses response ON response.control_id=control.control_id
    WHERE control.request_id=NEW.request_id AND response.control_id IS NULL
)
BEGIN SELECT RAISE(ABORT,'Evidence target has an unresolved control exchange'); END;
CREATE TRIGGER scientist_evidence_response_original BEFORE INSERT ON scientist_evidence_responses
WHEN NOT EXISTS (
    SELECT 1 FROM scientist_evidence_controls control
    JOIN desktop_sessions session ON session.session_id=control.session_id
    WHERE control.control_id=NEW.control_id AND control.request_sha256=NEW.request_sha256
      AND control.broker_peer_json=NEW.broker_peer_json
      AND json_extract(control.request_json,'$.op') IS json_extract(NEW.response_json,'$.op')
      AND session.status='running'
      AND session.runtime_id=json_extract(control.current_binding_json,'$.runtime_id')
      AND session.lease_id=json_extract(control.current_binding_json,'$.lease_id')
      AND session.generation=json_extract(control.current_binding_json,'$.generation')
      AND session.owner=json_extract(control.current_binding_json,'$.owner')
)
BEGIN SELECT RAISE(ABORT,'Evidence response requires its exact original control and current fence'); END;
CREATE TRIGGER scientist_evidence_controls_no_update BEFORE UPDATE ON scientist_evidence_controls
BEGIN SELECT RAISE(ABORT,'Evidence controls are append-only'); END;
CREATE TRIGGER scientist_evidence_controls_no_delete BEFORE DELETE ON scientist_evidence_controls
BEGIN SELECT RAISE(ABORT,'Evidence controls are append-only'); END;
CREATE TRIGGER scientist_evidence_responses_no_update BEFORE UPDATE ON scientist_evidence_responses
BEGIN SELECT RAISE(ABORT,'Evidence responses are append-only'); END;
CREATE TRIGGER scientist_evidence_responses_no_delete BEFORE DELETE ON scientist_evidence_responses
BEGIN SELECT RAISE(ABORT,'Evidence responses are append-only'); END;
INSERT INTO schema_migrations VALUES(24,'scientist_evidence_controls',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
