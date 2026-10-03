PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;
CREATE TABLE owned_parameter_skill_release_history (
    record_sha256 TEXT PRIMARY KEY CHECK(length(record_sha256)=64 AND record_sha256 NOT GLOB '*[^0-9a-f]*'),
    release_directory_sha256 TEXT NOT NULL CHECK(length(release_directory_sha256)=64 AND release_directory_sha256 NOT GLOB '*[^0-9a-f]*'),
    family_sha256 TEXT NOT NULL CHECK(length(family_sha256)=64 AND family_sha256 NOT GLOB '*[^0-9a-f]*'),
    stage TEXT NOT NULL CHECK(stage IN ('release','selection')),
    sequence INTEGER NOT NULL CHECK(sequence BETWEEN 1 AND 64),
    review_sha256 TEXT NOT NULL REFERENCES owned_parameter_skill_review_history(record_sha256),
    release_sha256 TEXT NOT NULL REFERENCES owned_parameter_skill_release_history(record_sha256),
    record_json TEXT NOT NULL CHECK(json_valid(record_json)),
    created_at TEXT NOT NULL,
    UNIQUE(release_directory_sha256,family_sha256,stage,sequence),
    CHECK(json_extract(record_json,'$.release_directory_sha256') IS release_directory_sha256),
    CHECK(json_extract(record_json,'$.family_sha256') IS family_sha256),
    CHECK(json_extract(record_json,'$.review_sha256') IS review_sha256),
    CHECK((stage='release' AND release_sha256=record_sha256
        AND json_extract(record_json,'$.kind')='owned_parameter_skill_manual_release'
        AND json_extract(record_json,'$.revision') IS sequence)
      OR (stage='selection' AND release_sha256!=record_sha256
        AND json_extract(record_json,'$.kind')='owned_parameter_skill_manual_selection'
        AND json_extract(record_json,'$.release_sha256') IS release_sha256
        AND json_extract(record_json,'$.sequence') IS sequence))
);
CREATE TRIGGER owned_parameter_skill_release_history_accepted_review
BEFORE INSERT ON owned_parameter_skill_release_history
WHEN NOT EXISTS (SELECT 1 FROM owned_parameter_skill_review_history
    WHERE record_sha256=NEW.review_sha256 AND stage='accept')
 OR EXISTS (SELECT 1 FROM owned_parameter_skill_review_history
    WHERE review_sha256=NEW.review_sha256 AND stage='revoke')
BEGIN SELECT RAISE(ABORT,'Manual release requires a current accepted review'); END;
CREATE TRIGGER owned_parameter_skill_release_history_no_update
BEFORE UPDATE ON owned_parameter_skill_release_history
BEGIN SELECT RAISE(ABORT,'Manual release history is append-only'); END;
CREATE TRIGGER owned_parameter_skill_release_history_no_delete
BEFORE DELETE ON owned_parameter_skill_release_history
BEGIN SELECT RAISE(ABORT,'Manual release history is append-only'); END;
INSERT INTO schema_migrations VALUES(22,'owned_parameter_skill_release_history',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
