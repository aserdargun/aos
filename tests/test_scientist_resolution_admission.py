"""AOS-synthetic retained sockets and SQLite admission; no native or GPU proof."""

from copy import deepcopy
import hashlib
import sqlite3
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from aos.scientist_desktop import ScientistDesktopScheduler
from aos.scientist_intents import ScientistIntentJournal
from aos.scientist_inventory import scientist_inference_inventory
from aos.scientist_protocol import scientist_request_frame
from aos.scientist_resolution import ScientistResolutionJournal
from aos.scientist_transport import ScientistAdmissionError

import test_scientist_retained_evidence_client as retained_cases


class ScientistResolutionAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.socket = retained_cases.ScientistRetainedEvidenceClientTests()
        self.socket.setUp()
        self.addCleanup(self.socket.doCleanups)
        self.fixture = self.socket.fixture
        self.store = self.fixture.store
        self.evidence, self.control = self.socket.capability_then_reconcile()
        self.response = self.socket.client(self.evidence).exchange(self.control)
        self.response_sha256 = self.evidence.inspect(self.control['control_id'])['response_sha256']
        self.authority = Mock(return_value=None)
        self.resolution = ScientistResolutionJournal(self.evidence, verifier=self.socket.proof,
                                                     verify_resolution=self.authority)
        self.original_rows = self.fixture.rows()

    def resolve(self):
        return self.resolution.resolve(self.control['control_id'], response_sha256=self.response_sha256)

    def count(self):
        return self.store.connection.execute('SELECT count(*) FROM scientist_turn_resolutions').fetchone()[0]

    def inventory(self):
        return scientist_inference_inventory(SimpleNamespace(store=self.store, session_id='session'))

    def assert_blocked(self):
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.fixture.rows(), self.original_rows)
        request = self.fixture.request.model_copy(update={'request_id': 'f' * 32})
        with self.assertRaises(ScientistAdmissionError):
            self.fixture.journal.verify_admission(request)

    def test_actual_retained_ack_resolution_then_fresh_identity_never_original_replay(self):
        result = self.resolve()
        self.assertEqual(result['response_sha256'], self.response_sha256)
        self.assertEqual(result['admission_record_sha256'], self.fixture.original_sha)
        self.assertEqual(self.fixture.rows(), self.original_rows)
        self.assertGreaterEqual(self.socket.physical.call_count, 1)
        self.assertEqual(len(self.socket.server.requests), 2)
        self.assertEqual(self.count(), 1)
        with self.assertRaises(ScientistAdmissionError):
            self.fixture.journal.verify_admission(self.fixture.request)
        payload = deepcopy(self.fixture.request.payload)
        payload['request']['options'][0]['id'] = 'fresh_synthetic_choice'
        request = self.fixture.request.model_copy(update={'request_id': 'f' * 32, 'payload': payload})
        frame = scientist_request_frame(request)[:-1]
        fresh = ScientistIntentJournal(self.store, self.fixture.binding, admission_history=self.fixture.history)
        fresh.verify_admission(request)
        fresh.persist_intent(frame, hashlib.sha256(frame).hexdigest(), time.monotonic() + 60, self.fixture.peer)
        original, checksum = self.fixture.history.read(request.request_id)
        self.assertEqual(original.schema_version, '2.0')
        self.assertNotEqual(checksum, self.fixture.original_sha)
        self.assertEqual(original.request_id, request.request_id)
        self.assertEqual(self.store.connection.execute(
            'SELECT state,receipt_json FROM scientist_turn_intents WHERE request_id=?',
            (self.fixture.request.request_id,)).fetchone()[:], ('pending', None))
        self.assertEqual(len(self.socket.server.requests), 2)

    def test_successful_ack_without_independent_verifier_or_authority_remains_blocked(self):
        for resolution in (ScientistResolutionJournal(self.evidence, verify_resolution=self.authority),
                           ScientistResolutionJournal(self.evidence, verifier=self.socket.proof)):
            with self.subTest(resolution=resolution), self.assertRaises(ScientistAdmissionError):
                resolution.resolve(self.control['control_id'], response_sha256=self.response_sha256)
        self.socket.physical.assert_not_called()
        self.fixture.source.assert_not_called()
        self.assert_blocked()

    def test_wrong_ack_hash_and_capability_ack_cannot_resolve(self):
        with self.assertRaises(ScientistAdmissionError):
            self.resolution.resolve(self.control['control_id'], response_sha256='0' * 64)
        capability_sha = self.evidence.inspect(self.socket.control['control_id'])['response_sha256']
        with self.assertRaises(ScientistAdmissionError):
            self.resolution.resolve(self.socket.control['control_id'], response_sha256=capability_sha)
        self.socket.physical.assert_not_called()
        self.fixture.source.assert_not_called()
        self.assert_blocked()

    def test_failed_independent_cleanup_and_revoked_current_authority_keep_original_fence(self):
        self.socket.physical.side_effect = ScientistAdmissionError('Synthetic independent drain is not verified')
        with self.assertRaises(ScientistAdmissionError):
            self.resolve()
        self.assert_blocked()
        self.socket.physical.reset_mock()
        self.authority.side_effect = ScientistAdmissionError('Synthetic current authority revoked')
        with self.assertRaises(ScientistAdmissionError):
            self.resolve()
        self.socket.physical.assert_not_called()
        self.assert_blocked()

    def test_takeover_stale_generation_and_pending_control_prevent_resolution(self):
        with self.store.connection:
            self.store.connection.execute("UPDATE desktop_sessions SET generation=1 WHERE session_id='session'")
        with self.assertRaises(ScientistAdmissionError):
            self.resolve()
        self.assertEqual(self.count(), 0)
        self.socket.physical.assert_not_called()
        with self.store.connection:
            self.store.connection.execute("UPDATE desktop_sessions SET generation=0 WHERE session_id='session'")
        request = self.socket.control | {'control_id': 'f' * 32}
        frame = self.evidence.codec.encode_request(request)
        self.evidence.persist_intent(frame, hashlib.sha256(frame).hexdigest(), time.monotonic() + 2,
                                     self.socket.peer)
        with self.assertRaises(ScientistAdmissionError):
            self.resolve()
        self.assert_blocked()
        self.assertEqual(len(self.socket.server.requests), 2)

    def test_revocation_after_physical_observation_rolls_back_resolution(self):
        retained_observation = self.socket.physical.side_effect

        def observed_then_revoked(*arguments):
            retained_observation(*arguments)
            self.authority.side_effect = ScientistAdmissionError('Synthetic authority revoked after physical observation')

        self.socket.physical.side_effect = observed_then_revoked
        with self.assertRaises(ScientistAdmissionError):
            self.resolve()
        self.assertGreaterEqual(self.socket.physical.call_count, 1)
        self.assert_blocked()
        self.assertEqual(len(self.socket.server.requests), 2)

    def test_raw_sql_same_session_cannot_bypass_unresolved_constraint(self):
        with self.assertRaises(sqlite3.IntegrityError), self.store.connection:
            self.store.connection.execute(
                "INSERT INTO scientist_turn_intents SELECT ?,session_id,binding_json,request_json,"
                "request_sha256,broker_peer_json,deadline,state,receipt_json,created_at "
                "FROM scientist_turn_intents WHERE request_id=?", ('f' * 32, self.fixture.request.request_id))
        self.assert_blocked()

    def test_historical_inventory_and_inspection_are_callback_free_without_gpu_authority(self):
        before = self.inventory()
        self.assertEqual((before['historical_count'], before['resolved_count'], before['unresolved_count']), (1, 0, 1))
        result = self.resolve()
        for callback in (self.authority, self.socket.authority, self.fixture.source,
                         self.fixture.resolver, self.socket.physical):
            callback.reset_mock()
            callback.side_effect = AssertionError('Historical inspection must not refresh authority')
        self.assertEqual(self.resolution.inspect(self.fixture.request.request_id), result)
        after = self.inventory()
        self.assertEqual((after['historical_count'], after['resolved_count'], after['unresolved_count']), (1, 1, 0))
        self.assertTrue(after['intents'][0]['resolution_recorded'])
        self.assertFalse(after['admission_blocked'])
        self.assertFalse(after['joint_runtime_admitted'])
        self.assertFalse(after['gpu_release_verified'])
        self.assertEqual(self.fixture.rows(), self.original_rows)
        for statement in ('UPDATE scientist_turn_resolutions SET created_at=created_at',
                          'DELETE FROM scientist_turn_resolutions'):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError), self.store.connection:
                self.store.connection.execute(statement)

    def test_actual_scheduler_global_gate_blocks_other_session_until_proof_resolution(self):
        scheduler = ScientistDesktopScheduler.__new__(ScientistDesktopScheduler)
        scheduler.store = self.store
        scheduler.engine = object()
        scheduler.vision_supervisor = object()
        confirm = Mock(return_value=None)
        scheduler.scientist_binding = SimpleNamespace(engine=scheduler.engine,
            vision_supervisor=scheduler.vision_supervisor, profiles={'synthetic': 'not_native'}, confirm_runtime=confirm)
        scheduler.controller = SimpleNamespace(session_id='different_synthetic_session')
        with self.assertRaises(ScientistAdmissionError):
            scheduler._scientist_admission()
        confirm.assert_called_once()
        self.resolve()
        scheduler._scientist_admission()
        self.assertEqual(confirm.call_count, 2)
        self.assertEqual(len(self.socket.server.requests), 2)

    def test_pending_control_after_resolution_still_blocks_inventory_and_scheduler(self):
        self.resolve()
        request = self.socket.control | {'control_id': 'f' * 32}
        frame = self.evidence.codec.encode_request(request)
        self.evidence.persist_intent(frame, hashlib.sha256(frame).hexdigest(), time.monotonic() + 2,
                                     self.socket.peer)
        inventory = self.inventory()
        self.assertEqual(inventory['unresolved_count'], 0)
        self.assertEqual(inventory['resolved_count'], 1)
        self.assertEqual(inventory['evidence_controls']['pending_count'], 1)
        self.assertTrue(inventory['admission_blocked'])
        self.assertFalse(inventory['gpu_release_verified'])
        scheduler = ScientistDesktopScheduler.__new__(ScientistDesktopScheduler)
        scheduler.store = self.store
        scheduler.engine = object()
        scheduler.vision_supervisor = object()
        scheduler.scientist_binding = SimpleNamespace(engine=scheduler.engine,
            vision_supervisor=scheduler.vision_supervisor, profiles={'synthetic': 'not_native'},
            confirm_runtime=Mock(return_value=None))
        scheduler.controller = SimpleNamespace(session_id='different_synthetic_session')
        with self.assertRaises(ScientistAdmissionError):
            scheduler._scientist_admission()
        self.assertEqual(len(self.socket.server.requests), 2)
        self.assertEqual(self.fixture.rows(), self.original_rows)
