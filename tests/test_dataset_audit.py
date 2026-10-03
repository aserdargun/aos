import asyncio
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from aos.computer import WorkspaceRuntime
from aos.contracts import REPO_ROOT, Settings, canonical, digest, now
from aos.dataset import validator
from aos.dataset_audit import audit_database, audit_snapshot, inspect_snapshot
from aos.decision import FixtureDecisionEngine
from aos.operator import Operator
from aos.storage import TrajectoryStore


class DatasetAuditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "trace?#.sqlite"
        self.store = TrajectoryStore(self.database)
        self.addCleanup(self.store.close)
        runtime = WorkspaceRuntime(self.root / "workspace")
        runtime.start()
        self.addCleanup(runtime.stop)
        self.result = asyncio.run(Operator(Settings(workspace=runtime.root, database=self.database),
                                          self.store, runtime, FixtureDecisionEngine()).hello())

    def label(self, **changes):
        verification = self.store.connection.execute("SELECT * FROM verifications LIMIT 1").fetchone()
        action = self.store.connection.execute("SELECT * FROM actions WHERE action_id=?", (verification["action_id"],)).fetchone()
        values = {"label_id": "synthetic-label", "run_id": verification["run_id"], "step_id": verification["step_id"],
                  "decision_id": action["decision_id"], "verification_id": verification["verification_id"],
                  "label_type": "choice", "label_json": canonical({"correct_option": action["actual_option"]}),
                  "source": "verified_outcome", "review_status": "accepted", "created_at": now(), **changes}
        with self.store.connection:
            self.store.insert("trajectory_labels", **values)

    def test_inventory_is_fail_closed_and_contains_no_raw_content(self):
        secret = "synthetic-private-marker"
        with self.store.connection:
            self.store.connection.execute("UPDATE tasks SET original_goal=?,normalized_goal=?", (secret, secret))
            self.store.connection.execute("UPDATE observations SET payload_json=?", (canonical({"secret": secret}),))
            self.store.connection.execute("UPDATE runs SET environment_json=?,deployment_snapshot_json=?",
                                          (canonical({"usage_rights": "approved", "synthetic": False, "secret": secret}),
                                           canonical({"real_model": True, "secret": secret})))
        report = audit_database(self.database)
        validator("dataset_audit").validate(report)
        self.assertEqual(report["run_count"], 1)
        self.assertEqual(report["ready_runs"], 0)
        row = report["runs"][0]
        self.assertEqual(row["run_ref"], digest({"run_id": self.result["run_id"]}))
        self.assertEqual(row["blockers"], sorted(["run_not_opted_in", "synthetic_content", "usage_rights_unrecorded",
                                                "redaction_review_unrecorded", "no_accepted_labels"]))
        for private in (secret, str(self.database), self.result["run_id"], "Hello from the local agent"):
            self.assertNotIn(private, canonical(report))

    def test_readonly_no_reconciliation_or_source_database_wal_changes(self):
        with self.store.connection:
            self.store.connection.execute("UPDATE runs SET status='running',training_eligible=1")
            self.store.connection.execute("UPDATE actions SET status='intent'")
        paths = [self.database, Path(str(self.database) + "-wal")]
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
        report = audit_database(self.database)
        self.assertEqual(report["runs"][0]["status"], "running")
        self.assertIn("incomplete_actions", report["runs"][0]["blockers"])
        self.assertEqual({path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}, before)
        self.assertEqual(self.store.connection.execute("SELECT status,training_eligible FROM runs").fetchone()[:], ("running", 1))
        with audit_snapshot(self.database) as (snapshot, identity):
            with self.assertRaises(sqlite3.OperationalError):
                snapshot.execute("UPDATE runs SET training_eligible=0")
            self.assertEqual(snapshot.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            snapshot.execute("SELECT 1")

    def test_snapshot_includes_committed_wal_and_is_stable_during_writer_commit(self):
        with self.store.connection:
            self.store.connection.execute("UPDATE runs SET training_eligible=1")
        with audit_snapshot(self.database) as (snapshot, identity):
            with self.store.connection:
                self.store.connection.execute("UPDATE runs SET training_eligible=0,status='paused'")
            report = inspect_snapshot(snapshot, identity)
            self.assertEqual(report["opted_in_runs"], 1)
            self.assertEqual(report["runs"][0]["status"], "succeeded")
        latest = audit_database(self.database)
        self.assertEqual(latest["opted_in_runs"], 0)
        self.assertEqual(latest["runs"][0]["status"], "paused")
        self.assertNotEqual(latest["snapshot"]["sha256"], identity["sha256"])
        self.assertEqual(latest["snapshot"]["sha256"], audit_database(self.database)["snapshot"]["sha256"])

    def test_uncommitted_writer_does_not_leak_into_snapshot(self):
        self.store.connection.execute("UPDATE runs SET training_eligible=1")
        try:
            self.assertEqual(audit_database(self.database)["opted_in_runs"], 0)
        finally:
            self.store.connection.rollback()

    def test_flag_and_accepted_linked_label_never_grant_training_rights(self):
        self.label()
        with self.store.connection:
            self.store.connection.execute("UPDATE runs SET training_eligible=1")
        row = audit_database(self.database)["runs"][0]
        self.assertEqual(row["counts"]["choice_labels_linked"], 1)
        self.assertEqual(row["counts"]["choice_labels_invalid"], 0)
        self.assertNotIn("no_accepted_labels", row["blockers"])
        self.assertNotIn("run_not_opted_in", row["blockers"])
        self.assertEqual(row["blockers"], sorted(["synthetic_content", "usage_rights_unrecorded", "redaction_review_unrecorded"]))
        self.assertFalse(row["training_ready"])

    def test_invalid_gold_null_links_and_wrong_evidence_are_counted(self):
        cases = [{"label_json": '{"correct_option":"not-an-option"}'}, {"decision_id": None},
                 {"verification_id": None}, {"label_json": '[]'}, {"source": "human_review"}]
        for changes in cases:
            with self.subTest(changes=changes):
                self.label(**changes)
                row = audit_database(self.database)["runs"][0]
                self.assertEqual(row["counts"]["choice_labels_invalid"], 1)
                self.assertIn("invalid_choice_labels", row["blockers"])
                with self.store.connection:
                    self.store.connection.execute("DELETE FROM trajectory_labels")
        self.label()
        original = dict(self.store.connection.execute("SELECT * FROM verifications LIMIT 1").fetchone())
        for column, value in [("evidence_refs_json", '["missing-reference"]'), ("evidence_refs_json", '[]'),
                              ("expected_json", 'true'), ("result", "failed"), ("verifier", "model-self-check")]:
            with self.subTest(column=column):
                with self.store.connection:
                    self.store.connection.execute(f"UPDATE verifications SET {column}=?", (value,))
                self.assertEqual(audit_database(self.database)["runs"][0]["counts"]["choice_labels_invalid"], 1)
                with self.store.connection:
                    self.store.connection.execute(f"UPDATE verifications SET {column}=?", (original[column],))

    def test_artifacts_and_pending_unsupported_reviews_are_blockers(self):
        self.label(review_status="pending")
        self.label(label_id="synthetic-policy", label_type="policy", source="human_review")
        run = self.store.connection.execute("SELECT run_id,step_id FROM steps LIMIT 1").fetchone()
        with self.store.connection:
            self.store.insert("artifacts", artifact_id="synthetic-private-artifact", run_id=run[0], step_id=run[1],
                              relative_path="do-not-open.png", media_type="image/png", sha256="0" * 64,
                              size_bytes=1, redaction_status="raw", created_at=now())
        row = audit_database(self.database)["runs"][0]
        self.assertEqual(row["counts"]["trajectory_labels"]["pending"], 1)
        self.assertEqual(row["counts"]["unsupported_labels"], 1)
        self.assertTrue({"unreviewed_labels", "unsupported_label_types", "unredacted_artifacts", "image_artifacts_excluded"} <= set(row["blockers"]))
        self.assertNotIn("do-not-open", canonical(row))

    def test_unknown_policy_never_infers_real_content_or_usage_rights(self):
        with self.store.connection:
            self.store.connection.execute("UPDATE runs SET policy_version='synthetic-private-policy'")
        report = audit_database(self.database)
        self.assertIn("content_provenance_unclassified", report["runs"][0]["blockers"])
        self.assertNotIn("synthetic_content", report["runs"][0]["blockers"])
        self.assertNotIn("synthetic-private-policy", canonical(report))

    def test_missing_symlink_empty_and_non_database_inputs_fail_without_creation(self):
        missing = self.root / "absent.sqlite"
        with self.assertRaises(ValueError):
            audit_database(missing)
        self.assertFalse(missing.exists())
        link = self.root / "link.sqlite"
        link.symlink_to(self.database)
        with self.assertRaises(ValueError):
            audit_database(link)
        bad = self.root / "bad.sqlite"
        for content in (b"", b"synthetic-secret-not-a-database"):
            bad.write_bytes(content)
            with self.assertRaises((ValueError, sqlite3.Error)):
                audit_database(bad)
            self.assertEqual(bad.read_bytes(), content)

    def test_schema_drift_and_unknown_migrations_are_rejected(self):
        with self.store.connection:
            self.store.connection.execute("CREATE VIEW unexpected AS SELECT 1")
        with self.assertRaisesRegex(ValueError, "unsupported_database_schema"):
            audit_database(self.database)
        with self.store.connection:
            self.store.connection.execute("DROP VIEW unexpected")
            self.store.connection.execute("DELETE FROM schema_migrations WHERE version=7")
        with self.assertRaisesRegex(ValueError, "unsupported_database_migrations"):
            audit_database(self.database)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0],
                         len(list((REPO_ROOT / 'database/migrations').glob('*.sql'))) - 1)

    def test_foreign_key_corruption_fails_instead_of_reporting_ready(self):
        self.store.connection.execute("PRAGMA foreign_keys=OFF")
        with self.store.connection:
            self.store.connection.execute("UPDATE actions SET run_id='missing-run'")
        with self.assertRaisesRegex(ValueError, "database_integrity_failure"):
            audit_database(self.database)

    def test_resource_limits_abort_without_partial_report(self):
        for variable, value, reason in [("MAX_SNAPSHOT_BYTES", 1, "snapshot_size_limit"),
                                        ("MAX_RUNS", 0, "run_count_limit"),
                                        ("AUDIT_TIMEOUT_SECONDS", 0, "snapshot_resource_limit")]:
            with self.subTest(variable=variable), patch("aos.dataset_audit." + variable, value):
                with self.assertRaisesRegex(ValueError, reason):
                    audit_database(self.database)

    def test_cli_report_and_sanitized_failure(self):
        command = [sys.executable, "-m", "aos.dataset_audit", "--database", str(self.database)]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["run_count"], 1)
        command[-1] = str(self.root / "synthetic-private-path")
        failed = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(failed.stdout, "")
        self.assertNotIn("synthetic-private-path", failed.stderr)
        self.assertNotIn("Traceback", failed.stderr)

    def test_synthetic_report_contract_rejects_ready_and_raw_fields(self):
        fixture = json.loads((REPO_ROOT / "examples/dataset_audit.json").read_text())
        self.assertTrue(fixture["synthetic"])
        validator("dataset_audit").validate(fixture["report"])
        for change in ({"training_ready": True}, {"raw_goal": "private"}, {"ready_runs": 1}):
            report = {**copy.deepcopy(fixture["report"]), **change}
            self.assertFalse(validator("dataset_audit").is_valid(report))


if __name__ == "__main__":
    unittest.main()
