import argparse
from collections import Counter
from contextlib import closing, contextmanager
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Literal, get_args

from .contracts import REPO_ROOT, canonical, digest, now
from .dataset import validator


MAX_SNAPSHOT_BYTES = 128 * 1024 * 1024
MAX_RUNS = 10000
AUDIT_TIMEOUT_SECONDS = 10
SupportedSchemaVersion = Literal[7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20,
                                 21, 22, 23, 24, 25, 26, 27, 28]
SUPPORTED_SCHEMA_VERSIONS = get_args(SupportedSchemaVersion)
CURRENT_SCHEMA_VERSION = max(SUPPORTED_SCHEMA_VERSIONS)
SYNTHETIC_POLICIES = {"hello-policy-v1", "browser-form-policy-v1", "vision-canvas-policy-v1",
                      "browser-local-navigation-policy-v1", "browser-staging-workflow-policy-v1"}
VERIFIERS = {"independent_read_equals": "aos-exact-bytes-v1", "independent_dom_equals": "aos-browser-form-v1",
             "independent_canvas_equals": "aos-visual-canvas-v1",
             "independent_local_page_equals": "aos-local-navigation-v1",
             "independent_staging_state_equals": "aos-synthetic-staging-v1"}
REASONS = ("run_not_opted_in", "synthetic_content", "content_provenance_unclassified", "usage_rights_unrecorded",
           "redaction_review_unrecorded", "run_not_verified", "no_passed_verification", "incomplete_actions",
           "no_accepted_labels", "unreviewed_labels", "invalid_choice_labels", "unsupported_label_types",
           "unredacted_artifacts", "image_artifacts_excluded", "dataset_reviews_require_validation")


