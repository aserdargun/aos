from dataclasses import dataclass
import math
import sqlite3
import time

from .contracts import canonical, digest
from .dataset import validate_record, validator
from .storage import TrajectoryStore


SOURCE_TABLES = ("steps", "state_snapshots", "runtime_states", "model_calls", "decisions", "actions",
                 "observations", "verifications", "supervisor_escalations", "human_interventions", "artifacts", "trajectory_labels")
SYNTHETIC_POLICIES = {"hello-policy-v1", "browser-form-policy-v1", "vision-canvas-policy-v1"}


@dataclass(frozen=True)
class ReviewAuthority:
    event_sha256: str
    expires_at: float
    monotonic_deadline: float | None = None


def authority_current(authority: ReviewAuthority) -> bool:
    return (type(authority.expires_at) in (int, float) and math.isfinite(authority.expires_at)
            and authority.expires_at > time.time()
            and (authority.monotonic_deadline is None
                 or (type(authority.monotonic_deadline) in (int, float) and math.isfinite(authority.monotonic_deadline)
                     and authority.monotonic_deadline > time.monotonic())))


def source_fingerprint(connection: sqlite3.Connection, run_id: str) -> str:
    run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if run is None or run["status"] not in {"succeeded", "failed", "cancelled"}:
        raise ValueError("review_requires_terminal_run")
    if connection.execute("SELECT 1 FROM actions WHERE run_id=? AND status IN ('intent','running','uncertain')", (run_id,)).fetchone():
        raise ValueError("review_requires_settled_actions")
    task = connection.execute("SELECT * FROM tasks WHERE task_id=?", (run["task_id"],)).fetchone()
    if task is None:
        raise ValueError("review_source_missing")
    source = {"fingerprint_version": "run-content-v1", "run": {key: run[key] for key in run.keys() if key != "training_eligible"},
              "task": dict(task)}
    for table in SOURCE_TABLES:
        source[table] = sorted((dict(row) for row in connection.execute(f"SELECT * FROM {table} WHERE run_id=?", (run_id,))), key=canonical)
    source["action_envelopes"] = sorted((dict(row) for row in connection.execute(
        "SELECT action_envelopes.* FROM action_envelopes JOIN actions USING(action_id) WHERE actions.run_id=?", (run_id,))), key=canonical)
    return digest(source)


def _stored_receipt(row: sqlite3.Row) -> dict:
    receipt = {"schema_version": "1.0", **{key: row[key] for key in row.keys() if key != "event_sha256"}}
    if not validator("dataset_review_receipt").is_valid(receipt) or digest(receipt) != row["event_sha256"]:
        raise ValueError("stored_review_integrity_failure")
    return receipt


def append_review(store: TrajectoryStore, receipt: dict, candidate: dict | None = None, *, authority: ReviewAuthority | None = None) -> str:
    if not validator("dataset_review_receipt").is_valid(receipt):
        raise ValueError("invalid_review_receipt")
    event_hash = digest(receipt)
    if (not isinstance(authority, ReviewAuthority) or authority.event_sha256 != event_hash
            or not authority_current(authority)):
        raise ValueError("explicit_review_authority_required")
    if receipt["decision"] == "accept":
        validate_record(receipt["kind"], candidate)
        if (digest(candidate) != receipt["candidate_sha256"] or candidate["provenance"]["run_id"] != receipt["run_id"]
                or candidate["provenance"]["synthetic"] != (receipt["provenance"] == "synthetic")
                or candidate["provenance"]["redaction_version"] != receipt["redaction_version"]):
            raise ValueError("candidate_review_binding_differs")
        expected_rights = "synthetic fixture authored for AOS" if receipt["usage_rights"] == "synthetic_authored" else "user_authorized"
        if candidate["provenance"]["usage_rights"] != expected_rights:
            raise ValueError("candidate_usage_rights_differ")
    if receipt["usage_rights"] == "synthetic_authored" and receipt["provenance"] != "synthetic":
        raise ValueError("invalid_usage_rights_binding")
    connection = store.connection
    if store.lock is None or connection.in_transaction:
        raise ValueError("review_requires_idle_owned_writer")
    connection.execute("BEGIN IMMEDIATE")
    try:
        existing = connection.execute("SELECT * FROM dataset_reviews WHERE receipt_id=?", (receipt["receipt_id"],)).fetchone()
        if existing is not None:
            if _stored_receipt(existing) != receipt:
                raise ValueError("receipt_id_conflict")
            connection.rollback()
            return receipt["receipt_id"]
        if receipt["decision"] == "accept":
            if source_fingerprint(connection, receipt["run_id"]) != receipt["source_sha256"]:
                raise ValueError("review_source_changed")
            policy = connection.execute("SELECT policy_version FROM runs WHERE run_id=?", (receipt["run_id"],)).fetchone()[0]
            if policy in SYNTHETIC_POLICIES and receipt["provenance"] != "synthetic":
                raise ValueError("bounded_task_remains_synthetic")
            if connection.execute("SELECT 1 FROM steps WHERE run_id=? AND step_id=?",
                                  (receipt["run_id"], candidate["provenance"]["step_id"])).fetchone() is None:
                raise ValueError("candidate_step_binding_differs")
        else:
            parent = connection.execute("SELECT * FROM dataset_reviews WHERE receipt_id=?", (receipt["revokes"],)).fetchone()
            if parent is None:
                raise ValueError("revoked_receipt_missing")
            _stored_receipt(parent)
        if not authority_current(authority):
            raise ValueError("explicit_review_authority_required")
        store.insert("dataset_reviews", **{key: value for key, value in receipt.items() if key != "schema_version"}, event_sha256=event_hash)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return receipt["receipt_id"]


def review_status(store: TrajectoryStore, receipt_id: str, candidate: dict) -> dict:
    connection = store.connection
    if connection.in_transaction and store.lock is not None:
        raise ValueError("review_requires_idle_reader")
    owns_transaction = not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN")
    try:
        row = connection.execute("SELECT * FROM dataset_reviews WHERE receipt_id=?", (receipt_id,)).fetchone()
        status = "missing"
        if row is not None:
            receipt = _stored_receipt(row)
            revocation = connection.execute("SELECT * FROM dataset_reviews WHERE revokes=?", (receipt_id,)).fetchone()
            if revocation is not None:
                _stored_receipt(revocation)
            if receipt["decision"] != "accept" or revocation is not None:
                status = "revoked"
            elif digest(candidate) != receipt["candidate_sha256"]:
                status = "candidate_changed"
            else:
                try:
                    current = source_fingerprint(connection, receipt["run_id"])
                except ValueError:
                    current = None
                status = "current" if current == receipt["source_sha256"] else "source_changed"
        return {"review_status": status, "training_ready": False}
    finally:
        if owns_transaction:
            connection.rollback()
