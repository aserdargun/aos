import asyncio
import copy
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from aos.computer import WorkspaceRuntime
from aos.contracts import REPO_ROOT, Settings, canonical, digest, now
from aos.dataset import validator
from aos.dataset_audit import CURRENT_SCHEMA_VERSION, SUPPORTED_SCHEMA_VERSIONS, audit_database
from aos.dataset_reviews import ReviewAuthority, append_review, review_status, source_fingerprint
from aos.decision import FixtureDecisionEngine
from aos.operator import Operator
from aos.storage import TrajectoryStore

import test_scientist_admission_history as admission_fixture


class DatasetCanonicalMigrationTests(unittest.TestCase):
    def test_all_supported_legacy_versions_audit_without_upgrade_or_byte_changes(self):
        migrations = sorted((REPO_ROOT / 'database/migrations').glob('*.sql'))
        with tempfile.TemporaryDirectory(prefix='synthetic-legacy-dataset-audit-') as temporary:
            for version in SUPPORTED_SCHEMA_VERSIONS:
                path = Path(temporary) / f'synthetic-v{version}.sqlite3'
                with closing(sqlite3.connect(path)) as connection:
                    for migration in migrations[:version]:
                        connection.executescript(migration.read_text())
                original = path.read_bytes()
                with self.subTest(version=version):
                    report = audit_database(path)
                    self.assertEqual(len(report['snapshot']['migrations']), version)
                    self.assertEqual(path.read_bytes(), original)
                    with closing(sqlite3.connect(path.absolute().as_uri() + '?mode=ro', uri=True)) as reader:
                        self.assertEqual(reader.execute('SELECT count(*) FROM schema_migrations').fetchone()[0], version)
                        self.assertEqual(reader.execute(
                            "SELECT count(*) FROM sqlite_master WHERE name IN ('scientist_evidence_controls','scientist_evidence_responses')").fetchone()[0],
                            0 if version < 24 else 2)
                        self.assertEqual(reader.execute(
                            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='scientist_turn_resolutions'").fetchone()[0],
                            int(version >= 26))
                        self.assertEqual(reader.execute(
                            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='scientist_lab_readbacks'").fetchone()[0],
                            int(version >= 27))
                        self.assertEqual(reader.execute(
                            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='scientist_no_admission_closures'").fetchone()[0],
                            int(version >= 28))

    def test_explicit_v23_upgrade_preserves_original_intent_and_admission_without_adoption(self):
        from aos.scientist_admission_history import ScientistAdmissionHistory
        from aos.scientist_intents import ScientistIntentBinding, ScientistIntentJournal
        from aos.scientist_protocol import ScientistTurnRequest, scientist_request_frame, scientist_request_sha256
        from aos.scientist_transport import BrokerPeer

        with tempfile.TemporaryDirectory(prefix='synthetic-v23-dataset-audit-') as temporary:
            path = Path(temporary) / 'synthetic-v23.sqlite3'
            with closing(sqlite3.connect(path)) as connection:
                connection.row_factory = sqlite3.Row
                for migration in sorted((REPO_ROOT / 'database/migrations').glob('*.sql'))[:23]:
                    connection.executescript(migration.read_text())
                with connection:
                    connection.execute('INSERT INTO desktop_sessions VALUES(?,?,?,?,?,?,?,?,?)',
                        ('session', 'runtime', 'synthetic', 'AGENT', 'lease', 0, 'running', 'synthetic', 'synthetic'))
                store = SimpleNamespace(connection=connection)
                binding = ScientistIntentBinding(session_id='session', runtime_id='runtime', owner='AGENT',
                    lease_id='lease', generation=0, authorization_context_sha256='a' * 64)
                request = ScientistTurnRequest(request_id='b' * 32, profile_id='aos.decider.turn.v1',
                    deployment_digest='c' * 64, payload={'synthetic': 'No model inference or allocation'})
                peer = BrokerPeer(1234, 1000, 42, admission_fixture.BOOT_ID, 'd' * 32, '/synthetic')
                history = ScientistAdmissionHistory(store,
                    capture=lambda original, _binding, original_peer:
                        admission_fixture.admission_capture_fixture(original, original_peer),
                    verify_current=lambda *arguments: None, clock=lambda: 100)
                journal = ScientistIntentJournal(store, binding, admission_history=history)
                journal.persist_intent(scientist_request_frame(request)[:-1], scientist_request_sha256(request),
                    time.monotonic() + 60, peer)
                original = {table: [tuple(row) for row in connection.execute('SELECT * FROM ' + table)]
                            for table in ('scientist_turn_intents', 'scientist_admission_history')}
            original_bytes = path.read_bytes()
            self.assertEqual(len(audit_database(path)['snapshot']['migrations']), 23)
            self.assertEqual(path.read_bytes(), original_bytes)
            with closing(TrajectoryStore(path)) as upgraded:
                for table, rows in original.items():
                    self.assertEqual([tuple(row) for row in upgraded.connection.execute('SELECT * FROM ' + table)], rows)
                for table in ('scientist_evidence_controls', 'scientist_evidence_responses'):
                    self.assertEqual(upgraded.connection.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0)
                self.assertEqual(len(audit_database(path)['snapshot']['migrations']), CURRENT_SCHEMA_VERSION)
                self.assertEqual(upgraded.connection.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_explicit_v24_upgrade_preserves_retained_v2_control_and_response_bytes(self):
        self._assert_retained_evidence_upgrade(24)

    def test_explicit_v25_upgrade_preserves_retained_ack_without_resolving_original_fence(self):
        self._assert_retained_evidence_upgrade(25)

    def _assert_retained_evidence_upgrade(self, original_version):
        from aos.scientist_admission_history import ScientistAdmissionHistory
        from aos.scientist_evidence_journal import ScientistEvidenceJournal
        from aos.scientist_evidence_transport import ScientistEvidenceCodec
        from aos.scientist_intents import ScientistIntentBinding, ScientistIntentJournal
        from aos.scientist_protocol import ScientistTurnRequest, scientist_request_frame, scientist_request_sha256
        from aos.scientist_terminal import canonical as wire_canonical, digest as wire_digest
        from aos.scientist_transport import BrokerPeer

        example = json.loads((REPO_ROOT / 'examples/scientist_admission_history_v2.json').read_text())
        self.assertIs(example['synthetic_cpu_fixture'], True)
        self.assertIs(example['runtime_admission'], False)
        binding = ScientistIntentBinding.model_validate(example['intent_binding'], strict=True)
        request = ScientistTurnRequest.model_validate(example['request'], strict=True)
        capture = {key: example['record'][key] for key in
                   ('admission_binding', 'capability_sha256', 'capability_freshness')}
        peer = BrokerPeer(**{key: value for key, value in
                            capture['admission_binding']['server_generation'].items() if key != 'unit'})
        schema_bytes = (REPO_ROOT / 'schemas/scientist_evidence_transport.schema.json').read_bytes()
        codec = ScientistEvidenceCodec(schema_bytes,
            transport_schema_sha256=wire_digest(json.loads(schema_bytes)), evidence_schema_sha256='a' * 64)
        tables = ('scientist_turn_intents', 'scientist_admission_history',
                  'scientist_evidence_controls', 'scientist_evidence_responses')
        with tempfile.TemporaryDirectory(prefix=f'synthetic-v{original_version}-retained-evidence-') as temporary:
            path = Path(temporary) / f'synthetic-v{original_version}.sqlite3'
            with closing(sqlite3.connect(path)) as connection:
                connection.row_factory = sqlite3.Row
                for migration in sorted((REPO_ROOT / 'database/migrations').glob('*.sql'))[:original_version]:
                    connection.executescript(migration.read_text())
                with connection:
                    connection.execute('INSERT INTO desktop_sessions VALUES(?,?,?,?,?,?,?,?,?)',
                        (binding.session_id, binding.runtime_id, 'synthetic', binding.owner,
                         binding.lease_id, binding.generation, 'running', 'synthetic', 'synthetic'))
                store = SimpleNamespace(connection=connection)
                history = ScientistAdmissionHistory(store, capture=lambda *_arguments: copy.deepcopy(capture),
                    verify_current=lambda *_arguments: None, clock=lambda: 100, record_version='2.0')
                intent = ScientistIntentJournal(store, binding, admission_history=history)
                intent.persist_intent(scientist_request_frame(request)[:-1], scientist_request_sha256(request),
                    time.monotonic() + 60, peer)
                original, _checksum = history.read(request.request_id)
                control = {'schema': 'aos-scientist-control-evidence.v2', 'version': 2,
                    'op': 'reconcile', 'control_id': 'd' * 32, 'profile_id': request.profile_id,
                    'deployment_digest': request.deployment_digest,
                    'target': {'request_id': request.request_id, 'request_sha256': original.request_sha256,
                        'original_peer_generation_sha256': wire_digest(
                            original.admission_binding.caller_generation.model_dump(mode='json'))},
                    'expected_capability_sha256': 'e' * 64, 'evidence_schema_sha256': codec.evidence_schema_sha256,
                    'transport_schema_sha256': codec.transport_schema_sha256}
                evidence = ScientistEvidenceJournal(store, binding, codec=codec, admission_history=history,
                    verify_control=lambda *_arguments: None)
                frame = codec.encode_request(control)
                evidence.persist_intent(frame, hashlib.sha256(frame).hexdigest(), time.monotonic() + 5, peer)
                response = {'schema': control['schema'], 'version': 2, 'control_id': control['control_id'],
                    'op': control['op'], 'ok': True, 'capability_sha256': control['expected_capability_sha256'],
                    'data': {'schema': 'aos-scientist-terminal-evidence.v2', 'version': 2,
                        'target': control['target'], 'terminal_canonical': wire_canonical(
                            {'synthetic_migration_only': 'kanıt değil', 'seconds': 1.0}),
                        'allocation_canonical': None, 'drain_canonical': None,
                        'no_admission_canonical': None, 'result_canonical': None}, 'error': None}
                evidence.record_response(response, peer)
                retained = {table: [tuple(row) for row in connection.execute('SELECT * FROM ' + table)]
                            for table in tables}
                original_metadata = tuple(connection.execute('SELECT * FROM schema_migrations WHERE version=18').fetchone())
                original_table_sql = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='scientist_turn_intents'").fetchone()[0]
                self.assertEqual(retained['scientist_evidence_controls'][0][5].encode(), frame)
                self.assertEqual(retained['scientist_evidence_responses'][0][2], wire_canonical(response))
            before_audit = path.read_bytes()
            self.assertEqual(len(audit_database(path)['snapshot']['migrations']), original_version)
            self.assertEqual(path.read_bytes(), before_audit)
            with closing(TrajectoryStore(path)) as upgraded:
                for table, rows in retained.items():
                    self.assertEqual([tuple(row) for row in upgraded.connection.execute('SELECT * FROM ' + table)], rows)
                self.assertEqual(upgraded.connection.execute('PRAGMA foreign_keys').fetchone()[0], 1)
                self.assertEqual(upgraded.connection.execute('PRAGMA foreign_key_check').fetchall(), [])
                report = audit_database(path)
                validator('dataset_audit').validate(report)
                self.assertEqual(len(report['snapshot']['migrations']), CURRENT_SCHEMA_VERSION)
                self.assertFalse(report['training_ready'])
                self.assertEqual(tuple(upgraded.connection.execute(
                    'SELECT * FROM schema_migrations WHERE version=18').fetchone()), original_metadata)
                self.assertEqual(upgraded.connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='scientist_turn_intents'").fetchone()[0],
                    original_table_sql)
                self.assertEqual(upgraded.connection.execute('SELECT count(*) FROM scientist_turn_resolutions').fetchone()[0], 0)
                self.assertEqual(upgraded.connection.execute('SELECT state FROM scientist_turn_intents').fetchone()[0],
                                 'pending')
                for table in ('scientist_evidence_controls', 'scientist_evidence_responses'):
                    for statement in (f'UPDATE {table} SET created_at=created_at', f'DELETE FROM {table}'):
                        with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                            with upgraded.connection:
                                upgraded.connection.execute(statement)
                with upgraded.connection:
                    upgraded.connection.execute("UPDATE desktop_sessions SET owner='AGENT',status='running'")
                current = upgraded.connection.execute('SELECT * FROM desktop_sessions').fetchone()
                fresh_binding = binding.model_copy(update={field: current[field]
                    for field in ('owner', 'lease_id', 'generation')})
                next_request = request.model_copy(update={'request_id': 'f' * 32})
                next_row = list(retained['scientist_turn_intents'][0])
                next_row[0] = next_request.request_id
                next_row[2] = fresh_binding.model_dump_json()
                next_row[3] = scientist_request_frame(next_request)[:-1].decode()
                next_row[4] = scientist_request_sha256(next_request)
                with self.assertRaisesRegex(sqlite3.IntegrityError, 'trusted resolution before new work'):
                    with upgraded.connection:
                        upgraded.connection.execute('INSERT INTO scientist_turn_intents VALUES(?,?,?,?,?,?,?,?,?,?)', next_row)
                self.assertEqual(upgraded.connection.execute('SELECT count(*) FROM scientist_turn_resolutions').fetchone()[0], 0)
                for table, rows in retained.items():
                    self.assertEqual([tuple(row) for row in upgraded.connection.execute('SELECT * FROM ' + table)], rows)


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
            self.assertEqual(len(audit_database(legacy)["snapshot"]["migrations"]), CURRENT_SCHEMA_VERSION)
        finally:
            upgraded.close()

    def test_v22_readonly_audit_and_additive_admission_migration_preserve_legacy_intent(self):
        legacy = self.root / 'legacy-admission.sqlite'
        connection = sqlite3.connect(legacy)
        for migration in sorted((REPO_ROOT / 'database/migrations').glob('*.sql'))[:22]:
            connection.executescript(migration.read_text())
        binding = {'session_id': 'synthetic-session', 'runtime_id': 'synthetic-runtime',
                   'lease_id': 'synthetic-lease', 'generation': 0, 'owner': 'AGENT',
                   'authorization_context_sha256': 'a' * 64}
        request = {'version': 1, 'op': 'infer', 'request_id': 'b' * 32,
                   'profile_id': 'aos.decider.turn.v1', 'deployment_digest': 'c' * 64,
                   'payload': {'text': 'Synthetic legacy intent; no actual inference'}}
        with connection:
            connection.execute('INSERT INTO desktop_sessions VALUES(?,?,?,?,?,?,?,?,?)',
                ('synthetic-session', 'synthetic-runtime', 'synthetic-image', 'AGENT',
                 'synthetic-lease', 0, 'running', 'synthetic', 'synthetic'))
            connection.execute('INSERT INTO scientist_turn_intents VALUES(?,?,?,?,?,?,?,?,?,?)',
                (request['request_id'], binding['session_id'], canonical(binding), canonical(request),
                 digest(request), '{}', 1000.0, 'pending', None, 'synthetic'))
        original = connection.execute('SELECT * FROM scientist_turn_intents').fetchall()
        original_schema = connection.execute("SELECT sql FROM sqlite_master WHERE tbl_name='scientist_turn_intents' AND name NOT IN ('scientist_one_unresolved_turn','scientist_session_turns','scientist_resolved_intent_immutable') ORDER BY name").fetchall()
        connection.close()
        before = legacy.read_bytes()
        self.assertEqual(len(audit_database(legacy)['snapshot']['migrations']), 22)
        self.assertEqual(legacy.read_bytes(), before)
        upgraded = TrajectoryStore(legacy)
        try:
            self.assertEqual([tuple(row) for row in upgraded.connection.execute('SELECT * FROM scientist_turn_intents')], original)
            self.assertEqual([tuple(row) for row in upgraded.connection.execute("SELECT sql FROM sqlite_master WHERE tbl_name='scientist_turn_intents' AND name NOT IN ('scientist_one_unresolved_turn','scientist_session_turns','scientist_resolved_intent_immutable','scientist_observed_intent_immutable') ORDER BY name")], original_schema)
            self.assertEqual([tuple(row) for row in upgraded.connection.execute(
                "SELECT type,name FROM sqlite_master WHERE name IN ('scientist_one_unresolved_turn','scientist_session_turns') ORDER BY name")],
                [('trigger', 'scientist_one_unresolved_turn'), ('index', 'scientist_session_turns')])
            self.assertEqual(tuple(upgraded.connection.execute(
                "SELECT type,sql FROM sqlite_master WHERE name='scientist_resolved_intent_immutable'").fetchone()),
                ('trigger', 'CREATE TRIGGER scientist_resolved_intent_immutable BEFORE UPDATE ON scientist_turn_intents\n'
                 'WHEN EXISTS (SELECT 1 FROM scientist_turn_resolutions WHERE request_id=OLD.request_id)\n'
                 "BEGIN SELECT RAISE(ABORT,'Resolved Scientist original intent and receipt are immutable'); END"))
            self.assertEqual(tuple(upgraded.connection.execute(
                "SELECT type,sql FROM sqlite_master WHERE name='scientist_observed_intent_immutable'").fetchone()),
                ('trigger', 'CREATE TRIGGER scientist_observed_intent_immutable BEFORE UPDATE ON scientist_turn_intents\n'
                 'WHEN EXISTS (SELECT 1 FROM scientist_no_admission_closures WHERE request_id=OLD.request_id)\n'
                 "BEGIN SELECT RAISE(ABORT,'Observed Scientist original intent is immutable'); END"))
            self.assertEqual(upgraded.connection.execute('SELECT count(*) FROM scientist_no_admission_closures').fetchone()[0], 0)
            self.assertEqual(upgraded.connection.execute('SELECT count(*) FROM scientist_admission_history').fetchone()[0], 0)
            self.assertEqual(len(audit_database(legacy)['snapshot']['migrations']), CURRENT_SCHEMA_VERSION)
            self.assertEqual(upgraded.connection.execute('PRAGMA foreign_key_check').fetchall(), [])
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
