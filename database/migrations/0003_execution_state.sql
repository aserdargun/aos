PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;
CREATE TABLE runtime_states (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
    state_version INTEGER NOT NULL CHECK(state_version >= 0),
    state_json TEXT NOT NULL CHECK(json_valid(state_json))
);
CREATE TABLE action_envelopes (
    action_id TEXT PRIMARY KEY REFERENCES actions(action_id),
    envelope_json TEXT NOT NULL CHECK(json_valid(envelope_json)),
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64)
);
INSERT INTO schema_migrations VALUES(3,'execution_state',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
