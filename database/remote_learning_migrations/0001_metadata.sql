PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;
CREATE TABLE remote_learning_migrations (
    version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL
);
CREATE TABLE source_binding (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    source_ref TEXT NOT NULL CHECK(length(source_ref)=64),
    source_schema_sha256 TEXT NOT NULL CHECK(length(source_schema_sha256)=64),
    run_id TEXT NOT NULL,
    profile_sha256 TEXT NOT NULL CHECK(length(profile_sha256)=64),
    plan_sha256 TEXT NOT NULL CHECK(length(plan_sha256)=64),
    binding_sha256 TEXT NOT NULL CHECK(length(binding_sha256)=64),
    consent_sha256 TEXT NOT NULL CHECK(length(consent_sha256)=64),
    run_identity_sha256 TEXT NOT NULL CHECK(length(run_identity_sha256)=64)
);
CREATE TABLE entries (
    entry_id TEXT PRIMARY KEY,
    role TEXT NOT NULL CHECK(role IN ('system1','system2')),
    source_event_id TEXT NOT NULL,
    entry_json TEXT NOT NULL CHECK(json_valid(entry_json)),
    content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64)
);
CREATE INDEX ix_remote_learning_role ON entries(role,entry_id);
CREATE TRIGGER remote_learning_binding_no_update BEFORE UPDATE ON source_binding BEGIN
    SELECT RAISE(ABORT,'remote learning source binding is immutable');
END;
CREATE TRIGGER remote_learning_binding_no_delete BEFORE DELETE ON source_binding BEGIN
    SELECT RAISE(ABORT,'remote learning source binding is immutable');
END;
CREATE TRIGGER remote_learning_entries_no_update BEFORE UPDATE ON entries BEGIN
    SELECT RAISE(ABORT,'remote learning entries are append-only');
END;
CREATE TRIGGER remote_learning_entries_no_delete BEFORE DELETE ON entries BEGIN
    SELECT RAISE(ABORT,'remote learning entries are append-only');
END;
CREATE TRIGGER remote_learning_entries_no_replace BEFORE INSERT ON entries
WHEN EXISTS(SELECT 1 FROM entries WHERE entry_id=NEW.entry_id) BEGIN
    SELECT RAISE(ABORT,'remote learning entry replacement forbidden');
END;
INSERT INTO remote_learning_migrations VALUES(1,'metadata',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
