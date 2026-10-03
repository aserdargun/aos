"""Synthetic owned CPU packet channels and original SQLite; no Scientist or GPU."""

import array
from copy import deepcopy
from dataclasses import replace
import json
import os
import socket
import struct
import threading
import time
import unittest
from unittest.mock import Mock, patch

from aos.scientist_budget_witness import ScientistRetainedTerminalVerifier, budget_witness_schema
from aos.scientist_retained_provider import (
    FRAME_LIMIT, SCHEMA, ScientistRetainedProviderAdapter, ScientistRetainedProviderClient,
    prepare_retained_provider_channel, _deny_current,
)
from aos.scientist_terminal import canonical, digest
from aos.scientist_transport import BrokerPeer, ScientistAdmissionError

import test_scientist_retained_host as host_cases


class SyntheticPacketProvider:
    def __init__(self, witness, snapshot):
        self.client, self.server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        for channel in (self.client, self.server):
            prepare_retained_provider_channel(channel)
            channel.settimeout(1)
        self.witness, self.snapshot = witness, snapshot
        self.requests, self.senders, self.errors = [], [], []
        self.transform = lambda response: response
        self.ancillary = []
        self.closed = False
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.closed:
            try:
                raw, ancillary, flags, _address = self.server.recvmsg(FRAME_LIMIT + 1, socket.CMSG_SPACE(12))
                if not raw:
                    return
                if flags or len(ancillary) != 1 or ancillary[0][:2] != (socket.SOL_SOCKET, socket.SCM_CREDENTIALS):
                    raise AssertionError('Synthetic provider requires real kernel packet credentials')
                self.senders.append(struct.unpack('3i', ancillary[0][2]))
                request = json.loads(raw)
                self.requests.append(request)
                data = self.witness if request['op'] == 'read_budget' else self.snapshot
                response = self.transform({'schema': SCHEMA, 'version': 1, 'sequence': request['sequence'],
                    'ok': True, 'data': deepcopy(data), 'reason_code': None})
                raw = response if isinstance(response, bytes) else canonical(response).encode()
                self.server.sendmsg([raw], self.ancillary)
            except socket.timeout:
                continue
            except OSError:
                return
            except Exception as error:
                self.errors.append(error)
                return

    def close(self):
        self.closed = True
        self.client.close()
        self.server.close()
        self.thread.join(2)


