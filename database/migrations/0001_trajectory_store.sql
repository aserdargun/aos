-- AOS schema v1. One-shot migration; execute on a fresh connection outside a transaction.
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
BEGIN IMMEDIATE;
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL
);
CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY, original_goal TEXT NOT NULL, normalized_goal TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT 'tr', success_criteria_json TEXT NOT NULL CHECK(json_valid(success_criteria_json)),
    workspace_scope_json TEXT NOT NULL CHECK(json_valid(workspace_scope_json)), created_at TEXT NOT NULL
);
CREATE TABLE runs (
    run_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
    status TEXT NOT NULL CHECK(status IN ('created','running','paused','waiting_human','succeeded','failed','cancelled')),
    outcome TEXT NOT NULL DEFAULT 'unknown' CHECK(outcome IN ('passed','failed','unknown')),
    policy_version TEXT NOT NULL, environment_json TEXT NOT NULL CHECK(json_valid(environment_json)),
    deployment_snapshot_json TEXT NOT NULL CHECK(json_valid(deployment_snapshot_json)),
    started_at TEXT NOT NULL, ended_at TEXT,
    training_eligible INTEGER NOT NULL DEFAULT 0 CHECK(training_eligible IN (0,1))
);
CREATE TABLE steps (
    step_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id), ordinal INTEGER NOT NULL CHECK(ordinal>=0),
    state TEXT NOT NULL, retry_count INTEGER NOT NULL DEFAULT 0 CHECK(retry_count>=0),
    started_at TEXT NOT NULL, ended_at TEXT, UNIQUE(run_id,ordinal), UNIQUE(run_id,step_id)
);
CREATE TABLE state_snapshots (
    snapshot_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, step_id TEXT NOT NULL,
    state_version INTEGER NOT NULL CHECK(state_version>=0), state_json TEXT NOT NULL CHECK(json_valid(state_json)),
    content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64), created_at TEXT NOT NULL,
    UNIQUE(run_id,step_id,snapshot_id),
    FOREIGN KEY(run_id,step_id) REFERENCES steps(run_id,step_id)
);
CREATE TABLE model_calls (
    call_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, step_id TEXT NOT NULL,
    deployment_id TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('system1','system2')),
    request_json TEXT NOT NULL CHECK(json_valid(request_json)), response_json TEXT CHECK(response_json IS NULL OR json_valid(response_json)),
    status TEXT NOT NULL CHECK(status IN ('ok','error','timeout','cancelled')),
    latency_ms REAL CHECK(latency_ms>=0), input_tokens INTEGER CHECK(input_tokens>=0), output_tokens INTEGER CHECK(output_tokens>=0),
    peak_vram_bytes INTEGER CHECK(peak_vram_bytes>=0), created_at TEXT NOT NULL,
    UNIQUE(run_id,step_id,call_id), FOREIGN KEY(run_id,step_id) REFERENCES steps(run_id,step_id)
);
CREATE TABLE decisions (
    decision_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, step_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, call_id TEXT,
    question TEXT NOT NULL, options_json TEXT NOT NULL CHECK(json_valid(options_json)),
    probabilities_json TEXT NOT NULL CHECK(json_valid(probabilities_json)), selected_option TEXT NOT NULL,
    confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
    policy_result TEXT NOT NULL CHECK(policy_result IN ('allow','deny','approval','escalate')),
    created_at TEXT NOT NULL, UNIQUE(run_id,step_id,decision_id),
    FOREIGN KEY(run_id,step_id) REFERENCES steps(run_id,step_id),
    FOREIGN KEY(run_id,step_id,snapshot_id) REFERENCES state_snapshots(run_id,step_id,snapshot_id),
    FOREIGN KEY(run_id,step_id,call_id) REFERENCES model_calls(run_id,step_id,call_id)
);
CREATE TABLE actions (
    action_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, step_id TEXT NOT NULL, decision_id TEXT,
    idempotency_key TEXT NOT NULL, tool TEXT NOT NULL, arguments_json TEXT NOT NULL CHECK(json_valid(arguments_json)),
    status TEXT NOT NULL CHECK(status IN ('intent','running','ok','error','denied','cancelled','uncertain')),
    actual_option TEXT, error_code TEXT, result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
    created_at TEXT NOT NULL, completed_at TEXT,
    UNIQUE(run_id,idempotency_key), UNIQUE(run_id,step_id,action_id),
    FOREIGN KEY(run_id,step_id) REFERENCES steps(run_id,step_id),
    FOREIGN KEY(run_id,step_id,decision_id) REFERENCES decisions(run_id,step_id,decision_id)
);
CREATE TABLE observations (
    observation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, step_id TEXT NOT NULL, action_id TEXT,
    kind TEXT NOT NULL, payload_json TEXT NOT NULL CHECK(json_valid(payload_json)), created_at TEXT NOT NULL,
    UNIQUE(run_id,step_id,observation_id), FOREIGN KEY(run_id,step_id) REFERENCES steps(run_id,step_id),
    FOREIGN KEY(run_id,step_id,action_id) REFERENCES actions(run_id,step_id,action_id)
);
CREATE TABLE verifications (
    verification_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, step_id TEXT NOT NULL, action_id TEXT NOT NULL,
    criterion TEXT NOT NULL, method TEXT NOT NULL, result TEXT NOT NULL CHECK(result IN ('passed','failed','unknown')),
    expected_json TEXT NOT NULL CHECK(json_valid(expected_json)), actual_json TEXT NOT NULL CHECK(json_valid(actual_json)),
    evidence_refs_json TEXT NOT NULL CHECK(json_valid(evidence_refs_json)), verifier TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE(run_id,step_id,verification_id), FOREIGN KEY(run_id,step_id) REFERENCES steps(run_id,step_id),
    FOREIGN KEY(run_id,step_id,action_id) REFERENCES actions(run_id,step_id,action_id)
);
CREATE TABLE supervisor_escalations (
    escalation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, step_id TEXT NOT NULL, call_id TEXT,
    reason TEXT NOT NULL, evidence_refs_json TEXT NOT NULL CHECK(json_valid(evidence_refs_json)),
    diagnosis TEXT, corrected_plan_json TEXT CHECK(corrected_plan_json IS NULL OR json_valid(corrected_plan_json)),
    outcome TEXT NOT NULL CHECK(outcome IN ('pending','recovered','failed','human_required')), created_at TEXT NOT NULL,
    FOREIGN KEY(run_id,step_id) REFERENCES steps(run_id,step_id),
    FOREIGN KEY(run_id,step_id,call_id) REFERENCES model_calls(run_id,step_id,call_id)
);
CREATE TABLE human_interventions (
    intervention_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, step_id TEXT NOT NULL, actor TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('approve','reject','correction','take_control','return_control','pause','resume','stop')),
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)), created_at TEXT NOT NULL,
    FOREIGN KEY(run_id,step_id) REFERENCES steps(run_id,step_id)
);
CREATE TABLE artifacts (
    artifact_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, step_id TEXT NOT NULL,
    relative_path TEXT NOT NULL, media_type TEXT NOT NULL, sha256 TEXT NOT NULL CHECK(length(sha256)=64),
    size_bytes INTEGER NOT NULL CHECK(size_bytes>=0), redaction_status TEXT NOT NULL CHECK(redaction_status IN ('raw','redacted','quarantined')),
    created_at TEXT NOT NULL, FOREIGN KEY(run_id,step_id) REFERENCES steps(run_id,step_id)
);
CREATE TABLE trajectory_labels (
    label_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, step_id TEXT NOT NULL, decision_id TEXT, verification_id TEXT,
    label_type TEXT NOT NULL CHECK(label_type IN ('choice','supervisor_recovery','policy')),
    label_json TEXT NOT NULL CHECK(json_valid(label_json)), source TEXT NOT NULL CHECK(source IN ('verified_outcome','human_review','supervisor_verified')),
    review_status TEXT NOT NULL CHECK(review_status IN ('pending','accepted','rejected')),
    created_at TEXT NOT NULL, FOREIGN KEY(run_id,step_id) REFERENCES steps(run_id,step_id),
    FOREIGN KEY(run_id,step_id,decision_id) REFERENCES decisions(run_id,step_id,decision_id),
    FOREIGN KEY(run_id,step_id,verification_id) REFERENCES verifications(run_id,step_id,verification_id)
);
CREATE INDEX ix_runs_task ON runs(task_id,started_at);
CREATE INDEX ix_actions_run_status ON actions(run_id,status);
CREATE INDEX ix_labels_review ON trajectory_labels(review_status,label_type);
INSERT INTO schema_migrations VALUES(1,'trajectory_store',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
