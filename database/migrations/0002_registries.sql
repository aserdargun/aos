PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;
CREATE TABLE models (
    model_id TEXT PRIMARY KEY, source TEXT NOT NULL, revision TEXT, sha256 TEXT,
    backend TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
    metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json)),
    CHECK(sha256 IS NULL OR length(sha256)=64), CHECK(enabled=0 OR (revision IS NOT NULL AND sha256 IS NOT NULL))
);
CREATE TABLE adapters (
    adapter_id TEXT PRIMARY KEY, model_id TEXT NOT NULL REFERENCES models(model_id), base_sha256 TEXT NOT NULL CHECK(length(base_sha256)=64),
    sha256 TEXT NOT NULL CHECK(length(sha256)=64), compatibility TEXT NOT NULL CHECK(compatibility IN ('unknown','passed','failed')),
    metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json)), UNIQUE(model_id,adapter_id)
);
CREATE TABLE deployments (
    deployment_id TEXT PRIMARY KEY, model_id TEXT NOT NULL REFERENCES models(model_id), adapter_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('EXPERIMENTAL','CANDIDATE','VALIDATED','ACTIVE','DEPRECATED','REJECTED')),
    config_json TEXT NOT NULL CHECK(json_valid(config_json)), config_sha256 TEXT NOT NULL CHECK(length(config_sha256)=64),
    created_at TEXT NOT NULL, FOREIGN KEY(model_id,adapter_id) REFERENCES adapters(model_id,adapter_id)
);
CREATE TABLE benchmarks (
    benchmark_id TEXT PRIMARY KEY, suite_version TEXT NOT NULL, suite_sha256 TEXT NOT NULL CHECK(length(suite_sha256)=64),
    deployment_id TEXT NOT NULL REFERENCES deployments(deployment_id),
    metrics_json TEXT NOT NULL CHECK(json_valid(metrics_json)), environment_json TEXT NOT NULL CHECK(json_valid(environment_json)),
    status TEXT NOT NULL CHECK(status IN ('passed','failed','incomplete')), created_at TEXT NOT NULL
);
CREATE TABLE deployment_events (
    activation_id TEXT PRIMARY KEY, role TEXT NOT NULL CHECK(role IN ('system1','system2')),
    deployment_id TEXT NOT NULL REFERENCES deployments(deployment_id), previous_deployment_id TEXT REFERENCES deployments(deployment_id),
    event TEXT NOT NULL CHECK(event IN ('activate','rollback','deactivate')), actor TEXT NOT NULL,
    evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json)), created_at TEXT NOT NULL,
    UNIQUE(role,deployment_id,activation_id)
);
CREATE TABLE active_deployments (
    role TEXT PRIMARY KEY CHECK(role IN ('system1','system2')),
    deployment_id TEXT NOT NULL REFERENCES deployments(deployment_id), activation_id TEXT NOT NULL UNIQUE,
    FOREIGN KEY(role,deployment_id,activation_id) REFERENCES deployment_events(role,deployment_id,activation_id)
);
INSERT INTO schema_migrations VALUES(2,'registries',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
