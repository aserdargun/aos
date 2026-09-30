PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;
CREATE TABLE entries_v2 (
    entry_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('system1','system2')),
    kind TEXT NOT NULL CHECK(kind IN ('call_observed','verification_linked','downstream_verification_linked')),
    entry_json TEXT NOT NULL CHECK(json_valid(entry_json)),
    content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64)
);
INSERT INTO entries_v2 SELECT entry_id,run_id,role,kind,entry_json,content_sha256 FROM entries;
DROP TABLE entries;
ALTER TABLE entries_v2 RENAME TO entries;
CREATE INDEX ix_learning_entries_run_role ON entries(run_id,role,entry_id);
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
INSERT INTO learning_migrations VALUES(2,'downstream_metadata',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
