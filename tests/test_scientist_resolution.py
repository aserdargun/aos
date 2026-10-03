"""Synthetic original SQLite resolution and proof callbacks, never physical GPU proof."""

from copy import deepcopy
import hashlib
import sqlite3
import time
import unittest
from unittest.mock import Mock

from aos.contracts import REPO_ROOT
from aos.scientist_evidence_journal import ScientistEvidenceJournal
from aos.scientist_intents import scientist_unresolved_predicate
from aos.scientist_protocol import ScientistTurnReceipt, scientist_request_frame, scientist_request_sha256
from aos.scientist_resolution import ScientistResolutionJournal
from aos.scientist_retained_evidence_transport import ScientistRetainedEvidenceCodec
from aos.scientist_terminal import canonical, digest
from aos.scientist_transport import ScientistAdmissionError

import test_scientist_budget_witness as budget_cases
from test_scientist_retained_evidence_transport import TRANSPORT_PIN, EVIDENCE_PIN


class ScientistResolutionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = budget_cases.ScientistBudgetWitnessTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.proof, self.witness, self.envelope, self.physical = self.fixture.retained_fixture()
        binding = self.fixture.original.admission_binding.model_dump(mode='json')
        self.capability = {'target': deepcopy(self.witness['target']), 'control_binding': binding,
            'operations': ['cancel', 'reconcile', 'status'], 'boot_id': binding['server_generation']['boot_id'],
            'issued_boottime': 100.125, 'expires_boottime': 160.125}
        self.codec = ScientistRetainedEvidenceCodec(
            (REPO_ROOT / 'schemas/scientist_retained_evidence_transport.schema.json').read_bytes(),
            transport_schema_sha256=TRANSPORT_PIN, evidence_schema_sha256=EVIDENCE_PIN,
            expected_capability=self.capability)
        self.authority = Mock(return_value=None)
        self.resolution_authority = Mock(return_value=None)
        self.evidence = ScientistEvidenceJournal(self.store, self.fixture.binding, codec=self.codec,
            admission_history=self.fixture.history, verify_control=self.authority)
        self.control = {'schema': 'aos-scientist-control-evidence.v3', 'version': 3, 'op': 'reconcile',
            'control_id': 'd' * 32, 'profile_id': self.fixture.request.profile_id,
            'deployment_digest': self.fixture.request.deployment_digest, 'target': deepcopy(self.witness['target']),
            'expected_capability_sha256': digest(self.capability), 'evidence_schema_sha256': EVIDENCE_PIN,
            'transport_schema_sha256': TRANSPORT_PIN}
        self.response = {'schema': self.control['schema'], 'version': 3, 'op': 'reconcile',
            'control_id': self.control['control_id'], 'ok': True, 'error': None,
            'capability_sha256': digest(self.capability),
            'data': {'evidence': self.envelope, 'original_budget_witness': self.witness}}
        frame = self.codec.encode_request(self.control)
        self.evidence.persist_intent(frame, hashlib.sha256(frame).hexdigest(), time.monotonic() + 5, self.fixture.peer)
        self.evidence.record_response(self.response, self.fixture.peer)
        self.response_sha = digest(self.response)
        self.journal = ScientistResolutionJournal(self.evidence, verifier=self.proof,
                                                  verify_resolution=self.resolution_authority)
        self.before = self.fixture.rows()

    def resolve(self, journal=None, **changes):
        return (journal or self.journal).resolve(self.control['control_id'],
                                                **({'response_sha256': self.response_sha} | changes))

    def count(self):
        return self.store.connection.execute('SELECT count(*) FROM scientist_turn_resolutions').fetchone()[0]

    def next_request(self):
        return self.fixture.request.model_copy(update={'request_id': 'f' * 32}, deep=True)

    def assert_unresolved(self):
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.fixture.rows(), self.before)
        with self.assertRaises(ScientistAdmissionError):
            self.fixture.journal.verify_admission(self.next_request())

    def test_resolution_preserves_original_bytes_and_independent_sqlite_readback(self):
        original_ack = self.evidence.inspect(self.control['control_id'])
        result = self.resolve()
        self.assertEqual(self.fixture.rows(), self.before)
        self.assertEqual(self.evidence.inspect(self.control['control_id']), original_ack)
        self.assertEqual(result['terminal_json'], self.envelope['terminal_canonical'])
        self.assertEqual(result['budget_witness_json'], canonical(self.witness))
        self.assertEqual(self.physical.call_count, 2)
        connection = sqlite3.connect(self.fixture.path)
        try:
            connection.row_factory = sqlite3.Row
            self.assertEqual(dict(connection.execute('SELECT * FROM scientist_turn_resolutions').fetchone()), result)
            self.assertEqual(connection.execute('SELECT state FROM scientist_turn_intents').fetchone()[0], 'pending')
        finally:
            connection.close()
        self.fixture.journal.verify_admission(self.next_request())

    def test_default_authority_or_missing_verifier_never_resolves_ack(self):
        for journal in (ScientistResolutionJournal(self.evidence),
                        ScientistResolutionJournal(self.evidence, verifier=self.proof),
                        ScientistResolutionJournal(self.evidence, verify_resolution=self.resolution_authority)):
            with self.assertRaises(ScientistAdmissionError): self.resolve(journal)
        self.physical.assert_not_called()
        self.assert_unresolved()

    def test_wrong_expected_ack_stale_generation_and_revoked_authority_fail_closed(self):
        with self.assertRaises(ScientistAdmissionError): self.resolve(response_sha256='f' * 64)
        self.resolution_authority.side_effect = ScientistAdmissionError('Synthetic current rights revoked')
        with self.assertRaises(ScientistAdmissionError): self.resolve()
        self.resolution_authority.side_effect = None
        self.store.connection.execute('UPDATE desktop_sessions SET generation=generation+1')
        self.store.connection.commit()
        self.before = self.fixture.rows()
        with self.assertRaises(ScientistAdmissionError): self.resolve()
        self.physical.assert_not_called()
        self.assert_unresolved()

    def test_physical_failure_and_post_insert_revocation_roll_back_only_resolution(self):
        self.physical.side_effect = ScientistAdmissionError('Synthetic physical cleanup failed')
        with self.assertRaises(ScientistAdmissionError): self.resolve()
        self.assert_unresolved()
        self.physical.side_effect = [None, ScientistAdmissionError('Synthetic post-insert proof revoked')]
        with self.assertRaises(ScientistAdmissionError): self.resolve()
        self.assert_unresolved()
        self.assertFalse(self.store.connection.in_transaction)

    def test_callback_takeover_is_rolled_back_and_original_fence_remains(self):
        def revoke(*_arguments):
            self.store.connection.execute('UPDATE desktop_sessions SET generation=generation+1')
        self.physical.side_effect = revoke
        with self.assertRaises(ScientistAdmissionError): self.resolve()
        self.assert_unresolved()
        self.assertEqual(self.store.connection.execute('SELECT generation FROM desktop_sessions').fetchone()[0],
                         self.fixture.binding.generation)

    def test_exact_retry_rechecks_proof_and_rights_without_new_row_or_inference(self):
        first = self.resolve()
        self.assertEqual(self.resolve(), first)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.physical.call_count, 4)
        self.assertEqual(self.fixture.rows(), self.before)
        self.resolution_authority.side_effect = ScientistAdmissionError('Synthetic revocation')
        with self.assertRaises(ScientistAdmissionError): self.resolve()
        self.assertEqual(self.count(), 1)

    def test_historical_inspection_never_calls_authority_and_corruption_denies(self):
        result = self.resolve()
        for callback in (self.authority, self.resolution_authority, self.physical, self.fixture.source, self.fixture.resolver):
            callback.side_effect = AssertionError('Historical inspection must not call authority')
        self.store.connection.execute('UPDATE desktop_sessions SET generation=generation+1')
        self.store.connection.commit()
        self.assertEqual(self.journal.inspect(self.fixture.request.request_id), result)
        self.store.connection.execute('DROP TRIGGER scientist_resolution_no_update')
        self.store.connection.execute("UPDATE scientist_turn_resolutions SET budget_witness_sha256=?", ('f' * 64,))
        self.store.connection.commit()
        with self.assertRaises(ScientistAdmissionError): self.journal.inspect(self.fixture.request.request_id)

    def test_append_only_sql_and_original_receipt_cannot_change_after_resolution(self):
        self.resolve()
        for statement in ('DELETE FROM scientist_turn_resolutions',
                          "UPDATE scientist_turn_resolutions SET created_at='changed'",
                          "UPDATE scientist_turn_intents SET state='receipt_recorded',receipt_json='{}'"):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                self.store.connection.execute(statement)
            self.store.connection.rollback()
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.fixture.rows(), self.before)

    def test_original_receipt_must_match_completed_terminal_and_stays_byte_exact(self):
        receipt = ScientistTurnReceipt.model_validate({'version': 1, 'request_id': self.fixture.request.request_id,
            'profile_id': self.fixture.request.profile_id, 'deployment_digest': self.fixture.request.deployment_digest,
            **self.fixture.result}, strict=True)
        self.fixture.journal.record_receipt(receipt, self.fixture.peer)
        before = self.fixture.rows()
        self.resolve()
        self.assertEqual(self.fixture.rows(), before)

    def test_late_genuine_receipt_is_denied_under_still_current_original_binding(self):
        self.resolve()
        receipt = ScientistTurnReceipt.model_validate({'version': 1, 'request_id': self.fixture.request.request_id,
            'profile_id': self.fixture.request.profile_id, 'deployment_digest': self.fixture.request.deployment_digest,
            **self.fixture.result}, strict=True)
        before = self.fixture.rows()
        with self.assertRaises(ScientistAdmissionError):
            self.fixture.journal.record_receipt(receipt, self.fixture.peer)
        self.assertEqual(self.fixture.rows(), before)

    def test_wrong_original_receipt_cannot_be_overwritten_by_completed_evidence(self):
        receipt = ScientistTurnReceipt.model_validate({'version': 1, 'request_id': self.fixture.request.request_id,
            'profile_id': self.fixture.request.profile_id, 'deployment_digest': self.fixture.request.deployment_digest,
            **self.fixture.result}, strict=True)
        changed = receipt.model_dump(mode='json')
        changed['generation']['invocation_id'] = 'f' * 32
        self.fixture.journal.record_receipt(ScientistTurnReceipt.model_validate(changed, strict=True), self.fixture.peer)
        before = self.fixture.rows()
        with self.assertRaises(ScientistAdmissionError): self.resolve()
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.fixture.rows(), before)
        self.physical.assert_not_called()

    def test_pending_control_on_same_target_still_blocks_resolution(self):
        next_control = self.control | {'control_id': 'e' * 32}
        frame = self.codec.encode_request(next_control)
        self.evidence.persist_intent(frame, hashlib.sha256(frame).hexdigest(), time.monotonic() + 5, self.fixture.peer)
        with self.assertRaises(ScientistAdmissionError): self.resolve()
        self.assert_unresolved()

    def test_fresh_next_intent_requires_current_agent_and_same_old_id_never_replays(self):
        self.resolve()
        with self.assertRaises(ScientistAdmissionError): self.fixture.journal.verify_admission(self.fixture.request)
        request = self.next_request()
        self.fixture.journal.persist_intent(scientist_request_frame(request)[:-1], scientist_request_sha256(request),
                                            time.monotonic() + 30, self.fixture.peer)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM scientist_turn_intents').fetchone()[0], 2)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM scientist_turn_intents WHERE '
                         + scientist_unresolved_predicate(self.store.connection)).fetchone()[0], 1)
        with self.assertRaises(ScientistAdmissionError):
            self.fixture.journal.verify_admission(request.model_copy(update={'request_id': 'e' * 32}))

    def test_legacy_database_without_resolution_table_remains_fenced(self):
        self.store.connection.execute('DROP TABLE scientist_turn_resolutions')
        self.assertEqual(scientist_unresolved_predicate(self.store.connection), '1=1')
        with self.assertRaises(ScientistAdmissionError): self.fixture.journal.verify_admission(self.next_request())
