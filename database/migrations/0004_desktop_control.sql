PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;
CREATE TABLE desktop_sessions (
    session_id TEXT PRIMARY KEY, runtime_id TEXT NOT NULL, image_id TEXT NOT NULL,
    owner TEXT NOT NULL CHECK(owner IN ('AGENT','HUMAN','PAUSED')),
    lease_id TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation >= 0),
    status TEXT NOT NULL CHECK(status IN ('running','paused','stopped')),
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE desktop_inputs (
    input_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES desktop_sessions(session_id),
    lease_id TEXT NOT NULL, generation INTEGER NOT NULL, tool TEXT NOT NULL CHECK(tool='type_note'),
    status TEXT NOT NULL CHECK(status IN ('queued','running','ok','cancelled','uncertain')),
    result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)), created_at TEXT NOT NULL
);
CREATE TABLE desktop_events (
    event_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES desktop_sessions(session_id),
    kind TEXT NOT NULL, payload_json TEXT NOT NULL CHECK(json_valid(payload_json)), created_at TEXT NOT NULL
);
INSERT INTO schema_migrations VALUES(4,'desktop_control',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
