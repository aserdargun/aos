PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;
CREATE TABLE learning_migrations (
    version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL
);
CREATE TABLE source_binding (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    source_ref TEXT NOT NULL CHECK(length(source_ref)=64),
    source_schema_sha256 TEXT NOT NULL CHECK(length(source_schema_sha256)=64)
);
CREATE TABLE run_bindings (
    run_id TEXT PRIMARY KEY,
    run_identity_sha256 TEXT NOT NULL CHECK(length(run_identity_sha256)=64)
);
CREATE TABLE entries (
    entry_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('system1','system2')),
    kind TEXT NOT NULL CHECK(kind IN ('call_observed','verification_linked')),
    entry_json TEXT NOT NULL CHECK(json_valid(entry_json)),
    content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64)
);
CREATE INDEX ix_learning_entries_run_role ON entries(run_id,role,entry_id);
CREATE TRIGGER learning_binding_no_update BEFORE UPDATE ON source_binding BEGIN
    SELECT RAISE(ABORT,'learning source binding is immutable');
END;
CREATE TRIGGER learning_binding_no_delete BEFORE DELETE ON source_binding BEGIN
    SELECT RAISE(ABORT,'learning source binding is immutable');
END;
CREATE TRIGGER learning_run_binding_no_update BEFORE UPDATE ON run_bindings BEGIN
    SELECT RAISE(ABORT,'learning run binding is immutable');
END;
CREATE TRIGGER learning_run_binding_no_delete BEFORE DELETE ON run_bindings BEGIN
    SELECT RAISE(ABORT,'learning run binding is immutable');
END;
CREATE TRIGGER learning_run_binding_no_replace BEFORE INSERT ON run_bindings
WHEN EXISTS(SELECT 1 FROM run_bindings WHERE run_id=NEW.run_id) BEGIN
    SELECT RAISE(ABORT,'learning run binding replacement forbidden');
END;
CREATE TRIGGER learning_entries_no_update BEFORE UPDATE ON entries BEGIN
    SELECT RAISE(ABORT,'learning entries are append-only');
END;
CREATE TRIGGER learning_entries_no_delete BEFORE DELETE ON entries BEGIN
    SELECT RAISE(ABORT,'learning entries are append-only');
END;
CREATE TRIGGER learning_entries_no_replace BEFORE INSERT ON entries
WHEN EXISTS(SELECT 1 FROM entries WHERE entry_id=NEW.entry_id) BEGIN
    SELECT RAISE(ABORT,'learning entry replacement forbidden');
END;
INSERT INTO learning_migrations VALUES(1,'incremental_metadata',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