def schema_signature(connection: sqlite3.Connection) -> str:
    return digest([tuple(row) for row in connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")])


@lru_cache
def expected_schema(version: int = CURRENT_SCHEMA_VERSION) -> tuple:
    migrations = sorted((REPO_ROOT / "database/migrations").glob("*.sql"))
    if version not in SUPPORTED_SCHEMA_VERSIONS or [int(path.name.split("_", 1)[0]) for path in migrations] != list(range(1, CURRENT_SCHEMA_VERSION + 1)):
        raise ValueError("unsupported_migration_set")
    migrations = migrations[:version]
    with closing(sqlite3.connect(":memory:")) as reference:
        reference.execute("PRAGMA foreign_keys=ON")
        for path in migrations:
            reference.executescript(path.read_text())
        versions = [tuple(row) for row in reference.execute("SELECT version,name FROM schema_migrations ORDER BY version")]
        return schema_signature(reference), versions, {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in migrations}


@contextmanager
def audit_snapshot(path: Path):
    path = Path(path).absolute()
    if path.is_symlink() or not path.is_file():
        raise ValueError("database_path_unavailable")
    deadline = time.monotonic() + AUDIT_TIMEOUT_SECONDS
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as source, \
            closing(sqlite3.connect(":memory:")) as snapshot:
        source.execute("PRAGMA foreign_keys=ON")
        source.execute("PRAGMA query_only=ON")
        source.execute("PRAGMA trusted_schema=OFF")
        if time.monotonic() >= deadline:
            raise ValueError("snapshot_resource_limit")
        source.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        source.execute("BEGIN")
        source.execute("SELECT count(*) FROM sqlite_master").fetchone()
        captured_at = now()
        page_size = source.execute("PRAGMA page_size").fetchone()[0]
        if page_size * source.execute("PRAGMA page_count").fetchone()[0] > MAX_SNAPSHOT_BYTES:
            raise ValueError("snapshot_size_limit")

        def progress(status, remaining, total):
            if total * page_size > MAX_SNAPSHOT_BYTES or time.monotonic() > deadline:
                raise ValueError("snapshot_resource_limit")

        source.backup(snapshot, pages=256, progress=progress, sleep=0.01)
        source.rollback()
        snapshot.row_factory = sqlite3.Row
        snapshot.execute("PRAGMA foreign_keys=ON")
        snapshot.execute("PRAGMA query_only=ON")
        snapshot.execute("PRAGMA trusted_schema=OFF")
        snapshot.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        signature = schema_signature(snapshot)
        expected = next((expected_schema(version) for version in SUPPORTED_SCHEMA_VERSIONS if expected_schema(version)[0] == signature), None)
        if expected is None:
            raise ValueError("unsupported_database_schema")
        signature, versions, migrations = expected
        if [tuple(row) for row in snapshot.execute("SELECT version,name FROM schema_migrations ORDER BY version")] != versions:
            raise ValueError("unsupported_database_migrations")
        if [tuple(row) for row in snapshot.execute("PRAGMA integrity_check")] != [("ok",)] or snapshot.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("database_integrity_failure")
        serialized = snapshot.serialize()
        identity = {"sha256": hashlib.sha256(serialized).hexdigest(), "bytes": len(serialized),
                    "captured_at": captured_at, "schema_sha256": signature, "migrations": migrations}
        del serialized
        yield snapshot, identity


def valid_choice_label(connection: sqlite3.Connection, label: sqlite3.Row) -> bool:
    if label["source"] != "verified_outcome":
        return False
    decision = connection.execute("SELECT options_json FROM decisions WHERE decision_id=? AND run_id=? AND step_id=?",
                                  (label["decision_id"], label["run_id"], label["step_id"])).fetchone()
    verification = connection.execute("SELECT * FROM verifications WHERE verification_id=? AND run_id=? AND step_id=?",
                                      (label["verification_id"], label["run_id"], label["step_id"])).fetchone()
    if decision is None or verification is None or verification["result"] != "passed":
        return False
    if VERIFIERS.get(verification["method"]) != verification["verifier"]:
        return False
    try:
        target = json.loads(label["label_json"])
        options = json.loads(decision["options_json"])
        option_ids = [option["id"] for option in options]
        references = json.loads(verification["evidence_refs_json"])
        if (not isinstance(target, dict) or not isinstance(target.get("correct_option"), str)
                or not 2 <= len(option_ids) <= 10 or any(not isinstance(option, str) for option in option_ids)
                or len(set(option_ids)) != len(option_ids) or target["correct_option"] not in option_ids
                or not isinstance(references, list) or not references
                or any(not isinstance(reference, str) for reference in references)
                or canonical(json.loads(verification["expected_json"])) != canonical(json.loads(verification["actual_json"]))):
            return False
    except (ValueError, TypeError, KeyError, RecursionError):
        return False
    action = connection.execute("SELECT status,decision_id,actual_option FROM actions WHERE action_id=? AND run_id=? AND step_id=?",
                                (verification["action_id"], label["run_id"], label["step_id"])).fetchone()
    if action is None or action["status"] != "ok" or action["decision_id"] != label["decision_id"] or action["actual_option"] != target["correct_option"]:
        return False
    for reference in references:
        evidence = connection.execute("""SELECT observation_id FROM observations WHERE observation_id=? AND run_id=? AND step_id=?
            UNION ALL SELECT artifact_id FROM artifacts WHERE artifact_id=? AND run_id=? AND step_id=?""",
            (reference, label["run_id"], label["step_id"], reference, label["run_id"], label["step_id"])).fetchall()
        if len(evidence) != 1:
            return False
    return True


def inspect_snapshot(connection: sqlite3.Connection, identity: dict) -> dict:
    if connection.execute("SELECT count(*) FROM runs").fetchone()[0] > MAX_RUNS:
        raise ValueError("run_count_limit")
    rows = []
    reviews_supported = connection.execute("SELECT 1 FROM schema_migrations WHERE version=8").fetchone() is not None
    for run in connection.execute("SELECT run_id,status,outcome,policy_version,training_eligible FROM runs ORDER BY run_id"):
        counts = {}
        for table, column, values in (
                ("trajectory_labels", "review_status", ("accepted", "pending", "rejected")),
                ("verifications", "result", ("passed", "failed", "unknown")),
                ("actions", "status", ("intent", "running", "ok", "error", "denied", "cancelled", "uncertain")),
                ("artifacts", "redaction_status", ("raw", "redacted", "quarantined"))):
            found = dict(connection.execute(f"SELECT {column},count(*) FROM {table} WHERE run_id=? GROUP BY {column}", (run["run_id"],)))
            counts[table] = {value: found.get(value, 0) for value in values}
        labels = connection.execute("SELECT * FROM trajectory_labels WHERE run_id=? AND review_status='accepted'", (run["run_id"],)).fetchall()
        choice_labels = [label for label in labels if label["label_type"] == "choice"]
        counts["choice_labels_linked"] = sum(valid_choice_label(connection, label) for label in choice_labels)
        counts["choice_labels_invalid"] = len(choice_labels) - counts["choice_labels_linked"]
        counts["unsupported_labels"] = len(labels) - len(choice_labels)
        counts["image_artifacts"] = connection.execute("SELECT count(*) FROM artifacts WHERE run_id=? AND lower(media_type) LIKE 'image/%'", (run["run_id"],)).fetchone()[0]
        has_reviews = reviews_supported and connection.execute("SELECT 1 FROM dataset_reviews WHERE run_id=? LIMIT 1", (run["run_id"],)).fetchone() is not None
        conditions = {
            "run_not_opted_in": not run["training_eligible"],
            "synthetic_content": run["policy_version"] in SYNTHETIC_POLICIES,
            "content_provenance_unclassified": run["policy_version"] not in SYNTHETIC_POLICIES,
            "usage_rights_unrecorded": not has_reviews, "redaction_review_unrecorded": not has_reviews,
            "dataset_reviews_require_validation": has_reviews,
            "run_not_verified": run["status"] != "succeeded" or run["outcome"] != "passed",
            "no_passed_verification": not counts["verifications"]["passed"],
            "incomplete_actions": any(counts["actions"][status] for status in ("intent", "running", "uncertain")),
            "no_accepted_labels": not labels,
            "unreviewed_labels": bool(counts["trajectory_labels"]["pending"] or counts["trajectory_labels"]["rejected"]),
            "invalid_choice_labels": bool(counts["choice_labels_invalid"]),
            "unsupported_label_types": bool(counts["unsupported_labels"]),
            "unredacted_artifacts": bool(counts["artifacts"]["raw"] or counts["artifacts"]["quarantined"]),
            "image_artifacts_excluded": bool(counts["image_artifacts"])}
        rows.append({"run_ref": digest({"run_id": run["run_id"]}), "status": run["status"], "outcome": run["outcome"],
                     "training_eligible_flag": bool(run["training_eligible"]), "training_ready": False,
                     "counts": counts, "blockers": sorted(reason for reason, blocked in conditions.items() if blocked)})
    report = {"schema_version": "1.0", "audit_version": "sqlite-eligibility-v2", "mode": "read_only_inventory",
              "training_ready": False, "snapshot": identity, "auditor_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "run_count": len(rows), "opted_in_runs": sum(row["training_eligible_flag"] for row in rows),
              "ready_runs": 0, "blocker_counts": dict(sorted(Counter(reason for row in rows for reason in row["blockers"]).items())),
              "runs": sorted(rows, key=lambda row: row["run_ref"])}
    if not validator("dataset_audit").is_valid(report):
        raise ValueError("invalid_audit_report")
    return report


def audit_database(path: Path) -> dict:
    with audit_snapshot(path) as (snapshot, identity):
        return inspect_snapshot(snapshot, identity)


def main() -> None:
    parser = argparse.ArgumentParser(description="AOS salt okunur SQLite veri uygunluk envanteri; eğitim veya export yapmaz")
    parser.add_argument("--database", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        report = audit_database(arguments.database)
    except (ValueError, OSError, sqlite3.Error, RecursionError):
        parser.exit(1, "Denetim tamamlanamadı: veritabanı yolu, şema, bütünlük veya kaynak sınırını kontrol edin.\n")
    print(canonical(report))


if __name__ == "__main__":
    main()
