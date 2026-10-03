PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;
CREATE TABLE scientist_turn_resolutions (
    request_id TEXT PRIMARY KEY REFERENCES scientist_turn_intents(request_id),
    session_id TEXT NOT NULL REFERENCES desktop_sessions(session_id),
    admission_record_sha256 TEXT NOT NULL REFERENCES scientist_admission_history(record_sha256),
    control_id TEXT NOT NULL UNIQUE REFERENCES scientist_evidence_responses(control_id),
    response_sha256 TEXT NOT NULL CHECK(length(response_sha256)=64 AND response_sha256 NOT GLOB '*[^0-9a-f]*'),
    current_binding_json TEXT NOT NULL CHECK(json_valid(current_binding_json)),
    terminal_json TEXT NOT NULL CHECK(json_valid(terminal_json)),
    terminal_receipt_sha256 TEXT NOT NULL CHECK(length(terminal_receipt_sha256)=64 AND terminal_receipt_sha256 NOT GLOB '*[^0-9a-f]*'),
    budget_witness_json TEXT NOT NULL CHECK(json_valid(budget_witness_json)),
    budget_witness_sha256 TEXT NOT NULL CHECK(length(budget_witness_sha256)=64 AND budget_witness_sha256 NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL,
    CHECK(json_extract(current_binding_json,'$.session_id') IS session_id),
    CHECK(json_extract(current_binding_json,'$.owner') IN ('AGENT','HUMAN')),
    CHECK(json_extract(terminal_json,'$.request_id') IS request_id),
    CHECK(json_extract(terminal_json,'$.receipt_sha256') IS terminal_receipt_sha256),
    CHECK(json_extract(budget_witness_json,'$.target.request_id') IS request_id)
);
CREATE TRIGGER scientist_resolution_original BEFORE INSERT ON scientist_turn_resolutions
WHEN NOT EXISTS (
    SELECT 1 FROM scientist_turn_intents intent
    JOIN scientist_admission_history admission ON admission.request_id=intent.request_id
    JOIN scientist_evidence_controls control ON control.request_id=intent.request_id
    JOIN scientist_evidence_responses response ON response.control_id=control.control_id
    JOIN desktop_sessions session ON session.session_id=intent.session_id
    WHERE intent.request_id=NEW.request_id AND intent.session_id=NEW.session_id
      AND admission.record_sha256=NEW.admission_record_sha256
      AND json_extract(admission.record_json,'$.schema_version') IS '2.0'
      AND control.control_id=NEW.control_id AND control.admission_record_sha256=admission.record_sha256
      AND response.request_sha256=control.request_sha256 AND response.broker_peer_json=control.broker_peer_json
      AND response.response_sha256=NEW.response_sha256
      AND json_extract(control.request_json,'$.schema') IS 'aos-scientist-control-evidence.v3'
      AND json_extract(control.request_json,'$.version') IS 3
      AND json_extract(control.request_json,'$.op') IS 'reconcile'
      AND json_extract(response.response_json,'$.schema') IS 'aos-scientist-control-evidence.v3'
      AND json_extract(response.response_json,'$.version') IS 3
      AND json_extract(response.response_json,'$.op') IS 'reconcile'
      AND json_extract(response.response_json,'$.ok') IS 1
      AND json_extract(response.response_json,'$.data.evidence.terminal_canonical') IS NEW.terminal_json
      AND json_extract(response.response_json,'$.data.original_budget_witness') IS NEW.budget_witness_json
      AND json_extract(NEW.terminal_json,'$.request_sha256') IS intent.request_sha256
      AND json_extract(NEW.terminal_json,'$.admission_binding_sha256') IS admission.admission_binding_sha256
      AND json_extract(NEW.terminal_json,'$.release_outcome') IN ('released','recovered_released','never_admitted')
      AND json_extract(NEW.budget_witness_json,'$.original_admission_binding_sha256') IS admission.admission_binding_sha256
      AND json_extract(NEW.current_binding_json,'$.runtime_id') IS json_extract(intent.binding_json,'$.runtime_id')
      AND session.status='running'
      AND session.runtime_id=json_extract(NEW.current_binding_json,'$.runtime_id')
      AND session.lease_id=json_extract(NEW.current_binding_json,'$.lease_id')
      AND session.generation=json_extract(NEW.current_binding_json,'$.generation')
      AND session.owner=json_extract(NEW.current_binding_json,'$.owner')
)
BEGIN SELECT RAISE(ABORT,'Resolution requires exact original admission, retained ACK and current fence'); END;
CREATE TRIGGER scientist_resolution_pending_control BEFORE INSERT ON scientist_turn_resolutions
WHEN EXISTS (
    SELECT 1 FROM scientist_evidence_controls control
    LEFT JOIN scientist_evidence_responses response ON response.control_id=control.control_id
    WHERE control.request_id=NEW.request_id AND response.control_id IS NULL
)
BEGIN SELECT RAISE(ABORT,'Resolution target has an unresolved control exchange'); END;
CREATE TRIGGER scientist_resolution_no_update BEFORE UPDATE ON scientist_turn_resolutions
BEGIN SELECT RAISE(ABORT,'Scientist resolutions are append-only'); END;
CREATE TRIGGER scientist_resolution_no_delete BEFORE DELETE ON scientist_turn_resolutions
BEGIN SELECT RAISE(ABORT,'Scientist resolutions are append-only'); END;
CREATE TRIGGER scientist_resolved_intent_immutable BEFORE UPDATE ON scientist_turn_intents
WHEN EXISTS (SELECT 1 FROM scientist_turn_resolutions WHERE request_id=OLD.request_id)
BEGIN SELECT RAISE(ABORT,'Resolved Scientist original intent and receipt are immutable'); END;
DROP INDEX scientist_one_unresolved_turn;
CREATE INDEX scientist_session_turns ON scientist_turn_intents(session_id);
CREATE TRIGGER scientist_one_unresolved_turn BEFORE INSERT ON scientist_turn_intents
WHEN EXISTS (
    SELECT 1 FROM scientist_turn_intents intent
    LEFT JOIN scientist_turn_resolutions resolution ON resolution.request_id=intent.request_id
    WHERE intent.session_id=NEW.session_id AND resolution.request_id IS NULL
)
BEGIN SELECT RAISE(ABORT,'Scientist session requires trusted resolution before new work'); END;
INSERT INTO schema_migrations VALUES(26,'scientist_turn_resolutions',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
