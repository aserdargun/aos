"""Synthetic CPU UDS bootstrap ACKs; no Scientist runtime or live capability."""

from contextlib import closing
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import unittest
from unittest.mock import Mock, patch

from aos.scientist_admission_history import ScientistAdmissionCaptureV2, ScientistAdmissionHistory
from aos.scientist_bootstrap import (
    BOOTSTRAP_PROPOSAL, BOOTSTRAP_SCHEMA, CONTROL_DESCRIPTOR_SHA256, HISTORY_DESCRIPTOR_SHA256,
    INFER_DESCRIPTOR_SHA256, ScientistBootstrapCapture, ScientistBootstrapCodec,
)
from aos.scientist_intents import ScientistIntentJournal
from aos.scientist_terminal import TERMINAL_DESCRIPTOR_SHA256, canonical, digest
from aos.scientist_transport import ScientistAdmissionError, ScientistUncertainTurn
from test_scientist_admission_history import BOOT_ID, admission_capture_fixture
from test_scientist_evidence_client import SyntheticEvidenceServer
import test_scientist_intents as intent_cases
from test_scientist_profile_output import OUTPUT_PIN


class ScientistBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.fixture = intent_cases.ScientistIntentTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = Path(self.fixture.temporary.name)
        self.root.chmod(0o700)
        self.store, self.request, self.binding = self.fixture.store, self.fixture.request, self.fixture.binding
        self.peer = replace(self.fixture.peer, pid=os.getpid(), uid=os.getuid(), boot_id=BOOT_ID)
        self.stable = admission_capture_fixture(self.request, self.peer)['admission_binding']
        self.stable['caller_generation']['pid'] = os.getpid()
        self.stable['profile_pin']['output_contract'] = deepcopy(OUTPUT_PIN)
        self.stable['infer_schema']['sha256'] = INFER_DESCRIPTOR_SHA256
        self.stable['control_schema']['sha256'] = CONTROL_DESCRIPTOR_SHA256
        self.stable['terminal_schema']['sha256'] = TERMINAL_DESCRIPTOR_SHA256
        self.codec = ScientistBootstrapCodec(control_descriptor_sha256=CONTROL_DESCRIPTOR_SHA256)
        self.authenticator = Mock()
        self.authenticator.authenticate.return_value = self.peer
        self.authenticator.still_current.return_value = True
        self.now = 100
        self.current = Mock(side_effect=self.verify_current)
        self.expected = Mock(side_effect=self.verify_capture)
        self.persist = Mock(side_effect=self.persist_intent)
        self.intent_path = self.root / 'synthetic-bootstrap-intents.sqlite3'
        with closing(sqlite3.connect(self.intent_path)) as connection, connection:
            connection.execute('CREATE TABLE synthetic_bootstrap_intents(frame BLOB, sha TEXT)')
        self.response_transform = None
        self.enterContext(patch('test_scientist_evidence_client.evidence_response_fixture', side_effect=self.response))

    def control(self):
        return {'schema': BOOTSTRAP_SCHEMA, 'version': 1, 'op': 'capability', 'control_id': 'a' * 32,
                'profile_id': self.request.profile_id, 'deployment_digest': self.request.deployment_digest,
                'target': None, 'expected_capability_sha256': None}

    def response(self, request, ignored=None):
        binding = deepcopy(self.stable)
        capability = {'admission_binding': binding, 'admission_binding_sha256': digest(binding),
            'server_generation_sha256': digest(binding['server_generation']),
            'caller_generation': deepcopy(binding['caller_generation']),
            'caller_generation_sha256': digest(binding['caller_generation']),
            'policy_sha256': binding['policy_sha256'], 'source_fingerprints': deepcopy(binding['source_fingerprints']),
            'profile_id': binding['profile_id'], **deepcopy(binding['profile_pin']),
            'infer_schema': deepcopy(binding['infer_schema']), 'control_schema': deepcopy(binding['control_schema']),
            'history_schema_sha256': HISTORY_DESCRIPTOR_SHA256,
            'operations': ['cancel', 'capability', 'reconcile', 'status'], 'request_bytes': 8192,
            'response_bytes': 131072, 'frame_seconds': 5.0, 'call_seconds': 10.0,
            'boot_id': BOOT_ID, 'issued_boottime': 90.0, 'expires_boottime': 150.0}
        response = {'schema': BOOTSTRAP_SCHEMA, 'version': 1, 'control_id': request['control_id'],
                    'op': 'capability', 'ok': True, 'capability_sha256': digest(capability),
                    'data': {'capability': capability, 'admission': 'enabled', 'reason_code': 'enabled'}, 'error': None}
        if self.response_transform is not None:
            self.response_transform(response)
        return response

    def verify_current(self, request, binding, peer):
        if (request != self.request or binding != self.binding or peer != self.peer):
            raise ScientistAdmissionError('Synthetic original caller authority differs')

    def verify_capture(self, request, binding, peer, capture):
        self.verify_current(request, binding, peer)
        if capture.admission_binding.model_dump(mode='json') != self.stable:
            raise ScientistAdmissionError('Synthetic independently pinned source or generation differs')

    def persist_intent(self, frame, fingerprint, deadline, peer):
        self.assertFalse(self.store.connection.in_transaction)
        self.assertEqual(fingerprint, hashlib.sha256(frame).hexdigest())
        self.assertEqual(peer, self.peer)
        self.assertGreater(deadline, time.monotonic())
        with closing(sqlite3.connect(self.intent_path)) as connection, connection:
            connection.execute('INSERT INTO synthetic_bootstrap_intents VALUES (?,?)', (frame, fingerprint))

    def server(self, **options):
        def inspect(frame):
            with closing(sqlite3.connect(self.intent_path)) as connection:
                recorded = connection.execute('SELECT frame FROM synthetic_bootstrap_intents ORDER BY rowid DESC').fetchone()
            self.assertEqual(recorded[0], frame[:-1])
            self.assertFalse(self.store.connection.in_transaction)
        server = SyntheticEvidenceServer(self.root, inspect=inspect, **options)
        self.addCleanup(server.close)
        return server

    def capture(self, server, **changes):
        options = {'codec': self.codec, 'verify_current': self.current, 'verify_capture': self.expected,
                   'persist_intent': self.persist, 'authenticator': self.authenticator, 'clock': lambda: self.now}
        return ScientistBootstrapCapture(self.store, server.path, **(options | changes))

    def prepare(self, capture):
        return capture.prepare(self.request, self.binding, self.peer)

    def consume(self, capture, **changes):
        self.store.connection.execute('BEGIN IMMEDIATE')
        try:
            return capture(changes.get('request', self.request), changes.get('binding', self.binding),
                           changes.get('peer', self.peer))
        finally:
            self.store.connection.rollback()

    def test_descriptor_is_explicit_candidate_not_full_wire_schema(self):
        self.assertFalse(BOOTSTRAP_PROPOSAL['full_wire_schema_reviewed'])
        self.assertEqual(BOOTSTRAP_PROPOSAL['scientist_commit'], '737b80b67167287f84476f6999fbf1e854a160ca')
        for pin in ('f' * 64, None, True):
            with self.subTest(pin=pin), self.assertRaises(ScientistAdmissionError):
                ScientistBootstrapCodec(control_descriptor_sha256=pin)

    def test_closed_bootstrap_only_request_rejects_versions_retained_targets_extra_and_noncanonical(self):
        request = self.control()
        self.assertEqual(self.codec.decode_request(self.codec.encode_request(request)), request)
        for changes in ({'version': 2}, {'version': True}, {'version': 1.0}, {'op': 'reconcile'},
                        {'target': {}}, {'expected_capability_sha256': 'f' * 64}, {'extra': True},
                        {'schema': 'aos-scientist-control-evidence.v2'}):
            with self.subTest(changes=changes), self.assertRaises(ScientistAdmissionError):
                self.codec.encode_request(request | changes)
        for raw in (json.dumps(request).encode(), b'{"op":"capability","op":"capability"}', b'x' * 8192):
            with self.subTest(raw=raw[:40]), self.assertRaises(ScientistAdmissionError):
                self.codec.decode_request(raw)

    def test_full_closed_ack_hashes_bindings_descriptors_and_limits_reject(self):
        request = self.codec.encode_request(self.control())
        for change in ('hash', 'binding_hash', 'caller_hash', 'server_hash', 'output', 'source',
                       'policy', 'profile', 'descriptor', 'infer', 'terminal', 'operations',
                       'limits', 'freshness', 'boot', 'extra', 'version', 'boolean_version', 'denied'):
            response = self.response(self.control())
            capability = response['data']['capability']
            if change == 'hash': response['capability_sha256'] = 'f' * 64
            elif change == 'binding_hash': capability['admission_binding_sha256'] = 'f' * 64
            elif change == 'caller_hash': capability['caller_generation_sha256'] = 'f' * 64
            elif change == 'server_hash': capability['server_generation_sha256'] = 'f' * 64
            elif change == 'output': capability['output_contract']['bundle_sha256'] = 'f' * 64
            elif change == 'source': capability['source_fingerprints']['aos'] = 'f' * 64
            elif change == 'policy': capability['policy_sha256'] = 'f' * 64
            elif change == 'profile': capability['deployment_digest'] = 'f' * 64
            elif change == 'descriptor': capability['control_schema']['sha256'] = 'f' * 64
            elif change == 'infer': capability['infer_schema']['sha256'] = 'f' * 64
            elif change == 'terminal': capability['admission_binding']['terminal_schema']['sha256'] = 'f' * 64
            elif change == 'operations': capability['operations'] = ['cancel'] * 4
            elif change == 'limits': capability['request_bytes'] = 8192.0
            elif change == 'freshness': capability['expires_boottime'] += 1
            elif change == 'boot': capability['boot_id'] = '00000000-0000-0000-0000-000000000002'
            elif change == 'extra': capability['extra'] = True
            elif change == 'version': response['version'] = 2
            elif change == 'boolean_version': response['version'] = True
            else: response['data'].update(admission='denied', reason_code='policy_disabled')
            if change != 'hash': response['capability_sha256'] = digest(capability)
            with self.subTest(change=change), self.assertRaises(ScientistAdmissionError):
                self.codec.decode_response(canonical(response).encode(), request)

    def test_response_noncanonical_duplicate_unknown_and_boolean_success_deny(self):
        request = self.codec.encode_request(self.control())
        response = self.response(self.control())
        for raw in (json.dumps(response).encode(), canonical(response | {'extra': None}).encode(),
                    canonical(response | {'ok': 1}).encode(), b'{"ok":true,"ok":true}',
                    b'x' * 131072):
            with self.subTest(raw=raw[:40]), self.assertRaises(ScientistAdmissionError):
                self.codec.decode_response(raw, request)

    def test_actual_uds_prepare_durable_before_send_then_one_shot_network_free_capture(self):
        server = self.server()
        capture = self.capture(server)
        self.assertIsNone(self.prepare(capture))
        self.assertEqual(len(server.requests), 1)
        self.persist.assert_called_once()
        with patch('aos.scientist_evidence_client.socket.socket') as network:
            result = self.consume(capture)
            network.assert_not_called()
        self.assertIsInstance(result, ScientistAdmissionCaptureV2)
        self.assertEqual(result.admission_binding.model_dump(mode='json'), self.stable)
        self.assertEqual(result.capability_sha256, self.response(json.loads(server.requests[0]))['capability_sha256'])
        with self.assertRaises(ScientistAdmissionError): self.consume(capture)
        with self.assertRaises(ScientistAdmissionError): self.prepare(capture)
        self.assertEqual(len(server.requests), 1)

    def test_prefetch_composes_with_actual_original_history_transaction_without_network_or_nested_commit(self):
        server = self.server()
        capture = self.capture(server)
        self.prepare(capture)
        history = ScientistAdmissionHistory(self.store, capture=capture, record_version='2.0',
                                            verify_current=lambda *arguments: None, clock=lambda: self.now)
        journal = ScientistIntentJournal(self.store, self.binding, admission_history=history)
        with patch('aos.scientist_evidence_client.socket.socket') as network:
            journal.persist_intent(self.fixture.frame, self.fixture.digest, time.monotonic() + 60, self.peer)
            network.assert_not_called()
        record, checksum = history.read(self.request.request_id)
        self.assertEqual(record.schema_version, '2.0')
        self.assertEqual(record.admission_binding.model_dump(mode='json'), self.stable)
        self.assertEqual(checksum, digest(record.model_dump(mode='json')))
        self.assertEqual(len(server.requests), 1)

    def test_default_authority_or_durable_writer_missing_denies_before_socket(self):
        from aos.scientist_bootstrap import _deny_bootstrap
        from aos.scientist_evidence_client import _deny_intent
        server = self.server()
        for options in ({'verify_current': _deny_bootstrap}, {'verify_capture': _deny_bootstrap},
                        {'persist_intent': _deny_intent}):
            capture = self.capture(server, **options)
            with patch('aos.scientist_evidence_client.socket.socket') as network:
                with self.assertRaises(ScientistAdmissionError): self.prepare(capture)
                network.assert_not_called()
        self.assertEqual(server.requests, [])

    def test_durable_writer_failure_or_non_none_result_never_sends_bootstrap_request(self):
        server = self.server()
        for failure in (ScientistAdmissionError('Synthetic write unavailable'), True):
            writer = Mock(side_effect=failure) if isinstance(failure, Exception) else Mock(return_value=failure)
            capture = self.capture(server, persist_intent=writer)
            with self.subTest(failure=failure), self.assertRaises(ScientistAdmissionError):
                self.prepare(capture)
        self.assertEqual(server.requests, [])

    def test_current_callback_cannot_open_original_transaction_before_network(self):
        server = self.server()
        def begin(*arguments):
            self.store.connection.execute('BEGIN IMMEDIATE')
        capture = self.capture(server, verify_current=begin)
        try:
            with patch('aos.scientist_evidence_client.socket.socket') as network:
                with self.assertRaises(ScientistAdmissionError): self.prepare(capture)
                network.assert_not_called()
        finally:
            self.store.connection.rollback()
        self.assertEqual(server.requests, [])

    def test_durable_writer_cannot_start_original_transaction_before_dispatch(self):
        server = self.server()
        def begin(*arguments):
            self.persist_intent(*arguments)
            self.store.connection.execute('BEGIN IMMEDIATE')
        capture = self.capture(server, persist_intent=begin)
        try:
            with self.assertRaises(ScientistAdmissionError): self.prepare(capture)
        finally:
            self.store.connection.rollback()
        self.assertEqual(server.requests, [])

    def test_expected_capture_callback_must_preserve_transaction_boundary_in_both_phases(self):
        server = self.server()
        def begin(*arguments):
            self.store.connection.execute('BEGIN IMMEDIATE')
        capture = self.capture(server, verify_capture=begin)
        try:
            with self.assertRaises(ScientistAdmissionError): self.prepare(capture)
        finally:
            self.store.connection.rollback()
        with self.assertRaises(ScientistAdmissionError): self.consume(capture)
        capture = self.capture(server)
        self.prepare(capture)
        self.expected.side_effect = lambda *arguments: self.store.connection.rollback()
        with self.assertRaises(ScientistAdmissionError): self.consume(capture)

    def test_current_authority_revocation_after_send_cannot_leave_a_consumable_ack(self):
        server = self.server()
        original_inspect = server.inspect
        def revoke(frame):
            original_inspect(frame)
            self.current.side_effect = ScientistAdmissionError('Synthetic current authority revoked')
        server.inspect = revoke
        capture = self.capture(server)
        with self.assertRaises(ScientistUncertainTurn): self.prepare(capture)
        with self.assertRaises(ScientistAdmissionError): self.consume(capture)
        self.assertEqual(len(server.requests), 1)

    def test_expiry_inside_actual_history_capture_rolls_back_original_intent_and_history(self):
        server = self.server()
        capture = self.capture(server)
        self.prepare(capture)
        self.now = 150
        history = ScientistAdmissionHistory(self.store, capture=capture, record_version='2.0',
                                            verify_current=lambda *arguments: None, clock=lambda: self.now)
        journal = ScientistIntentJournal(self.store, self.binding, admission_history=history)
        with self.assertRaises(ScientistAdmissionError):
            journal.persist_intent(self.fixture.frame, self.fixture.digest, time.monotonic() + 60, self.peer)
        for table in ('scientist_turn_intents', 'scientist_admission_history'):
            self.assertEqual(self.store.connection.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0)
        self.assertFalse(self.store.connection.in_transaction)
        self.assertEqual(len(server.requests), 1)

    def test_prepare_inside_transaction_and_capture_without_transaction_or_prefetch_deny(self):
        server = self.server()
        capture = self.capture(server)
        with self.assertRaises(ScientistAdmissionError): self.consume(capture)
        self.store.connection.execute('BEGIN IMMEDIATE')
        try:
            with self.assertRaises(ScientistAdmissionError): self.prepare(capture)
        finally:
            self.store.connection.rollback()
        self.assertEqual(server.requests, [])
        self.prepare(capture)
        with self.assertRaises(ScientistAdmissionError): capture(self.request, self.binding, self.peer)

    def test_freshness_and_current_authority_rechecked_when_consuming(self):
        for change in ('expired', 'revoked'):
            with self.subTest(change=change):
                server = self.server()
                capture = self.capture(server)
                self.prepare(capture)
                if change == 'expired': self.now = 150
                else: self.current.side_effect = ScientistAdmissionError('Synthetic authority revoked')
                with self.assertRaises(ScientistAdmissionError): self.consume(capture)
                self.now = 100
                self.current.side_effect = self.verify_current
                server.close()
                server.path.unlink()

    def test_same_request_different_intent_or_peer_cannot_consume_ack(self):
        server = self.server()
        capture = self.capture(server)
        self.prepare(capture)
        with self.assertRaises(ScientistAdmissionError):
            self.consume(capture, binding=self.binding.model_copy(update={'generation': 1}))
        with self.assertRaises(ScientistAdmissionError): self.consume(capture)
        self.assertEqual(len(server.requests), 1)

    def test_ack_independent_source_pins_and_actual_caller_cannot_be_replaced_by_rehashing(self):
        server = self.server()
        for field in ('source', 'caller', 'server'):
            def changed(response):
                capability = response['data']['capability']
                binding = capability['admission_binding']
                if field == 'source':
                    binding['source_fingerprints']['aos'] = 'f' * 64
                    capability['source_fingerprints'] = deepcopy(binding['source_fingerprints'])
                elif field == 'caller':
                    binding['caller_generation']['pid'] += 1
                    capability['caller_generation'] = deepcopy(binding['caller_generation'])
                    capability['caller_generation_sha256'] = digest(binding['caller_generation'])
                else:
                    binding['server_generation']['invocation_id'] = 'f' * 32
                    capability['server_generation_sha256'] = digest(binding['server_generation'])
                capability['admission_binding_sha256'] = digest(binding)
                response['capability_sha256'] = digest(capability)
            self.response_transform = changed
            with self.subTest(field=field), self.assertRaises(ScientistAdmissionError):
                self.prepare(self.capture(server))

    def test_transport_denial_timeout_malformed_response_and_peer_revocation_do_not_capture(self):
        for mode in ('negative', 'foreign', 'missing_lf', 'multiple', 'oversize', 'hold'):
            server = self.server(mode=mode)
            capture = self.capture(server, timeout_seconds=.05 if mode == 'hold' else 1)
            with self.subTest(mode=mode), self.assertRaises(ScientistUncertainTurn): self.prepare(capture)
            with self.assertRaises(ScientistAdmissionError): self.consume(capture)
            with self.assertRaises(ScientistAdmissionError): self.prepare(capture)
            server.close()
            server.path.unlink()
        server = self.server()
        self.authenticator.still_current.return_value = False
        with self.assertRaises(ScientistAdmissionError): self.prepare(self.capture(server))
        self.assertEqual(server.requests, [])


if __name__ == '__main__':
    unittest.main()