class ScientistRetainedProviderTests(unittest.TestCase):
    def setUp(self):
        self.host_fixture = host_cases.ScientistRetainedHostTests()
        def actual_peer(_pid, _uid, *generation):
            return BrokerPeer(os.getpid(), os.getuid(), *generation)
        with patch('test_scientist_intents.BrokerPeer', side_effect=actual_peer):
            self.host_fixture.setUp()
        self.addCleanup(self.host_fixture.doCleanups)
        self.fixture = self.host_fixture.fixture
        self.host = self.host_fixture.host
        self.socket_fixture = self.host_fixture.socket
        self.host_fixture.discover()
        self.host_fixture.reconcile()
        self.capability_id = self.host_fixture.capability_id
        self.reconcile_id = self.host_fixture.reconcile_id
        self.capability = self.host.inspect(self.capability_id)
        self.reconcile = self.host.inspect(self.reconcile_id, capability_control_id=self.capability_id)
        self.snapshot = self.physical_snapshot()
        self.provider = SyntheticPacketProvider(self.socket_fixture.witness, self.snapshot)
        self.addCleanup(self.provider.close)
        self.client = ScientistRetainedProviderClient(self.provider.client,
            authenticator=self.socket_fixture.authenticator, expected_peer=self.socket_fixture.peer, timeout_seconds=1)
        self.current = Mock(return_value=None)
        self.adapter = self.make_adapter()

    def physical_snapshot(self):
        envelope = deepcopy(self.socket_fixture.envelope)
        terminal = json.loads(envelope['terminal_canonical'])
        drain = json.loads(envelope['drain_canonical'])
        budget, generation = terminal['original_budget'], terminal['child_generation']
        principal = terminal['original_principal']
        child = {'owner': 'aos', 'request_id': self.fixture.request.request_id, 'fencing_token': 42,
            'profile_id': terminal['profile_id'], 'deployment_digest': terminal['deployment_digest'],
            'request_sha256': digest(self.fixture.request.payload), **drain['child_intent'],
            'launch_state': 'drained', 'invocation_id': generation['invocation_id'],
            'main_pid': generation['pid'], 'main_start_ticks': generation['start_ticks'],
            'boot_id': generation['boot_id'], 'control_group': generation['control_group'],
            'observed_gpu_pids': drain['observed_gpu_pids']}
        ticket = {'owner': 'aos', 'request_id': terminal['request_id'], 'payload_sha256': terminal['request_sha256'],
            'submitted_at': 100, 'sequence': 1, 'state': 'done',
            **{key: budget[key] for key in ('activation_seconds', 'inference_seconds', 'total_seconds', 'queue_deadline')},
            **{'owner_' + key: principal[key] for key in ('pid', 'start_ticks', 'boot_id', 'unit', 'invocation_id')}}
        return {'schema': 'aos-scientist-physical-snapshot.v1', 'version': 1, 'evidence': envelope,
            'original_budget': budget, 'child': child, 'ticket': ticket, 'handoff_stage': 'go',
            'arbiter': {'active_owner': None, 'active_request_id': None, 'active_token': 42, 'phase': None}}

    def make_adapter(self, **changes):
        return ScientistRetainedProviderAdapter(self.host, self.client, **({
            'capability_control_id': self.capability_id, 'capability_response_sha256': self.capability['response_sha256'],
            'reconcile_control_id': self.reconcile_id, 'reconcile_response_sha256': self.reconcile['response_sha256'],
            'verify_current': self.current} | changes))

    def exchange(self):
        return self.client.exchange(op='read_budget', **self.adapter._bindings(self.fixture.original),
            expected_capability_sha256=digest(self.capability['response']['data']['capability']))

    def resolve(self):
        budget = self.adapter.create_budget_verifier(schema_sha256=digest(budget_witness_schema()))
        self.host.verifier = ScientistRetainedTerminalVerifier(budget, **(self.socket_fixture.proof.options | {
            'verify_resolver': self.adapter.verify_resolver, 'verify_physical': self.adapter.verify_physical}))
        return self.host.resolve(self.reconcile_id, self.capability_id, response_sha256=self.reconcile['response_sha256'])

    def assert_no_resolution(self, before):
        self.assertEqual(self.fixture.rows(), before)
        self.assertEqual(self.fixture.store.connection.execute('SELECT count(*) FROM scientist_turn_resolutions').fetchone()[0], 0)
        self.assertFalse(self.fixture.store.connection.in_transaction)

    def test_actual_kernel_credentials_and_sequence(self):
        self.assertEqual(self.exchange(), self.socket_fixture.witness)
        self.assertEqual(self.exchange(), self.socket_fixture.witness)
        self.assertEqual([request['sequence'] for request in self.provider.requests], [1, 2])
        self.assertEqual([sender[:2] for sender in self.provider.senders], [(os.getpid(), os.getuid())] * 2)
        self.assertFalse(self.client.poisoned)

    def test_actual_original_sql_resolution_composes_two_full_five_read_proofs(self):
        before = self.fixture.rows()
        def current(*_arguments):
            self.assertTrue(self.fixture.store.connection.in_transaction)
        self.current.side_effect = current
        result = self.resolve()
        self.assertEqual([request['op'] for request in self.provider.requests],
            ['read_budget', 'read_budget', 'verify_physical', 'read_budget', 'read_budget'] * 2)
        self.assertEqual(self.fixture.rows(), before)
        self.assertEqual(self.fixture.store.connection.execute('SELECT count(*) FROM scientist_turn_resolutions').fetchone()[0], 1)
        self.assertEqual(self.host.inspect_resolution(self.fixture.request.request_id, self.capability_id), result)
        self.assertEqual(len(self.provider.requests), 10)
        physical = self.provider.requests[2]
        self.assertEqual(physical['expected_evidence'], self.socket_fixture.envelope)
        self.assertEqual(physical['expected_capability_sha256'], digest(self.capability['response']['data']['capability']))

    def test_default_source_authority_and_wrong_durable_ack_pin_deny_without_packet(self):
        for adapter in (self.make_adapter(verify_current=_deny_current),
                self.make_adapter(capability_response_sha256='f' * 64)):
            self.adapter = adapter
            before = self.fixture.rows()
            with self.assertRaises(ScientistAdmissionError): self.resolve()
            self.assert_no_resolution(before)
        self.assertEqual(self.provider.requests, [])

    def test_candidate_only_callback_does_not_replace_source_readback(self):
        self.provider.transform = lambda response: response | {'ok': False, 'data': None, 'reason_code': 'provider_denied'}
        before = self.fixture.rows()
        with self.assertRaises(ScientistAdmissionError): self.resolve()
        self.assert_no_resolution(before)
        self.assertEqual([request['op'] for request in self.provider.requests], ['read_budget'])
        self.assertTrue(self.client.poisoned)

    def test_foreign_budget_after_physical_rolls_back_resolution(self):
        def mutate(response):
            if response['sequence'] == 4:
                response['data']['profile_config_sha256'] = 'f' * 64
            return response
        self.provider.transform = mutate
        before = self.fixture.rows()
        with self.assertRaises(ScientistAdmissionError): self.resolve()
        self.assert_no_resolution(before)
        self.assertEqual(len(self.provider.requests), 4)

    def test_physical_original_active_or_quarantined_denies_resolution(self):
        self.snapshot['arbiter'].update(active_owner='aos', active_request_id=self.fixture.request.request_id, phase='quarantined')
        before = self.fixture.rows()
        with self.assertRaises(ScientistAdmissionError): self.resolve()
        self.assert_no_resolution(before)
        self.assertEqual(len(self.provider.requests), 3)

    def test_physical_child_nonce_and_pid_provenance_are_exact(self):
        self.snapshot['child']['nonce'] = 'f' * 64
        before = self.fixture.rows()
        with self.assertRaises(ScientistAdmissionError): self.resolve()
        self.assert_no_resolution(before)

    def test_revoke_after_remote_physical_denies_sql_resolution(self):
        def current(operation, *_arguments):
            if operation == 'verify_physical' and len(self.provider.requests) >= 3:
                raise ScientistAdmissionError('Synthetic independent source revoked')
        self.current.side_effect = current
        before = self.fixture.rows()
        with self.assertRaises(ScientistAdmissionError): self.resolve()
        self.assert_no_resolution(before)

    def test_wrong_response_sequence_closes_without_retry(self):
        self.provider.transform = lambda response: response | {'sequence': 9}
        with self.assertRaises(ScientistAdmissionError): self.exchange()
        with self.assertRaises(ScientistAdmissionError): self.exchange()
        self.assertEqual(len(self.provider.requests), 1)
        self.assertTrue(self.client.poisoned)

    def test_noncanonical_response_closes(self):
        self.provider.transform = lambda response: canonical(response).encode() + b'\n'
        with self.assertRaises(ScientistAdmissionError): self.exchange()
        self.assertTrue(self.client.poisoned)

    def test_extra_response_fields_and_true_physical_proof_are_rejected(self):
        self.provider.transform = lambda response: response | {'data': True}
        with self.assertRaises(ScientistAdmissionError): self.exchange()
        self.assertTrue(self.client.poisoned)

    def test_received_file_descriptors_are_closed_and_channel_denied(self):
        descriptor = os.open('/dev/null', os.O_RDONLY)
        self.addCleanup(os.close, descriptor)
        self.provider.ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [descriptor]))]
        closed = []
        real_close = os.close
        def observe_close(received):
            closed.append(received)
            return real_close(received)
        with patch('aos.scientist_retained_provider.os.close', side_effect=observe_close):
            with self.assertRaises(ScientistAdmissionError): self.exchange()
        self.assertEqual(len(closed), 1)
        self.assertNotEqual(closed[0], descriptor)
        with self.assertRaises(OSError): os.fstat(closed[0])

    def test_sender_authentication_revocation_closes(self):
        self.socket_fixture.authenticator.still_current.return_value = False
        with self.assertRaises(ScientistAdmissionError): self.exchange()
        self.assertEqual(self.provider.requests, [])
        self.assertTrue(self.client.poisoned)

    def test_actual_packet_pid_cannot_be_replaced_by_authenticator_assertion(self):
        peer = replace(self.socket_fixture.peer, pid=os.getpid() + 100000)
        self.socket_fixture.authenticator.authenticate.return_value = peer
        self.client = ScientistRetainedProviderClient(self.provider.client,
            authenticator=self.socket_fixture.authenticator, expected_peer=peer, timeout_seconds=1)
        with self.assertRaises(ScientistAdmissionError): self.exchange()
        self.assertEqual(self.provider.senders[0][0], os.getpid())
        self.assertTrue(self.client.poisoned)

    def test_deadline_closes_without_resending(self):
        def delayed(response):
            time.sleep(0.1)
            return response
        self.provider.transform = delayed
        self.client = ScientistRetainedProviderClient(self.provider.client,
            authenticator=self.socket_fixture.authenticator, expected_peer=self.socket_fixture.peer, timeout_seconds=0.02)
        with self.assertRaises(ScientistAdmissionError): self.exchange()
        with self.assertRaises(ScientistAdmissionError): self.exchange()
        self.assertTrue(self.client.poisoned)
        self.assertLessEqual(len(self.provider.requests), 1)

    def test_oversized_response_and_truncation_close(self):
        self.provider.transform = lambda _response: b' ' * (FRAME_LIMIT + 2)
        with self.assertRaises(ScientistAdmissionError): self.exchange()
        self.assertTrue(self.client.poisoned)

    def test_second_full_proof_fence_regression_denies_resolution(self):
        def regress(response):
            if response['sequence'] == 3:
                response['data']['arbiter']['active_token'] = 43
            return response
        self.provider.transform = regress
        before = self.fixture.rows()
        with self.assertRaises(ScientistAdmissionError): self.resolve()
        self.assert_no_resolution(before)
        self.assertEqual(len(self.provider.requests), 8)

    def test_resolution_rights_remain_independent_of_source_authority(self):
        self.host_fixture.resolution_authority.side_effect = ScientistAdmissionError('Synthetic resolution revoked')
        before = self.fixture.rows()
        with self.assertRaises(ScientistAdmissionError): self.resolve()
        self.assert_no_resolution(before)
        self.assertEqual(self.provider.requests, [])

    def test_concurrent_exchange_is_denied_and_channel_closed(self):
        self.client._lock.acquire()
        try:
            with self.assertRaises(ScientistAdmissionError): self.exchange()
        finally:
            self.client._lock.release()
        self.assertTrue(self.client.poisoned)
        self.assertEqual(self.provider.requests, [])


if __name__ == '__main__':
    unittest.main()
