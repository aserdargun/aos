PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;
CREATE TABLE owned_parameter_skill_review_history (
    record_sha256 TEXT PRIMARY KEY CHECK(length(record_sha256)=64 AND record_sha256 NOT GLOB '*[^0-9a-f]*'),
    review_directory_sha256 TEXT NOT NULL CHECK(length(review_directory_sha256)=64 AND review_directory_sha256 NOT GLOB '*[^0-9a-f]*'),
    review_sha256 TEXT NOT NULL REFERENCES owned_parameter_skill_review_history(record_sha256),
    candidate_sha256 TEXT NOT NULL CHECK(length(candidate_sha256)=64 AND candidate_sha256 NOT GLOB '*[^0-9a-f]*'),
    source_fingerprint_sha256 TEXT NOT NULL CHECK(length(source_fingerprint_sha256)=64 AND source_fingerprint_sha256 NOT GLOB '*[^0-9a-f]*'),
    stage TEXT NOT NULL CHECK(stage IN ('accept','revoke')),
    record_json TEXT NOT NULL CHECK(json_valid(record_json)),
    created_at TEXT NOT NULL,
    UNIQUE(review_directory_sha256,review_sha256,stage),
    CHECK(json_extract(record_json,'$.review_directory_sha256') IS review_directory_sha256),
    CHECK(json_extract(record_json,'$.candidate_sha256') IS candidate_sha256),
    CHECK(json_extract(record_json,'$.source_fingerprint_sha256') IS source_fingerprint_sha256),
    CHECK((stage='accept' AND review_sha256=record_sha256
        AND json_extract(record_json,'$.kind')='owned_parameter_skill_manual_review'
        AND json_type(record_json,'$.reviewed')='true')
      OR (stage='revoke' AND review_sha256!=record_sha256
        AND json_extract(record_json,'$.kind')='owned_parameter_skill_manual_review_revocation'
        AND json_extract(record_json,'$.review_sha256') IS review_sha256
        AND json_type(record_json,'$.reviewed')='false'))
);
CREATE TRIGGER owned_parameter_skill_review_history_revoke_binding
BEFORE INSERT ON owned_parameter_skill_review_history WHEN NEW.stage='revoke'
AND NOT EXISTS (
    SELECT 1 FROM owned_parameter_skill_review_history
    WHERE record_sha256=NEW.review_sha256 AND stage='accept'
      AND review_directory_sha256=NEW.review_directory_sha256
      AND candidate_sha256=NEW.candidate_sha256
      AND source_fingerprint_sha256=NEW.source_fingerprint_sha256
)
BEGIN SELECT RAISE(ABORT,'Manual review revocation history binding differs'); END;
CREATE TRIGGER owned_parameter_skill_review_history_no_update
BEFORE UPDATE ON owned_parameter_skill_review_history
BEGIN SELECT RAISE(ABORT,'Manual review history is append-only'); END;
CREATE TRIGGER owned_parameter_skill_review_history_no_delete
BEFORE DELETE ON owned_parameter_skill_review_history
BEGIN SELECT RAISE(ABORT,'Manual review history is append-only'); END;
INSERT INTO schema_migrations VALUES(21,'owned_parameter_skill_review_history',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
