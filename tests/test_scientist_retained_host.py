"""Synthetic trusted-host composition on an owned CPU socket and original SQLite."""

from pathlib import Path
import threading
import unittest
from unittest.mock import Mock, patch

from aos.contracts import REPO_ROOT
from aos.scientist_evidence_journal import _deny_control
from aos.scientist_resolution import _deny_resolution
from aos.scientist_retained_host import ScientistRetainedHost
from aos.scientist_transport import ScientistAdmissionError

import test_scientist_retained_evidence_client as retained_cases
from test_scientist_retained_evidence_transport import TRANSPORT_PIN, EVIDENCE_PIN


class ScientistRetainedHostTests(unittest.TestCase):
    def setUp(self):
        self.socket = retained_cases.ScientistRetainedEvidenceClientTests()
        self.socket.setUp()
        self.addCleanup(self.socket.doCleanups)
        self.fixture = self.socket.fixture
        self.resolution_authority = Mock(return_value=None)
        self.options = {'socket_path': self.socket.server.path,
            'reviewed_schema_bytes': (REPO_ROOT / 'schemas/scientist_retained_evidence_transport.schema.json').read_bytes(),
            'transport_schema_sha256': TRANSPORT_PIN, 'evidence_schema_sha256': EVIDENCE_PIN,
            'authenticator': self.socket.authenticator, 'verifier': self.socket.proof,
            'verify_control': self.socket.authority, 'verify_resolution': self.resolution_authority,
            'timeout_seconds': 2}
        self.host = self.make_host()
        self.request_id = self.fixture.request.request_id
        self.capability_id, self.reconcile_id = 'd' * 32, 'e' * 32

    def make_host(self, binding=None, **changes):
        return ScientistRetainedHost(self.fixture.store, self.fixture.history, binding or self.fixture.binding,
                                      **(self.options | changes))

    def discover(self, host=None):
        return (host or self.host).discover(self.request_id, self.capability_id)

    def reconcile(self, host=None):
        return (host or self.host).reconcile(self.request_id, self.capability_id, self.reconcile_id)

    def test_constructor_never_uses_socket_authority_or_changes_caller_store(self):
        before = self.fixture.rows(), self.fixture.store.connection.total_changes
        with patch('aos.scientist_evidence_client.socket.socket', side_effect=AssertionError('No connection')):
            host = self.make_host()
            self.assertIsNone(host.uncertain_control_id)
        self.assertEqual((self.fixture.rows(), self.fixture.store.connection.total_changes), before)
        self.socket.authority.assert_not_called()
        self.resolution_authority.assert_not_called()
        self.socket.physical.assert_not_called()
        self.socket.authenticator.authenticate.assert_not_called()

    def test_each_missing_provider_denies_before_connect_or_intent(self):
        for changes in ({'authenticator': None}, {'verifier': None},
                        {'verify_control': _deny_control}, {'verify_resolution': _deny_resolution}):
            with self.subTest(changes=changes), patch('aos.scientist_evidence_client.socket.socket') as connect:
                with self.assertRaises(ScientistAdmissionError): self.discover(self.make_host(**changes))
                connect.assert_not_called()
        self.assertEqual(self.socket.counts(), (0, 0))

    def test_constructor_requires_explicit_pins_path_timeout_and_original_history(self):
        for changes in ({'transport_schema_sha256': 'f' * 64}, {'socket_path': Path('relative.sock')},
                        {'timeout_seconds': 11}, {'timeout_seconds': True}, {'reviewed_schema_bytes': 5},
                        {'verify_control': None}, {'authenticator': object()}):
            with self.subTest(changes=changes), self.assertRaises((ScientistAdmissionError, ValueError, TypeError)):
                self.make_host(**changes)
        with self.assertRaises(ScientistAdmissionError):
            ScientistRetainedHost(object(), self.fixture.history, self.fixture.binding, **self.options)
        self.assertEqual(self.socket.server.requests, [])

    def test_missing_schema26_metadata_is_denied_without_upgrade_or_connection(self):
        connection = self.fixture.store.connection
        connection.execute('DELETE FROM schema_migrations WHERE version=26')
        connection.commit()
        before = tuple(connection.iterdump()), connection.total_changes
        with patch('aos.scientist_evidence_client.socket.socket') as connect:
            with self.assertRaises(ScientistAdmissionError): self.make_host()
            connect.assert_not_called()
        self.assertEqual((tuple(connection.iterdump()), connection.total_changes), before)

    def test_missing_required_resolution_table_is_denied_before_connect(self):
        connection = self.fixture.store.connection
        connection.execute('DROP TABLE scientist_turn_resolutions')
        connection.commit()
        before = connection.total_changes
        with patch('aos.scientist_evidence_client.socket.socket') as connect:
            with self.assertRaises(ScientistAdmissionError): self.make_host()
            connect.assert_not_called()
        self.assertEqual(connection.total_changes, before)

    def test_invalid_ids_missing_capability_and_foreign_host_scope_deny_before_connect(self):
        with patch('aos.scientist_evidence_client.socket.socket') as connect:
            for invoke in (lambda: self.host.discover(self.request_id, 'bad-id'),
                           lambda: self.host.discover('f' * 32, self.capability_id),
                           lambda: self.host.reconcile(self.request_id, self.capability_id, self.reconcile_id),
                           lambda: self.discover(self.make_host(binding=self.fixture.binding.model_copy(
                               update={'runtime_id': 'foreign'})))):
                with self.assertRaises(ScientistAdmissionError): invoke()
            connect.assert_not_called()
        self.assertEqual(self.socket.counts(), (0, 0))

    def test_repeated_control_id_even_after_ack_is_denied_without_new_connection(self):
        self.discover()
        before = self.socket.counts()
        with patch('aos.scientist_evidence_client.socket.socket') as connect:
            for host in (self.host, self.make_host()):
                with self.assertRaises(ScientistAdmissionError): self.discover(host)
            connect.assert_not_called()
        self.assertEqual(self.socket.counts(), before)
        self.assertEqual(len(self.socket.server.requests), 1)

    def test_selected_capability_must_be_actual_stored_capability_operation(self):
        self.discover()
        self.reconcile()
        with patch('aos.scientist_evidence_client.socket.socket') as connect:
            with self.assertRaises(ScientistAdmissionError):
                self.host.reconcile(self.request_id, self.reconcile_id, 'f' * 32)
            with self.assertRaises(ScientistAdmissionError):
                self.host.inspect(self.reconcile_id, capability_control_id=self.reconcile_id)
            connect.assert_not_called()
        self.assertEqual(self.socket.counts(), (2, 2))

    def test_historical_inspection_never_uses_current_authority_socket_or_resolution(self):
        capability = self.discover()
        reconcile = self.reconcile()
        self.fixture.store.connection.execute('UPDATE desktop_sessions SET generation=generation+1')
        self.fixture.store.connection.commit()
        for callback in (self.socket.authority, self.socket.physical, self.fixture.source,
                         self.fixture.resolver, self.resolution_authority):
            callback.side_effect = AssertionError('Historical inspection cannot call authority')
        host = self.make_host(authenticator=None, verifier=None, verify_control=_deny_control,
                              verify_resolution=_deny_resolution)
        with patch('aos.scientist_evidence_client.socket.socket', side_effect=AssertionError('No connection')):
            self.assertEqual(host.inspect(self.capability_id)['response'], capability)
            self.assertEqual(host.inspect(self.reconcile_id, capability_control_id=self.capability_id)['response'], reconcile)
            with self.assertRaises(ScientistAdmissionError):
                self.make_host(binding=self.fixture.binding.model_copy(update={'runtime_id': 'foreign'})).inspect(self.capability_id)
        self.assertEqual(self.socket.counts(), (2, 2))

    def test_resolution_and_historical_resolution_inspection_use_no_network(self):
        self.discover()
        self.reconcile()
        checksum = self.host.inspect(self.reconcile_id, capability_control_id=self.capability_id)['response_sha256']
        with patch('aos.scientist_evidence_client.socket.socket', side_effect=AssertionError('No network for resolution')):
            result = self.host.resolve(self.reconcile_id, self.capability_id, response_sha256=checksum)
            self.assertEqual(self.host.resolve(self.reconcile_id, self.capability_id, response_sha256=checksum), result)
            for callback in (self.socket.authority, self.socket.physical, self.fixture.source,
                             self.fixture.resolver, self.resolution_authority):
                callback.side_effect = AssertionError('Historical inspection cannot call authority')
            self.assertEqual(self.host.inspect_resolution(self.request_id, self.capability_id), result)
            with self.assertRaises(ScientistAdmissionError):
                self.host.discover(self.request_id, 'f' * 32)
        self.assertEqual(len(self.socket.server.requests), 2)
        self.assertEqual(self.fixture.store.connection.execute('SELECT state FROM scientist_turn_intents').fetchone()[0], 'pending')

    def test_singleflight_and_pre_cancel_never_dispatch_or_persist(self):
        self.host._lock.acquire()
        try:
            with self.assertRaises(ScientistAdmissionError): self.discover()
            with self.assertRaises(ScientistAdmissionError): self.reconcile()
            with self.assertRaises(ScientistAdmissionError):
                self.host.resolve(self.reconcile_id, self.capability_id, response_sha256='f' * 64)
        finally:
            self.host._lock.release()
        canceled = threading.Event()
        canceled.set()
        with patch('aos.scientist_evidence_client.socket.socket') as connect:
            with self.assertRaises(ScientistAdmissionError):
                self.host.discover(self.request_id, self.capability_id, cancel_event=canceled)
            connect.assert_not_called()
        self.assertIsNone(self.host.uncertain_control_id)
        self.assertEqual(self.socket.counts(), (0, 0))

    def test_changed_capability_ack_cannot_supply_retained_authority(self):
        self.discover()
        self.fixture.store.connection.execute('DROP TRIGGER scientist_evidence_responses_no_update')
        self.fixture.store.connection.execute('UPDATE scientist_evidence_responses SET response_sha256=?', ('f' * 64,))
        self.fixture.store.connection.commit()
        with patch('aos.scientist_evidence_client.socket.socket') as connect:
            with self.assertRaises(ScientistAdmissionError): self.reconcile()
            connect.assert_not_called()
        self.assertEqual(len(self.socket.server.requests), 1)
