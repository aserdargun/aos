PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;
CREATE TABLE dataset_reviews (
    receipt_id TEXT PRIMARY KEY NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64 AND source_sha256 NOT GLOB '*[^0-9a-f]*'),
    candidate_sha256 TEXT NOT NULL CHECK(length(candidate_sha256)=64 AND candidate_sha256 NOT GLOB '*[^0-9a-f]*'),
    kind TEXT NOT NULL CHECK(kind IN ('system1_choice','system2_supervisor')),
    purpose TEXT NOT NULL CHECK(purpose='offline_training_text'),
    provenance TEXT NOT NULL CHECK(provenance IN ('synthetic','real')),
    usage_rights TEXT NOT NULL CHECK(usage_rights IN ('synthetic_authored','user_authorized')),
    redaction_version TEXT NOT NULL CHECK(length(redaction_version)>0),
    reviewer_id TEXT NOT NULL CHECK(length(reviewer_id)>0),
    authorization_ref TEXT NOT NULL CHECK(length(authorization_ref)>0),
    decision TEXT NOT NULL CHECK(decision IN ('accept','revoke')),
    revokes TEXT REFERENCES dataset_reviews(receipt_id),
    created_at TEXT NOT NULL,
    event_sha256 TEXT NOT NULL CHECK(length(event_sha256)=64 AND event_sha256 NOT GLOB '*[^0-9a-f]*'),
    CHECK((decision='accept' AND revokes IS NULL) OR (decision='revoke' AND revokes IS NOT NULL))
);
CREATE UNIQUE INDEX dataset_one_accept ON dataset_reviews(run_id,source_sha256,candidate_sha256,kind) WHERE decision='accept';
CREATE UNIQUE INDEX dataset_one_revocation ON dataset_reviews(revokes) WHERE revokes IS NOT NULL;
CREATE TRIGGER dataset_reviews_no_update BEFORE UPDATE ON dataset_reviews BEGIN
    SELECT RAISE(ABORT,'dataset reviews are append-only');
END;
CREATE TRIGGER dataset_reviews_no_delete BEFORE DELETE ON dataset_reviews BEGIN
    SELECT RAISE(ABORT,'dataset reviews are append-only');
END;
CREATE TRIGGER dataset_reviews_no_replace BEFORE INSERT ON dataset_reviews WHEN
    EXISTS(SELECT 1 FROM dataset_reviews WHERE receipt_id=NEW.receipt_id) OR
    (NEW.decision='accept' AND EXISTS(SELECT 1 FROM dataset_reviews WHERE decision='accept'
        AND run_id=NEW.run_id AND source_sha256=NEW.source_sha256 AND candidate_sha256=NEW.candidate_sha256 AND kind=NEW.kind)) OR
    (NEW.revokes IS NOT NULL AND EXISTS(SELECT 1 FROM dataset_reviews WHERE revokes=NEW.revokes))
BEGIN
    SELECT RAISE(ABORT,'dataset review replacement forbidden');
END;
CREATE TRIGGER dataset_reviews_revoke_binding BEFORE INSERT ON dataset_reviews WHEN NEW.decision='revoke' AND NOT EXISTS(
    SELECT 1 FROM dataset_reviews WHERE receipt_id=NEW.revokes AND decision='accept'
        AND run_id=NEW.run_id AND source_sha256=NEW.source_sha256 AND candidate_sha256=NEW.candidate_sha256
        AND kind=NEW.kind AND purpose=NEW.purpose AND provenance=NEW.provenance
        AND usage_rights=NEW.usage_rights AND redaction_version=NEW.redaction_version
)
BEGIN
    SELECT RAISE(ABORT,'dataset revocation binding differs');
END;
INSERT INTO schema_migrations VALUES(8,'dataset_reviews',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
