"""Actual AOS-synthetic sockets and original SQLite, not Scientist or GPU execution."""

import hashlib
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from aos.contracts import REPO_ROOT
from aos.scientist_admission_history import ScientistAdmissionHistory
from aos.scientist_desktop import ScientistDesktopBinding
from aos.scientist_intents import ScientistIntentJournal
from aos.scientist_protocol import scientist_request_frame
from aos.scientist_retained_host import ScientistRetainedHost
from aos.scientist_transport import ScientistAdmissionError, ScientistUncertainTurn

import test_scientist_evidence_transport as transport_cases
import test_scientist_retained_evidence_client as retained_cases


class ScientistRetainedHostIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.socket = retained_cases.ScientistRetainedEvidenceClientTests()
        self.socket.setUp()
        self.addCleanup(self.socket.doCleanups)
        self.fixture = self.socket.fixture
        self.resolution_authority = Mock(return_value=None)
        self.capability_id = 'd' * 32
        self.reconcile_id = 'e' * 32
        self.original_rows = self.fixture.rows()
        self.host = self.make_host()

    def make_host(self, **changes):
        options = {'socket_path': self.socket.server.path,
            'reviewed_schema_bytes': (REPO_ROOT / 'schemas/scientist_retained_evidence_transport.schema.json').read_bytes(),
            'transport_schema_sha256': retained_cases.RETAINED_PIN,
            'evidence_schema_sha256': transport_cases.EVIDENCE_PIN,
            'authenticator': self.socket.authenticator, 'verifier': self.socket.proof,
            'verify_control': self.socket.authority, 'verify_resolution': self.resolution_authority,
            'timeout_seconds': 2}
        return ScientistRetainedHost(self.fixture.store, self.fixture.history, self.fixture.binding,
                                     **(options | changes))

    def exchange_pair(self):
        capability = self.host.discover(self.fixture.request.request_id, self.capability_id)
        self.assertEqual(capability['data']['admission'], 'denied')
        return self.host.reconcile(self.fixture.request.request_id, self.capability_id, self.reconcile_id)

    def resolve(self):
        inspected = self.host.inspect(self.reconcile_id, capability_control_id=self.capability_id)
        return self.host.resolve(self.reconcile_id, self.capability_id,
                                 response_sha256=inspected['response_sha256'])

    def assert_original_pending(self):
        self.assertEqual(self.fixture.rows(), self.original_rows)
        self.assertEqual(self.fixture.store.connection.execute(
            'SELECT state,receipt_json FROM scientist_turn_intents').fetchone()[:], ('pending', None))

    def test_explicit_discover_reconcile_resolve_then_new_original_capture(self):
        self.assertEqual(self.socket.server.requests, [])
        self.socket.authority.assert_not_called()
        self.exchange_pair()
        self.assertEqual(self.socket.counts(), (2, 2))
        self.socket.physical.assert_not_called()
        self.fixture.source.assert_not_called()
        self.assert_original_pending()
        resolution = self.resolve()
        self.assertEqual(resolution['request_id'], self.fixture.request.request_id)
        self.assertGreaterEqual(self.socket.physical.call_count, 1)
        self.assert_original_pending()
        request = self.fixture.request.model_copy(update={'request_id': 'f' * 32})
        frame = scientist_request_frame(request)[:-1]
        journal = ScientistIntentJournal(self.fixture.store, self.fixture.binding,
                                        admission_history=self.fixture.history)
        journal.persist_intent(frame, hashlib.sha256(frame).hexdigest(), time.monotonic() + 60,
                               self.fixture.peer)
        record, checksum = self.fixture.history.read(request.request_id)
        self.assertEqual(record.schema_version, '2.0')
        self.assertNotEqual(checksum, self.fixture.original_sha)
        with self.assertRaises(ScientistAdmissionError):
            journal.verify_admission(self.fixture.request)
        self.assertEqual(len(self.socket.server.requests), 2)

    def test_wrong_target_schema_pin_or_default_authority_denied_before_connect(self):
        with patch('aos.scientist_evidence_client.socket.socket', side_effect=AssertionError('Must not connect')):
            for operation in (
                    lambda: self.host.discover('f' * 32, self.capability_id),
                    lambda: self.host.reconcile(self.fixture.request.request_id, 'f' * 32, self.reconcile_id),
                    lambda: self.make_host(transport_schema_sha256='0' * 64),
                    lambda: self.make_host(reviewed_schema_bytes=b'{}'),
                    lambda: self.make_host(verify_control=None)):
                with self.subTest(operation=operation), self.assertRaises((ScientistAdmissionError, TypeError, ValueError)):
                    operation()
            with self.assertRaises(ScientistAdmissionError):
                ScientistRetainedHost(self.fixture.store, self.fixture.history, self.fixture.binding,
                    socket_path=self.socket.server.path,
                    reviewed_schema_bytes=(REPO_ROOT / 'schemas/scientist_retained_evidence_transport.schema.json').read_bytes(),
                    transport_schema_sha256=retained_cases.RETAINED_PIN,
                    evidence_schema_sha256=transport_cases.EVIDENCE_PIN).discover(
                        self.fixture.request.request_id, self.capability_id)
        self.assertEqual(self.socket.counts(), (0, 0))
        self.assert_original_pending()

    def test_lost_ack_sticky_host_and_new_host_durable_pending_fence(self):
        self.socket.server.mode = 'disconnect'
        with self.assertRaises(ScientistUncertainTurn):
            self.host.discover(self.fixture.request.request_id, self.capability_id)
        self.assertEqual(self.host.uncertain_control_id, self.capability_id)
        self.assertTrue(self.host.inspect(self.capability_id)['pending'])
        self.assertEqual(self.socket.counts(), (1, 0))
        for host in (self.host, self.make_host()):
            for control_id in (self.capability_id, 'f' * 32):
                with self.subTest(host=host, control_id=control_id), self.assertRaises(ScientistAdmissionError):
                    host.discover(self.fixture.request.request_id, control_id)
        self.assertEqual(len(self.socket.server.requests), 1)
        self.assert_original_pending()

    def test_existing_ack_control_id_is_read_only_not_dispatch_authority(self):
        self.exchange_pair()
        before = tuple(self.fixture.store.connection.iterdump())
        fresh = self.make_host()
        with self.assertRaises(ScientistAdmissionError):
            fresh.discover(self.fixture.request.request_id, self.capability_id)
        with self.assertRaises(ScientistAdmissionError):
            fresh.reconcile(self.fixture.request.request_id, self.capability_id, self.reconcile_id)
        self.assertFalse(fresh.inspect(self.capability_id)['pending'])
        self.assertFalse(fresh.inspect(self.reconcile_id, capability_control_id=self.capability_id)['pending'])
        self.assertEqual(tuple(self.fixture.store.connection.iterdump()), before)
        self.assertEqual(len(self.socket.server.requests), 2)
        self.socket.physical.assert_not_called()

    def test_resolution_repeat_requires_fresh_rights_and_independent_proof(self):
        self.exchange_pair()
        result = self.resolve()
        physical_count = self.socket.physical.call_count
        self.assertEqual(self.resolve(), result)
        self.assertGreater(self.socket.physical.call_count, physical_count)
        self.resolution_authority.side_effect = ScientistAdmissionError('Synthetic resolution rights revoked')
        with self.assertRaises(ScientistAdmissionError):
            self.resolve()
        self.resolution_authority.side_effect = None
        self.fixture.source.side_effect = ScientistAdmissionError('Independent original budget source revoked')
        with self.assertRaises(ScientistAdmissionError):
            self.resolve()
        self.assertEqual(self.fixture.store.connection.execute('SELECT count(*) FROM scientist_turn_resolutions').fetchone()[0], 1)
        self.assert_original_pending()
        self.assertEqual(len(self.socket.server.requests), 2)

    def test_historical_inspection_never_refreshes_callbacks_or_resolves(self):
        self.exchange_pair()
        result = self.resolve()
        for callback in (self.socket.authority, self.resolution_authority, self.fixture.source,
                         self.fixture.resolver, self.socket.physical):
            callback.reset_mock()
            callback.side_effect = AssertionError('Historical inspection is not current authority')
        self.assertFalse(self.host.inspect(self.reconcile_id, capability_control_id=self.capability_id)['pending'])
        self.assertEqual(self.host.inspect_resolution(self.fixture.request.request_id, self.capability_id), result)
        self.assertEqual(len(self.socket.server.requests), 2)
        self.assert_original_pending()

    def desktop_factory(self, history=None):
        controller = SimpleNamespace(store=self.fixture.store,
            state=lambda: dict(self.fixture.store.connection.execute(
                "SELECT * FROM desktop_sessions WHERE session_id='session'").fetchone()))
        return ScientistDesktopBinding(controller, admission_history=history or self.fixture.history)

    def factory_options(self):
        return {'socket_path': self.socket.server.path,
            'reviewed_schema_bytes': (REPO_ROOT / 'schemas/scientist_retained_evidence_transport.schema.json').read_bytes(),
            'transport_schema_sha256': retained_cases.RETAINED_PIN,
            'evidence_schema_sha256': transport_cases.EVIDENCE_PIN}

    def test_desktop_factory_retains_original_store_without_implicit_network_or_authority(self):
        before = tuple(self.fixture.store.connection.iterdump())
        factory = self.desktop_factory()
        host = factory.create_retained_host(self.fixture.binding, **self.factory_options())
        self.assertIsInstance(host, ScientistRetainedHost)
        self.assertIs(host.store, self.fixture.store)
        self.assertIs(host.history, self.fixture.history)
        self.assertIsNot(host.binding, self.fixture.binding)
        self.assertEqual(host.binding, self.fixture.binding)
        with patch('aos.scientist_evidence_client.socket.socket', side_effect=AssertionError('No implicit authority')):
            with self.assertRaises(ScientistAdmissionError):
                host.discover(self.fixture.request.request_id, self.capability_id)
        self.assertEqual(tuple(self.fixture.store.connection.iterdump()), before)
        self.assertEqual(self.socket.server.requests, [])

    def test_desktop_factory_rejects_stale_binding_legacy_history_or_wrong_store(self):
        factory = self.desktop_factory()
        for changes in ({'generation': 1}, {'lease_id': 'other'}, {'owner': 'HUMAN'},
                        {'session_id': 'other'}, {'runtime_id': 'other'}):
            with self.subTest(changes=changes), self.assertRaises(ScientistAdmissionError):
                factory.create_retained_host(self.fixture.binding.model_copy(update=changes), **self.factory_options())
        legacy = ScientistAdmissionHistory(self.fixture.store)
        with self.assertRaises(ScientistAdmissionError):
            self.desktop_factory(legacy).create_retained_host(self.fixture.binding, **self.factory_options())
        factory.controller.store = object()
        with self.assertRaises(ScientistAdmissionError):
            factory.create_retained_host(self.fixture.binding, **self.factory_options())
        self.assertEqual(self.socket.server.requests, [])
        self.assert_original_pending()
