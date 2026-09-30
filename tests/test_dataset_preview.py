import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from aos.computer import WorkspaceRuntime
from aos.contracts import REPO_ROOT, Settings, canonical, digest, now
from aos.dataset import validator
from aos.dataset_audit import audit_snapshot
from aos.dataset_preview import derive_choice, inspect_preview, load_candidate, preview_database
from aos.dataset_reviews import ReviewAuthority, append_review, source_fingerprint
from aos.decision import FixtureDecisionEngine
from aos.operator import Operator
from aos.storage import TrajectoryStore


class DatasetPreviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "trace.sqlite"
        self.store = TrajectoryStore(self.path)
        self.addCleanup(self.store.close)
        runtime = WorkspaceRuntime(self.root / "workspace")
        runtime.start()
        self.addCleanup(runtime.stop)
        result = asyncio.run(Operator(Settings(workspace=runtime.root, database=self.path), self.store, runtime, FixtureDecisionEngine()).hello())
        self.run_id = result["run_id"]
        self.label_id = "synthetic-reviewed-label"
        self.verification = self.store.connection.execute("SELECT * FROM verifications LIMIT 1").fetchone()
        self.action = self.store.connection.execute("SELECT * FROM actions WHERE action_id=?", (self.verification["action_id"],)).fetchone()

    def label(self, **changes):
        values = {"label_id": self.label_id, "run_id": self.run_id, "step_id": self.verification["step_id"],
                  "decision_id": self.action["decision_id"], "verification_id": self.verification["verification_id"],
                  "label_type": "choice", "label_json": canonical({"correct_option": self.action["actual_option"],
                    "outcome": "passed", "rationale": "Explicit synthetic review of independent readback."}),
                  "source": "verified_outcome", "review_status": "accepted", "created_at": now(), **changes}
        with self.store.connection:
            self.store.insert("trajectory_labels", **values)

    def derived(self):
        with audit_snapshot(self.path) as (snapshot, identity):
            run = snapshot.execute("SELECT * FROM runs WHERE run_id=?", (self.run_id,)).fetchone()
            label = snapshot.execute("SELECT * FROM trajectory_labels WHERE label_id=?", (self.label_id,)).fetchone()
            return derive_choice(snapshot, run, label)

    def reviewed_candidate(self):
        candidate = self.derived()
        candidate["provenance"].update(usage_rights="synthetic fixture authored for AOS", redaction_version="synthetic-review-v1")
        return candidate

    def accept(self, candidate):
        receipt = {"schema_version": "1.0", "receipt_id": "synthetic-accept", "run_id": self.run_id,
                   "source_sha256": source_fingerprint(self.store.connection, self.run_id), "candidate_sha256": digest(candidate),
                   "kind": "system1_choice", "purpose": "offline_training_text", "provenance": "synthetic",
                   "usage_rights": "synthetic_authored", "redaction_version": "synthetic-review-v1",
                   "reviewer_id": "synthetic-reviewer", "authorization_ref": "synthetic-authority",
                   "decision": "accept", "revokes": None, "created_at": now()}
        append_review(self.store, receipt, candidate, authority=ReviewAuthority(digest(receipt), time.time() + 60))
        return receipt

    def preview(self, candidate=None, receipt_id=None):
        return preview_database(self.path, self.run_id, label_id=self.label_id, candidate=candidate, receipt_id=receipt_id)

    def test_missing_label_never_generates_gold_or_changes_source(self):
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (self.path, Path(str(self.path) + "-wal"))}
        report = preview_database(self.path, self.run_id)
        validator("dataset_preview").validate(report)
        self.assertIn("no_accepted_choice_labels", report["blockers"])
        self.assertIsNone(report["projection_sha256"])
        self.assertFalse(report["training_ready"])
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM trajectory_labels").fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM dataset_reviews").fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute("SELECT training_eligible FROM runs").fetchone()[0], 0)
        self.assertEqual(before, {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in before})

    def test_projection_is_deterministic_and_only_exposes_hashes(self):
        self.label()
        with self.store.connection:
            self.store.connection.execute("UPDATE tasks SET original_goal='synthetic-private-goal'")
        report = self.preview()
        self.assertEqual(report, self.preview())
        candidate = self.derived()
        self.assertEqual(digest(candidate), report["projection_sha256"])
        self.assertEqual(candidate["provenance"]["usage_rights"], "unreviewed")
        self.assertEqual(candidate["provenance"]["redaction_version"], "preview-unreviewed-v1")
        self.assertIn("candidate_not_supplied", report["blockers"])
        for sensitive in (self.run_id, self.label_id, "synthetic-private-goal", "Explicit synthetic review", "Hello from the local agent", str(self.path)):
            self.assertNotIn(sensitive, canonical(report))
        self.assertIn("label_selection_required", preview_database(self.path, self.run_id)["blockers"])

    def test_matching_candidate_and_receipt_are_not_training_permission(self):
        self.label()
        candidate = self.reviewed_candidate()
        self.accept(candidate)
        report = self.preview(candidate, "synthetic-accept")
        self.assertTrue(report["derivation_matches"])
        self.assertEqual(report["receipt_status"], "current")
        self.assertFalse(report["training_ready"])
        self.assertFalse(report["metadata_claims_verified"])
        self.assertEqual(set(report["blockers"]), {"training_not_authorized", "reviewer_authentication_unverified",
                                                   "redaction_unverified", "usage_rights_unverified", "synthetic_content_excluded"})

    def test_current_receipt_does_not_hide_wrong_derivation(self):
        self.label()
        candidate = self.reviewed_candidate()
        candidate["state"] = "Synthetic but not the recorded pre-decision state."
        self.accept(candidate)
        report = self.preview(candidate, "synthetic-accept")
        self.assertEqual(report["receipt_status"], "current")
        self.assertFalse(report["derivation_matches"])
        self.assertIn("candidate_derivation_differs", report["blockers"])

    def test_candidate_changes_and_option_order_are_detected(self):
        self.label()
        candidate = self.reviewed_candidate()
        self.accept(candidate)
        for change in (lambda value: value["options"].reverse(), lambda value: value.update(question="Different question?"),
                       lambda value: value["target"].update(rationale="Different rationale"),
                       lambda value: value["prediction"].update(selected_option="ask_human"),
                       lambda value: value["provenance"].update(verification_ref="different-verification")):
            modified = copy.deepcopy(candidate)
            change(modified)
            report = self.preview(modified, "synthetic-accept")
            self.assertFalse(report["derivation_matches"])
            self.assertEqual(report["receipt_status"], "candidate_changed")
        invalid = {**candidate, "chain_of_thought": "not supported"}
        self.assertIn("candidate_invalid", self.preview(invalid)["blockers"])

    def test_gold_is_existing_reviewed_target_not_prediction(self):
        self.label()
        with self.store.connection:
            self.store.connection.execute("UPDATE decisions SET selected_option='ask_supervisor'")
        candidate = self.derived()
        self.assertEqual(candidate["prediction"]["selected_option"], "ask_supervisor")
        self.assertEqual(candidate["target"]["correct_option"], "write_file")

    def test_no_implicit_rationale_or_policy_supervisor_label_conversion(self):
        cases = [{"review_status": "pending"}, {"label_type": "policy"}, {"source": "human_review"},
                 {"label_json": '{"correct_option":"write_file"}'}]
        reasons = ("label_not_accepted", "label_kind_unsupported", "label_evidence_invalid", "label_target_incomplete")
        for changes, reason in zip(cases, reasons):
            self.label(**changes)
            self.assertIn(reason, self.preview()["blockers"])
            with self.store.connection:
                self.store.connection.execute("DELETE FROM trajectory_labels")

    def test_decision_snapshot_is_used_instead_of_future_runtime_state(self):
        self.label()
        expected = self.derived()
        current = json.loads(self.store.connection.execute("SELECT state_json FROM runtime_states").fetchone()[0])
        current["observation"] = "SYNTHETIC FUTURE OUTCOME DO NOT LEAK"
        with self.store.connection:
            self.store.connection.execute("UPDATE runtime_states SET state_json=?", (canonical(current),))
        self.assertEqual(self.derived(), expected)
        with self.store.connection:
            self.store.connection.execute("UPDATE state_snapshots SET content_sha256=? WHERE snapshot_id=(SELECT snapshot_id FROM decisions LIMIT 1)", ("0" * 64,))
        self.assertIn("decision_snapshot_invalid", self.preview()["blockers"])

    def test_source_changes_and_revocations_are_detected(self):
        self.label()
        candidate = self.reviewed_candidate()
        receipt = self.accept(candidate)
        with self.store.connection:
            self.store.connection.execute("UPDATE tasks SET original_goal='changed synthetic task'")
        report = self.preview(candidate, receipt["receipt_id"])
        self.assertTrue(report["derivation_matches"])
        self.assertEqual(report["receipt_status"], "source_changed")
        revoked = {**receipt, "receipt_id": "synthetic-revoke", "decision": "revoke", "revokes": receipt["receipt_id"]}
        append_review(self.store, revoked, authority=ReviewAuthority(digest(revoked), time.time() + 60))
        self.assertEqual(self.preview(candidate, receipt["receipt_id"])["receipt_status"], "revoked")

    def test_model_request_response_binding_without_running_models(self):
        self.label()
        candidate = self.derived()
        decision = self.store.connection.execute("SELECT * FROM decisions LIMIT 1").fetchone()
        state = json.loads(self.store.connection.execute("SELECT state_json FROM state_snapshots WHERE snapshot_id=?", (decision["snapshot_id"],)).fetchone()[0])
        request = {key: candidate[key] for key in ("state", "question", "options")}
        with self.store.connection:
            self.store.insert("model_calls", call_id="synthetic-call", run_id=self.run_id, step_id=self.verification["step_id"],
                              deployment_id=state["deployment_id"], role="system1", request_json=canonical(request),
                              response_json=canonical(candidate["prediction"]), status="ok", created_at=now())
            self.store.connection.execute("UPDATE decisions SET call_id='synthetic-call'")
        self.assertIsNotNone(self.preview()["projection_sha256"])
        with self.store.connection:
            self.store.connection.execute("UPDATE model_calls SET request_json='{}'")
        self.assertIn("model_request_differs", self.preview()["blockers"])
        with self.store.connection:
            self.store.connection.execute("UPDATE model_calls SET request_json=?,response_json='{}'", (canonical(request),))
        self.assertIn("model_prediction_differs", self.preview()["blockers"])

    def test_unknown_unsettled_and_image_sources_are_not_projected(self):
        self.label()
        with self.store.connection:
            self.store.connection.execute("UPDATE runs SET status='paused'")
        self.assertIn("source_unsettled", self.preview()["blockers"])
        with self.store.connection:
            self.store.connection.execute("UPDATE runs SET status='succeeded',policy_version='unknown'")
        self.assertIn("task_kind_unsupported", self.preview()["blockers"])
        with self.store.connection:
            self.store.connection.execute("UPDATE runs SET policy_version='hello-policy-v1'")
            self.store.insert("artifacts", artifact_id="synthetic-image", run_id=self.run_id, step_id=self.verification["step_id"],
                              relative_path="not-opened.png", media_type="image/png", sha256="0" * 64, size_bytes=1,
                              redaction_status="redacted", created_at=now())
        self.assertIn("image_artifacts_excluded", self.preview()["blockers"])

    def test_frozen_snapshot_does_not_mix_writer_updates(self):
        self.label()
        candidate = self.reviewed_candidate()
        self.accept(candidate)
        with audit_snapshot(self.path) as (snapshot, identity):
            with self.store.connection:
                self.store.connection.execute("UPDATE tasks SET original_goal='concurrent synthetic change'")
            report = inspect_preview(snapshot, identity, self.run_id, label_id=self.label_id, candidate=candidate, receipt_id="synthetic-accept")
            self.assertEqual(report["receipt_status"], "current")
        self.assertEqual(self.preview(candidate, "synthetic-accept")["receipt_status"], "source_changed")

    def test_strict_candidate_loader_and_safe_cli(self):
        self.label()
        candidate = self.reviewed_candidate()
        path = self.root / "candidate.json"
        path.write_text(canonical(candidate))
        self.assertEqual(load_candidate(path), candidate)
        command = [sys.executable, "-m", "aos.dataset_preview", "--database", str(self.path), "--run-id", self.run_id,
                   "--label-id", self.label_id, "--candidate", str(path)]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(json.loads(completed.stdout)["derivation_matches"])
        self.assertNotIn(candidate["state"], completed.stdout)
        for payload in ('{"state":"synthetic-secret","state":"duplicate"}', 'null', '{"synthetic-private":"broken"}'):
            path.write_text(payload)
            with self.assertRaises(ValueError):
                load_candidate(path)
            failed = subprocess.run(command, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual(failed.stdout, "")
            self.assertNotIn("synthetic-private", failed.stderr)
        path.write_text(canonical(candidate))
        with patch("aos.dataset_preview.MAX_CANDIDATE_BYTES", 1), self.assertRaises(ValueError):
            load_candidate(path)
        link = self.root / "link.json"
        link.symlink_to(path)
        with self.assertRaises(ValueError):
            load_candidate(link)

    def test_missing_arguments_and_report_contract(self):
        report = preview_database(self.path, "synthetic-missing-run")
        self.assertIn("run_missing", report["blockers"])
        with self.assertRaises(ValueError):
            preview_database(self.path, self.run_id, receipt_id="not-enough-input")
        fixture = json.loads((REPO_ROOT / "examples/dataset_preview.json").read_text())
        self.assertTrue(fixture["synthetic"])
        validator("dataset_preview").validate(fixture["report"])
        for change in ({"training_ready": True}, {"raw_state": "not allowed"}, {"metadata_claims_verified": True}):
            self.assertFalse(validator("dataset_preview").is_valid({**fixture["report"], **change}))

    def test_v7_preview_does_not_migrate_or_require_receipts_table(self):
        self.label()
        candidate = self.reviewed_candidate()
        legacy = self.root / "legacy.sqlite"
        connection = sqlite3.connect(legacy)
        try:
            for path in sorted((REPO_ROOT / "database/migrations").glob("*.sql"))[:7]:
                connection.executescript(path.read_text())
            with connection:
                connection.execute("PRAGMA defer_foreign_keys=ON")
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                    table = row[0]
                    if table == "schema_migrations":
                        continue
                    rows = self.store.connection.execute(f'SELECT * FROM "{table}"').fetchall()
                    if rows:
                        placeholders = ",".join("?" for _ in rows[0])
                        connection.executemany(f'INSERT INTO "{table}" VALUES({placeholders})', rows)
        finally:
            connection.close()
        before = legacy.read_bytes()
        report = preview_database(legacy, self.run_id, label_id=self.label_id, candidate=candidate, receipt_id="missing")
        self.assertTrue(report["derivation_matches"])
        self.assertEqual(report["receipt_status"], "unavailable_v7")
        self.assertEqual(report["source_sha256"], source_fingerprint(self.store.connection, self.run_id))
        self.assertEqual(legacy.read_bytes(), before)
        with audit_snapshot(legacy) as (snapshot, identity):
            self.assertEqual(snapshot.execute("SELECT max(version) FROM schema_migrations").fetchone()[0], 7)

    def test_missing_and_corrupt_receipts_are_not_current(self):
        self.label()
        candidate = self.reviewed_candidate()
        self.assertEqual(self.preview(candidate, "missing")["receipt_status"], "missing")
        receipt = self.accept(candidate)
        invalid = {**receipt, "receipt_id": "synthetic-corrupt", "source_sha256": "0" * 64}
        invalid.pop("schema_version")
        with self.store.connection:
            self.store.insert("dataset_reviews", **invalid, event_sha256="0" * 64)
        report = self.preview(candidate, "synthetic-corrupt")
        self.assertEqual(report["receipt_status"], "invalid")
        self.assertIn("receipt_not_current", report["blockers"])

    def test_missing_label_evidence_and_real_model_call_fail_closed(self):
        self.assertIn("label_unavailable", self.preview()["blockers"])
        self.label()
        with self.store.connection:
            self.store.connection.execute("UPDATE runs SET deployment_snapshot_json=?", (canonical({"real_model": True}),))
        self.assertIn("model_call_missing", self.preview()["blockers"])
        with self.store.connection:
            self.store.connection.execute("UPDATE verifications SET evidence_refs_json='[\"missing-evidence\"]'")
        self.assertIn("label_evidence_invalid", self.preview()["blockers"])

    @unittest.skipUnless(os.environ.get("AOS_BROWSER_TESTS") == "1", "Opt in to real isolated Chromium with AOS_BROWSER_TESTS=1")
    def test_browser_fill_and_submit_projection_with_fixture_engine(self):
        from aos.browser import BrowserRuntime
        from aos.browser_operator import BrowserOperator

        runtime = BrowserRuntime(REPO_ROOT / "models/browser-manifest.json")
        self.addCleanup(runtime.stop)
        runtime.start()
        result = asyncio.run(BrowserOperator(Settings(workspace=self.root / "unused", database=self.path), self.store, runtime, FixtureDecisionEngine()).form())
        self.assertEqual(result["status"], "succeeded")
        self.assertFalse(result["real_model"])
        self.run_id = result["run_id"]
        verifications = self.store.connection.execute("SELECT * FROM verifications WHERE run_id=?", (self.run_id,)).fetchall()
        self.assertEqual(len(verifications), 2)
        for index, verification in enumerate(verifications):
            self.verification = verification
            self.action = self.store.connection.execute("SELECT * FROM actions WHERE action_id=?", (verification["action_id"],)).fetchone()
            self.label_id = f"synthetic-browser-label-{index}"
            self.label()
            candidate = self.reviewed_candidate()
            self.assertEqual(candidate["target"]["correct_option"], self.action["actual_option"])
            self.assertTrue(self.preview(candidate)["derivation_matches"])
            self.assertFalse(self.preview(candidate)["training_ready"])


if __name__ == "__main__":
    unittest.main()
