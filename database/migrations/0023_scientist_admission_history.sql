PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;
CREATE TABLE scientist_admission_history (
    request_id TEXT PRIMARY KEY REFERENCES scientist_turn_intents(request_id),
    request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64 AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
    session_id TEXT NOT NULL REFERENCES desktop_sessions(session_id),
    intent_binding_sha256 TEXT NOT NULL CHECK(length(intent_binding_sha256)=64 AND intent_binding_sha256 NOT GLOB '*[^0-9a-f]*'),
    admission_binding_sha256 TEXT NOT NULL CHECK(length(admission_binding_sha256)=64 AND admission_binding_sha256 NOT GLOB '*[^0-9a-f]*'),
    record_sha256 TEXT NOT NULL UNIQUE CHECK(length(record_sha256)=64 AND record_sha256 NOT GLOB '*[^0-9a-f]*'),
    record_json TEXT NOT NULL CHECK(json_valid(record_json)),
    created_at TEXT NOT NULL,
    CHECK(json_extract(record_json,'$.request_id') IS request_id),
    CHECK(json_extract(record_json,'$.request_sha256') IS request_sha256),
    CHECK(json_extract(record_json,'$.session_id') IS session_id),
    CHECK(json_extract(record_json,'$.intent_binding_sha256') IS intent_binding_sha256),
    CHECK(json_extract(record_json,'$.admission_binding_sha256') IS admission_binding_sha256)
);
CREATE TRIGGER scientist_admission_history_original_intent
BEFORE INSERT ON scientist_admission_history
WHEN NOT EXISTS (
    SELECT 1 FROM scientist_turn_intents
    WHERE request_id=NEW.request_id AND request_sha256=NEW.request_sha256
      AND session_id=NEW.session_id AND state='pending' AND receipt_json IS NULL
)
BEGIN SELECT RAISE(ABORT,'Scientist admission identity requires the exact pending intent'); END;
CREATE TRIGGER scientist_admission_history_no_update
BEFORE UPDATE ON scientist_admission_history
BEGIN SELECT RAISE(ABORT,'Scientist admission identity is append-only'); END;
CREATE TRIGGER scientist_admission_history_no_delete
BEFORE DELETE ON scientist_admission_history
BEGIN SELECT RAISE(ABORT,'Scientist admission identity is append-only'); END;
INSERT INTO schema_migrations VALUES(23,'scientist_admission_history',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
