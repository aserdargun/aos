PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;
CREATE TABLE scientist_turn_intents (
    request_id TEXT PRIMARY KEY CHECK(length(request_id)=32),
    session_id TEXT NOT NULL REFERENCES desktop_sessions(session_id),
    binding_json TEXT NOT NULL CHECK(json_valid(binding_json) AND
        json_extract(binding_json,'$.session_id') IS session_id),
    request_json TEXT NOT NULL CHECK(json_valid(request_json) AND
        json_extract(request_json,'$.request_id') IS request_id),
    request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
    broker_peer_json TEXT NOT NULL CHECK(json_valid(broker_peer_json)),
    deadline REAL NOT NULL CHECK(deadline>0),
    state TEXT NOT NULL CHECK(state IN ('pending','receipt_recorded')),
    receipt_json TEXT CHECK(receipt_json IS NULL OR json_valid(receipt_json)),
    created_at TEXT NOT NULL,
    CHECK((state='pending' AND receipt_json IS NULL) OR
          (state='receipt_recorded' AND receipt_json IS NOT NULL))
);
CREATE UNIQUE INDEX scientist_one_unresolved_turn ON scientist_turn_intents(session_id);
CREATE TRIGGER scientist_turn_current_session BEFORE INSERT ON scientist_turn_intents
WHEN NOT EXISTS (
    SELECT 1 FROM desktop_sessions WHERE session_id=NEW.session_id AND status='running'
      AND runtime_id=json_extract(NEW.binding_json,'$.runtime_id')
      AND lease_id=json_extract(NEW.binding_json,'$.lease_id')
      AND generation=json_extract(NEW.binding_json,'$.generation')
      AND owner=json_extract(NEW.binding_json,'$.owner')
)
BEGIN SELECT RAISE(ABORT,'Scientist intent session fence differs'); END;
CREATE TRIGGER scientist_turn_immutable BEFORE UPDATE ON scientist_turn_intents
WHEN NEW.request_id != OLD.request_id OR NEW.session_id != OLD.session_id
    OR NEW.binding_json != OLD.binding_json OR NEW.request_json != OLD.request_json
    OR NEW.request_sha256 != OLD.request_sha256 OR NEW.broker_peer_json != OLD.broker_peer_json
    OR NEW.deadline != OLD.deadline OR NEW.created_at != OLD.created_at
    OR OLD.state != 'pending' OR NEW.state != 'receipt_recorded'
BEGIN SELECT RAISE(ABORT,'Scientist intent identity or transition is immutable'); END;
CREATE TRIGGER scientist_turn_no_delete BEFORE DELETE ON scientist_turn_intents
BEGIN SELECT RAISE(ABORT,'Scientist intent requires trusted reconciliation, not deletion'); END;
INSERT INTO schema_migrations VALUES(18,'scientist_turn_intents',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
