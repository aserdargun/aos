import asyncio
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from aos.computer import WorkspaceRuntime
from aos.contracts import REPO_ROOT, Settings, canonical, digest, now
from aos.dataset import validator
from aos.dataset_audit import audit_database
from aos.dataset_reviews import ReviewAuthority, append_review, review_status, source_fingerprint
from aos.decision import FixtureDecisionEngine
from aos.operator import Operator
from aos.storage import TrajectoryStore


class DatasetReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "store.sqlite"
        self.store = TrajectoryStore(self.database)
        self.addCleanup(lambda: self.store.close())
        runtime = WorkspaceRuntime(self.root / "workspace")
        runtime.start()
        self.addCleanup(runtime.stop)
        result = asyncio.run(Operator(Settings(workspace=runtime.root, database=self.database),
                                      self.store, runtime, FixtureDecisionEngine()).hello())
        self.run_id = result["run_id"]
        step = self.store.connection.execute("SELECT step_id FROM steps WHERE run_id=?", (self.run_id,)).fetchone()[0]
        verification = self.store.connection.execute("SELECT verification_id FROM verifications WHERE run_id=?", (self.run_id,)).fetchone()[0]
        self.candidate = json.loads((REPO_ROOT / "examples/system1_choice.jsonl").read_text().splitlines()[0])
        self.candidate["provenance"].update(run_id=self.run_id, step_id=step, verification_ref=verification)
        self.receipt = {"schema_version": "1.0", "receipt_id": "synthetic-accept", "run_id": self.run_id,
                        "source_sha256": source_fingerprint(self.store.connection, self.run_id), "candidate_sha256": digest(self.candidate),
                        "kind": "system1_choice", "purpose": "offline_training_text", "provenance": "synthetic",
                        "usage_rights": "synthetic_authored", "redaction_version": "fixture-v1", "reviewer_id": "synthetic-reviewer",
                        "authorization_ref": "synthetic-local-authorization", "decision": "accept", "revokes": None, "created_at": now()}

    def grant(self, receipt=None):
        return ReviewAuthority(digest(receipt or self.receipt), time.time() + 60)

    def accept(self):
        return append_review(self.store, self.receipt, self.candidate, authority=self.grant())

    def test_explicit_exact_event_authority_is_required(self):
        for authority in (None, {"event_sha256": digest(self.receipt), "expires_at": time.time() + 60},
                          ReviewAuthority("0" * 64, time.time() + 60), ReviewAuthority(digest(self.receipt), time.time() - 1),
                          ReviewAuthority(digest(self.receipt), float("nan")), ReviewAuthority(digest(self.receipt), float("inf"))):
            with self.subTest(authority=authority), self.assertRaisesRegex(ValueError, "explicit_review_authority_required"):
                append_review(self.store, self.receipt, self.candidate, authority=authority)
        changed = {**self.receipt, "reviewer_id": "different-reviewer"}
        with self.assertRaisesRegex(ValueError, "explicit_review_authority_required"):
            append_review(self.store, changed, self.candidate, authority=self.grant())
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM dataset_reviews").fetchone()[0], 0)

    def test_receipt_is_idempotent_private_and_does_not_enable_training(self):
        source = self.receipt["source_sha256"]
        self.assertEqual(self.accept(), "synthetic-accept")
        self.assertEqual(self.accept(), "synthetic-accept")
        self.assertEqual(source_fingerprint(self.store.connection, self.run_id), source)
        row = dict(self.store.connection.execute("SELECT * FROM dataset_reviews").fetchone())
        self.assertEqual(row["event_sha256"], digest(self.receipt))
        self.assertNotIn("Hello from the local agent", canonical(row))
        self.assertEqual(self.store.connection.execute("SELECT training_eligible FROM runs").fetchone()[0], 0)
        self.assertEqual(review_status(self.store, "synthetic-accept", self.candidate), {"review_status": "current", "training_ready": False})
        self.assertEqual(review_status(self.store, "missing", self.candidate)["review_status"], "missing")
        changed = {**self.receipt, "authorization_ref": "different-authorization"}
        with self.assertRaisesRegex(ValueError, "receipt_id_conflict"):
            append_review(self.store, changed, self.candidate, authority=self.grant(changed))

    def test_expiry_during_write_rolls_back_and_nested_transaction_is_untouched(self):
        current = time.time()
        authority = ReviewAuthority(digest(self.receipt), current + 60)
        with patch("aos.dataset_reviews.time.time", side_effect=[current, current + 120]):
            with self.assertRaisesRegex(ValueError, "explicit_review_authority_required"):
                append_review(self.store, self.receipt, self.candidate, authority=authority)
        self.assertFalse(self.store.connection.in_transaction)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM dataset_reviews").fetchone()[0], 0)
        self.store.connection.execute("UPDATE tasks SET normalized_goal='pending caller transaction'")
        try:
            with self.assertRaisesRegex(ValueError, "review_requires_idle_owned_writer"):
                self.accept()
            self.assertTrue(self.store.connection.in_transaction)
            self.assertEqual(self.store.connection.execute("SELECT normalized_goal FROM tasks").fetchone()[0], "pending caller transaction")
        finally:
            self.store.connection.rollback()

    def test_candidate_content_scope_and_rights_cannot_be_transferred(self):
        self.accept()
        changed = {**self.candidate, "state": "Different synthetic source content"}
        self.assertEqual(review_status(self.store, "synthetic-accept", changed)["review_status"], "candidate_changed")
        with self.assertRaisesRegex(ValueError, "candidate_review_binding_differs"):
            append_review(self.store, {**self.receipt, "receipt_id": "other"}, changed,
                          authority=self.grant({**self.receipt, "receipt_id": "other"}))
        for field, value in [("purpose", "remote_upload"), ("kind", "vision_pixels"), ("usage_rights", "unknown")]:
            changed_receipt = {**self.receipt, field: value}
            with self.assertRaisesRegex(ValueError, "invalid_review_receipt"):
                append_review(self.store, changed_receipt, self.candidate, authority=self.grant(changed_receipt))
        rights = {**self.receipt, "usage_rights": "user_authorized"}
        with self.assertRaisesRegex(ValueError, "candidate_usage_rights_differ"):
            append_review(self.store, rights, self.candidate, authority=self.grant(rights))

    def test_terminal_source_and_settled_actions_are_required(self):
        with self.store.connection:
            self.store.connection.execute("UPDATE runs SET status='paused'")
        with self.assertRaisesRegex(ValueError, "review_requires_terminal_run"):
            self.accept()
        with self.store.connection:
            self.store.connection.execute("UPDATE runs SET status='succeeded'")
            self.store.connection.execute("UPDATE actions SET status='uncertain'")
        with self.assertRaisesRegex(ValueError, "review_requires_settled_actions"):
            self.accept()
        self.assertFalse(self.store.connection.in_transaction)

    def test_source_changes_before_and_after_review_invalidate_binding(self):
        self.accept()
        with self.store.connection:
            self.store.connection.execute("UPDATE observations SET payload_json=?", (canonical({"synthetic": "modified"}),))
        self.assertEqual(review_status(self.store, "synthetic-accept", self.candidate)["review_status"], "source_changed")
        stale = {**self.receipt, "receipt_id": "stale-accept"}
        with self.assertRaisesRegex(ValueError, "review_source_changed"):
            append_review(self.store, stale, self.candidate, authority=self.grant(stale))
        self.accept()
        self.assertEqual(review_status(self.store, "synthetic-accept", self.candidate)["review_status"], "source_changed")
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM dataset_reviews").fetchone()[0], 1)

    def test_task_and_label_content_participate_but_eligibility_flag_does_not(self):
        original = self.receipt["source_sha256"]
        with self.store.connection:
            self.store.connection.execute("UPDATE runs SET training_eligible=1")
        self.assertEqual(source_fingerprint(self.store.connection, self.run_id), original)
        with self.store.connection:
            self.store.connection.execute("UPDATE tasks SET normalized_goal='changed synthetic task'")
        changed = source_fingerprint(self.store.connection, self.run_id)
        self.assertNotEqual(changed, original)
        with self.store.connection:
            self.store.insert("trajectory_labels", label_id="synthetic-new-label", run_id=self.run_id,
                              step_id=self.candidate["provenance"]["step_id"], label_type="choice", label_json='{}',
                              source="human_review", review_status="pending", created_at=now())
        self.assertNotEqual(source_fingerprint(self.store.connection, self.run_id), changed)

    def test_bounded_real_inference_cannot_be_relabelled_real_content(self):
        candidate = copy.deepcopy(self.candidate)
        candidate["provenance"].update(synthetic=False, usage_rights="user_authorized")
        receipt = {**self.receipt, "provenance": "real", "usage_rights": "user_authorized", "candidate_sha256": digest(candidate)}
        with self.assertRaisesRegex(ValueError, "bounded_task_remains_synthetic"):
            append_review(self.store, receipt, candidate, authority=self.grant(receipt))
        candidate["provenance"]["step_id"] = "different-step"
        candidate["provenance"].update(synthetic=True, usage_rights="synthetic fixture authored for AOS")
        receipt = {**self.receipt, "candidate_sha256": digest(candidate)}
        with self.assertRaisesRegex(ValueError, "candidate_step_binding_differs"):
            append_review(self.store, receipt, candidate, authority=self.grant(receipt))

    def test_revocation_is_append_only_and_works_after_source_changes(self):
        self.accept()
        with self.store.connection:
            self.store.connection.execute("UPDATE tasks SET normalized_goal='changed synthetic task'")
        revocation = {**self.receipt, "receipt_id": "synthetic-revoke", "decision": "revoke", "revokes": "synthetic-accept"}
        append_review(self.store, revocation, authority=self.grant(revocation))
        append_review(self.store, revocation, authority=self.grant(revocation))
        self.assertEqual(review_status(self.store, "synthetic-accept", self.candidate)["review_status"], "revoked")
        self.accept()
        self.assertEqual(review_status(self.store, "synthetic-accept", self.candidate)["review_status"], "revoked")
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM dataset_reviews").fetchone()[0], 2)

    def test_revocation_requires_matching_original_scope_and_authority(self):
        self.accept()
        revocation = {**self.receipt, "receipt_id": "synthetic-revoke", "decision": "revoke", "revokes": "synthetic-accept"}
        with self.assertRaisesRegex(ValueError, "explicit_review_authority_required"):
            append_review(self.store, revocation, self.candidate, authority=self.grant())
        changed = {**revocation, "source_sha256": "0" * 64}
        with self.assertRaises(sqlite3.IntegrityError):
            append_review(self.store, changed, self.candidate, authority=self.grant(changed))
        self.assertEqual(review_status(self.store, "synthetic-accept", self.candidate)["review_status"], "current")
        missing = {**revocation, "revokes": "missing"}
        with self.assertRaisesRegex(ValueError, "revoked_receipt_missing"):
            append_review(self.store, missing, self.candidate, authority=self.grant(missing))

    def test_sql_update_delete_and_replace_cannot_erase_history(self):
        self.accept()
        row = dict(self.store.connection.execute("SELECT * FROM dataset_reviews").fetchone())
        for sql in ("UPDATE dataset_reviews SET reviewer_id='another'", "DELETE FROM dataset_reviews",
                    "INSERT OR REPLACE INTO dataset_reviews SELECT * FROM dataset_reviews"):
            with self.assertRaises(sqlite3.IntegrityError), self.store.connection:
                self.store.connection.execute(sql)
        altered = {**row, "receipt_id": "replacement-id"}
        columns = ','.join(altered)
        with self.assertRaises(sqlite3.IntegrityError), self.store.connection:
            self.store.connection.execute(f"INSERT OR REPLACE INTO dataset_reviews ({columns}) VALUES ({','.join('?' for _ in altered)})", tuple(altered.values()))
        self.assertEqual(dict(self.store.connection.execute("SELECT * FROM dataset_reviews").fetchone()), row)

    def test_review_survives_reopen_and_readonly_access_cannot_append(self):
        self.accept()
        self.store.close()
        self.store = TrajectoryStore(self.database)
        self.assertEqual(review_status(self.store, "synthetic-accept", self.candidate)["review_status"], "current")
        readonly = TrajectoryStore(self.database, readonly=True)
        try:
            self.assertEqual(review_status(readonly, "synthetic-accept", self.candidate)["review_status"], "current")
            with self.assertRaisesRegex(ValueError, "review_requires_idle_owned_writer"):
                append_review(readonly, self.receipt, self.candidate, authority=self.grant())
        finally:
            readonly.close()

    def test_inventory_reports_unassessed_receipts_not_permission(self):
        self.accept()
        report = audit_database(self.database)
        self.assertEqual(report["audit_version"], "sqlite-eligibility-v2")
        row = report["runs"][0]
        self.assertIn("dataset_reviews_require_validation", row["blockers"])
        self.assertNotIn("usage_rights_unrecorded", row["blockers"])
        self.assertFalse(report["training_ready"])
        self.assertEqual(report["ready_runs"], 0)
        self.assertNotIn("synthetic-reviewer", canonical(report))

    def test_v7_audit_and_additive_v8_v9_migrations_preserve_existing_rows(self):
        legacy = self.root / "legacy.sqlite"
        connection = sqlite3.connect(legacy)
        for path in sorted((REPO_ROOT / "database/migrations").glob("*.sql"))[:7]:
            connection.executescript(path.read_text())
        with connection:
            connection.execute("INSERT INTO tasks VALUES('synthetic-task','original','normalized','en','[]','[]',?)", (now(),))
            connection.execute("INSERT INTO runs VALUES('synthetic-run','synthetic-task','cancelled','unknown','hello-policy-v1','{}','{}',?,NULL,0)", (now(),))
        original = connection.execute("SELECT * FROM runs").fetchall()
        connection.close()
        before = legacy.read_bytes()
        self.assertEqual(len(audit_database(legacy)["snapshot"]["migrations"]), 7)
        self.assertEqual(legacy.read_bytes(), before)
        upgraded = TrajectoryStore(legacy)
        try:
            self.assertEqual([tuple(row) for row in upgraded.connection.execute("SELECT * FROM runs")], original)
            self.assertEqual(upgraded.connection.execute("SELECT count(*) FROM dataset_reviews").fetchone()[0], 0)
            self.assertEqual(upgraded.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(len(audit_database(legacy)["snapshot"]["migrations"]), 17)
        finally:
            upgraded.close()

    def test_receipt_fixture_and_revoke_schema(self):
        fixture = json.loads((REPO_ROOT / "examples/dataset_review_receipts.json").read_text())
        self.assertTrue(fixture["synthetic"])
        for receipt in fixture["receipts"]:
            validator("dataset_review_receipt").validate(receipt)
        for changes in ({"revokes": "not-null"}, {"raw_content": "not allowed"}, {"decision": "activate"}):
            self.assertFalse(validator("dataset_review_receipt").is_valid({**self.receipt, **changes}))


if __name__ == "__main__":
    unittest.main()
