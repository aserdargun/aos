import hashlib
from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from aos.scientist_protocol import ScientistTurnRequest
from aos.scientist_transport import (
    BROKER_UNIT, BrokerPeer, ScientistAdmissionError, ScientistTurnClient,
    ScientistUncertainTurn, SystemdBrokerAuthenticator,
)


class SyntheticBroker:
    def __init__(self, root, mode='success', response_factory=None, frame_seconds=2):
        self.path = root / 'broker.sock'
        self.mode = mode
        self.response_factory = response_factory
        self.frame_seconds = frame_seconds
        self.requests = []
        self.stopped = threading.Event()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.path))
        self.path.chmod(0o600)
        self.listener.listen()
        self.listener.settimeout(.05)
        self.thread = threading.Thread(target=self.serve)
        self.thread.start()

    def serve(self):
        while not self.stopped.is_set():
            try:
                connection, address = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(self.frame_seconds)
                data = bytearray()
                while b'\n' not in data:
                    try:
                        chunk = connection.recv(8192)
                    except (OSError, TimeoutError):
                        return
                    if not chunk:
                        break
                    data.extend(chunk)
                if not data:
                    continue
                self.requests.append(bytes(data))
                self.entered.set()
                if self.mode == 'hold':
                    self.release.wait(2)
                if self.mode == 'disconnect':
                    continue
                request = json.loads(data)
                receipt = {key: request[key] for key in ['version', 'request_id', 'profile_id', 'deployment_digest']}
                unit = 'swapp-aos-gpu-turn-' + 'c' * 32 + '.service'
                receipt.update(generation={'unit': unit, 'invocation_id': 'd' * 32,
                                           'main_pid': 1234, 'control_group': '/synthetic/' + unit},
                               response={'synthetic_cpu_fixture': True}, usage={})
                if self.response_factory is not None:
                    receipt['response'] = self.response_factory(request)
                if self.mode == 'wrong_id':
                    receipt['request_id'] = 'e' * 32
                encoded = json.dumps(receipt).encode() + b'\n'
                if self.mode == 'multiple':
                    encoded += b'{}\n'
                if self.mode == 'oversize':
                    encoded = b'x' * (128 * 1024 + 1)
                try:
                    connection.sendall(encoded)
                    if self.mode == 'split_multiple':
                        time.sleep(.01)
                        connection.sendall(b'{}\n')
                except OSError:
                    pass

    def close(self):
        self.stopped.set()
        self.release.set()
        self.listener.close()
        self.thread.join(3)
        if self.thread.is_alive():
            raise AssertionError('Synthetic CPU broker did not close')


class ScientistTransportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.request = ScientistTurnRequest(request_id='a' * 32,
                        profile_id='aos.decider.turn.v1', deployment_digest='b' * 64,
                        payload={'text': 'Türkçe synthetic CPU fixture'})
        self.authenticator = Mock()
        self.peer = BrokerPeer(os.getpid(), os.getuid(), 1, 'synthetic', 'd' * 32, '/synthetic')
        self.authenticator.authenticate.return_value = self.peer
        self.authenticator.still_current.return_value = True
        self.admission = Mock(return_value=None)
        self.intent = Mock(return_value=None)

    def broker(self, mode='success'):
        broker = SyntheticBroker(self.root, mode)
        self.addCleanup(broker.close)
        return broker

    def client(self, broker, **keywords):
        return ScientistTurnClient(broker.path, authenticator=self.authenticator,
                                   verify_admission=self.admission, persist_intent=self.intent, **keywords)

    def test_original_outer_deadline_reaches_every_authentication_check(self):
        broker = self.broker()
        deadline = time.monotonic() + 5
        self.client(broker).infer(self.request, deadline=deadline)
        self.assertEqual(self.authenticator.authenticate.call_args.kwargs, {'deadline': deadline})
        self.assertEqual(len(self.authenticator.still_current.call_args_list), 2)
        for call in self.authenticator.still_current.call_args_list:
            self.assertEqual(call.kwargs, {'deadline': deadline})
        self.assertEqual(self.intent.call_args.args[2], deadline)

    def test_durable_preparation_does_not_consume_server_frame_window(self):
        broker = SyntheticBroker(self.root, frame_seconds=.1)
        self.addCleanup(broker.close)
        self.intent.side_effect = lambda *arguments: time.sleep(.3)
        receipt = self.client(broker).infer(self.request)
        self.assertTrue(receipt.response['synthetic_cpu_fixture'])
        self.assertEqual(len(broker.requests), 1)

    def test_fresh_dispatch_connection_rejects_changed_peer_generation(self):
        broker = self.broker()
        self.authenticator.authenticate.side_effect = [self.peer, replace(self.peer, invocation_id='e' * 32)]
        client = self.client(broker)
        with self.assertRaises(ScientistAdmissionError):
            client.infer(self.request)
        self.intent.assert_called_once()
        self.assertEqual(broker.requests, [])
        self.assertIsNone(client.uncertain_request_id)

    def test_cancellation_during_fresh_peer_authentication_prevents_frame(self):
        broker = self.broker()
        cancelled = threading.Event()
        calls = []

        def authenticate(*arguments, **keywords):
            calls.append(arguments)
            if len(calls) == 2:
                cancelled.set()
            return self.peer

        self.authenticator.authenticate.side_effect = authenticate
        client = self.client(broker)
        with self.assertRaises(ScientistAdmissionError):
            client.infer(self.request, cancel_event=cancelled)
        self.intent.assert_called_once()
        self.assertEqual(broker.requests, [])
        self.assertIsNone(client.uncertain_request_id)

    def test_authority_revocation_during_fresh_peer_authentication_prevents_frame(self):
        broker = self.broker()
        calls = []

        def authenticate(*arguments, **keywords):
            calls.append(arguments)
            if len(calls) == 2:
                self.admission.side_effect = ScientistAdmissionError('Synthetic revoked task authority')
            return self.peer

        self.authenticator.authenticate.side_effect = authenticate
        client = self.client(broker)
        with self.assertRaises(ScientistAdmissionError):
            client.infer(self.request)
        self.intent.assert_called_once()
        self.assertEqual(broker.requests, [])
        self.assertIsNone(client.uncertain_request_id)

    def test_authentication_deadline_failure_prevents_intent_and_dispatch(self):
        broker = self.broker()
        client = self.client(broker)
        self.authenticator.authenticate.side_effect = ScientistAdmissionError('expired original deadline')
        with self.assertRaises(ScientistAdmissionError):
            client.infer(self.request, deadline=time.monotonic() + 5)
        self.intent.assert_not_called()
        self.assertEqual(broker.requests, [])
        self.assertIsNone(client.uncertain_request_id)

    def test_default_admission_denies_before_any_connection(self):
        client = ScientistTurnClient(self.root / 'absent.sock')
        with patch('aos.scientist_transport.socket.socket') as factory:
            with self.assertRaises(ScientistAdmissionError):
                client.infer(self.request)
            factory.assert_not_called()

    def test_cpu_socket_roundtrip_persists_exact_intent_and_forbids_replay(self):
        broker = self.broker()
        client = self.client(broker)
        receipt = client.infer(self.request)
        self.assertTrue(receipt.response['synthetic_cpu_fixture'])
        frame, digest, deadline, peer = self.intent.call_args.args
        self.assertEqual(broker.requests, [frame + b'\n'])
        self.assertEqual(digest, hashlib.sha256(frame).hexdigest())
        self.assertIsInstance(deadline, float)
        self.assertEqual(peer, self.peer)
        self.assertEqual(self.admission.call_count, 3)
        self.assertEqual(self.authenticator.still_current.call_count, 2)
        with self.assertRaises(ScientistAdmissionError):
            client.infer(self.request)
        self.assertEqual(len(broker.requests), 1)

    def test_peer_rejection_never_records_intent_or_sends_request(self):
        broker = self.broker()
        self.authenticator.authenticate.side_effect = ScientistAdmissionError('wrong principal')
        with self.assertRaises(ScientistAdmissionError):
            self.client(broker).infer(self.request)
        self.intent.assert_not_called()
        self.assertEqual(broker.requests, [])

    def test_revoke_after_intent_prevents_dispatch(self):
        broker = self.broker()
        self.admission.side_effect = [None, ScientistAdmissionError('revoked')]
        client = self.client(broker)
        with self.assertRaises(ScientistAdmissionError):
            client.infer(self.request)
        self.intent.assert_called_once()
        self.assertIsNone(client.uncertain_request_id)
        self.assertEqual(broker.requests, [])

    def test_old_peer_generation_before_dispatch_is_denied(self):
        broker = self.broker()
        self.authenticator.still_current.return_value = False
        with self.assertRaises(ScientistAdmissionError):
            self.client(broker).infer(self.request)
        self.assertEqual(broker.requests, [])

    def test_failed_intent_persistence_prevents_dispatch(self):
        broker = self.broker()
        self.intent.side_effect = OSError('fsync failed')
        with self.assertRaises(OSError):
            self.client(broker).infer(self.request)
        self.assertEqual(broker.requests, [])

    def test_disconnect_wrong_receipt_and_oversize_block_all_new_work(self):
        for mode in ['disconnect', 'wrong_id', 'oversize', 'multiple', 'split_multiple']:
            with self.subTest(mode=mode):
                directory = self.root / mode
                directory.mkdir(mode=0o700)
                broker = SyntheticBroker(directory, mode)
                try:
                    client = self.client(broker)
                    with self.assertRaises(ScientistUncertainTurn):
                        client.infer(self.request)
                    self.assertEqual(client.uncertain_request_id, self.request.request_id)
                    with self.assertRaises(ScientistAdmissionError):
                        client.infer(self.request.model_copy(update={'request_id': 'f' * 32}))
                    self.assertEqual(len(broker.requests), 1)
                finally:
                    broker.close()

    def test_generation_change_after_reply_does_not_return_success(self):
        broker = self.broker()
        self.authenticator.still_current.side_effect = [True, False]
        client = self.client(broker)
        with self.assertRaises(ScientistUncertainTurn):
            client.infer(self.request)
        self.assertIsNotNone(client.uncertain_request_id)

    def test_revoke_after_reply_does_not_return_success(self):
        broker = self.broker()
        self.admission.side_effect = [None, None, ScientistAdmissionError('revoked during inference')]
        client = self.client(broker)
        with self.assertRaises(ScientistUncertainTurn):
            client.infer(self.request)
        self.assertIsNotNone(client.uncertain_request_id)

    def test_mutating_admission_copy_does_not_change_persisted_request(self):
        broker = self.broker()

        def admit(request):
            request.payload['text'] = 'changed'

        self.admission.side_effect = admit
        client = self.client(broker)
        client.infer(self.request)
        self.assertEqual(json.loads(broker.requests[0])['payload']['text'], self.request.payload['text'])

    def test_truthy_or_false_callback_results_cannot_authorize_dispatch(self):
        broker = self.broker()
        for value in [True, False, {}]:
            self.admission.return_value = value
            with self.assertRaises(ScientistAdmissionError):
                self.client(broker).infer(self.request)
        self.assertEqual(broker.requests, [])

    def test_foreign_authenticated_peer_cannot_override_socket_credentials(self):
        broker = self.broker()
        self.authenticator.authenticate.return_value = BrokerPeer(99999, os.getuid(), 1, '', '', '')
        with self.assertRaises(ScientistAdmissionError):
            self.client(broker).infer(self.request)
        self.assertEqual(broker.requests, [])

    def test_timeout_and_concurrent_turn_are_fail_closed(self):
        broker = self.broker('hold')
        client = self.client(broker, timeout_seconds=1)
        errors = []

        def run():
            try:
                client.infer(self.request)
            except ScientistUncertainTurn as error:
                errors.append(error)

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(broker.entered.wait(2))
        with self.assertRaises(ScientistAdmissionError):
            client.infer(self.request.model_copy(update={'request_id': 'f' * 32}))
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertEqual(client.uncertain_request_id, self.request.request_id)

    def test_public_socket_and_symlink_are_denied(self):
        broker = self.broker()
        broker.path.chmod(0o666)
        with self.assertRaises(ScientistAdmissionError):
            self.client(broker).infer(self.request)
        broker.path.chmod(0o600)
        alternate = self.root / 'alias.sock'
        alternate.symlink_to(broker.path)
        with self.assertRaises(ScientistAdmissionError):
            ScientistTurnClient(alternate, verify_admission=self.admission).infer(self.request)

    def test_missing_intent_writer_never_dispatches_even_with_test_admission(self):
        broker = self.broker()
        client = ScientistTurnClient(broker.path, authenticator=self.authenticator,
                                     verify_admission=self.admission)
        with self.assertRaises(ScientistAdmissionError):
            client.infer(self.request)
        self.assertEqual(broker.requests, [])

    def test_boolean_intent_completion_is_not_durable_attestation(self):
        broker = self.broker()
        self.intent.return_value = True
        with self.assertRaises(ScientistAdmissionError):
            self.client(broker).infer(self.request)
        self.assertEqual(broker.requests, [])

    def test_nonboolean_peer_current_result_is_not_admission(self):
        broker = self.broker()
        self.authenticator.still_current.return_value = 1
        with self.assertRaises(ScientistAdmissionError):
            self.client(broker).infer(self.request)
        self.assertEqual(broker.requests, [])

    def test_socket_replacement_during_authentication_is_denied(self):
        broker = self.broker()
        client = self.client(broker)
        with patch.object(client, '_socket_identity', side_effect=[(1, 1), (1, 2)]):
            with self.assertRaises(ScientistAdmissionError):
                client.infer(self.request)
        self.intent.assert_not_called()
        self.assertEqual(broker.requests, [])

    def test_intent_time_consumes_original_deadline_and_does_not_dispatch_after_timeout(self):
        broker = self.broker()
        client = self.client(broker, timeout_seconds=1)
        self.intent.side_effect = lambda *arguments: time.sleep(.1)
        deadline = time.monotonic() + .05
        with self.assertRaises(TimeoutError):
            client.infer(self.request, deadline=deadline)
        self.intent.assert_called_once()
        self.assertEqual(self.intent.call_args.args[2], deadline)
        self.assertEqual(broker.requests, [])
        self.assertIsNone(client.uncertain_request_id)

    def test_client_lifetime_bound_denies_instead_of_forgetting_replay_history(self):
        broker = self.broker()
        client = self.client(broker)
        client._attempted = {format(index, '032x') for index in range(256)}
        with self.assertRaises(ScientistAdmissionError):
            client.infer(self.request)
        self.assertEqual(broker.requests, [])

    def test_failed_receipt_writer_makes_successful_wire_result_uncertain(self):
        broker = self.broker()
        writer = Mock(side_effect=OSError('receipt commit failed'))
        client = self.client(broker, record_receipt=writer)
        with self.assertRaises(ScientistUncertainTurn):
            client.infer(self.request)
        self.assertEqual(client.uncertain_request_id, self.request.request_id)
        writer.assert_called_once()

    def test_invalid_timeout_and_relative_socket_are_rejected(self):
        for timeout in [True, 0, 721, 1.0]:
            with self.assertRaises(ValueError):
                ScientistTurnClient(self.root / 'broker.sock', timeout_seconds=timeout)
        with self.assertRaises(ValueError):
            ScientistTurnClient(Path('relative.sock'))


class SystemdBrokerAuthenticatorTests(unittest.TestCase):
    def setUp(self):
        self.pid = 1234
        self.runtime = '/run/user/' + str(os.getuid())
        self.group = '/user.slice/' + BROKER_UNIT
        self.identity = (55, 'synthetic-boot', self.group)
        self.properties = {'LoadState': 'loaded', 'ActiveState': 'active', 'MainPID': str(self.pid),
                           'InvocationID': 'a' * 32, 'ControlGroup': self.group}

    def authenticate(self, identities=None, **options):
        result = Mock(returncode=0, stdout='\n'.join(key + '=' + value for key, value in self.properties.items()))
        with patch.dict(os.environ, {'XDG_RUNTIME_DIR': self.runtime,
                        'DBUS_SESSION_BUS_ADDRESS': 'unix:path=' + self.runtime + '/bus'}), \
                patch('aos.scientist_transport._process_identity', side_effect=identities or [self.identity, self.identity]), \
                patch.object(Path, 'is_symlink', return_value=False), \
                patch.object(Path, 'is_dir', return_value=True), \
                patch.object(Path, 'stat', return_value=Mock(st_uid=os.getuid())), \
                patch('aos.scientist_transport.subprocess.run', return_value=result) as lookup:
            peer = SystemdBrokerAuthenticator().authenticate(self.pid, os.getuid(), **options)
            self.assertEqual(lookup.call_args.args[0][3], BROKER_UNIT)
            self.lookup_timeout = lookup.call_args.kwargs['timeout']
            return peer

    def test_outer_deadline_caps_broker_query(self):
        with patch('aos.scientist_transport.time.monotonic', return_value=10):
            self.authenticate(deadline=10.25)
        self.assertEqual(self.lookup_timeout, 0.25)

    def test_broker_deadline_expiry_prevents_query_and_late_success(self):
        with patch('aos.scientist_transport.subprocess.run') as query:
            for deadline in [True, 'later', float('inf'), float('nan'), 10, 9]:
                with self.subTest(deadline=deadline), patch('aos.scientist_transport.time.monotonic', return_value=10):
                    with self.assertRaises(ScientistAdmissionError):
                        SystemdBrokerAuthenticator().authenticate(self.pid, os.getuid(), deadline=deadline)
            query.assert_not_called()
        with patch('aos.scientist_transport.time.monotonic', side_effect=[10, 10, 10.1, 10.3]):
            with self.assertRaises(ScientistAdmissionError):
                self.authenticate(deadline=10.25)

    def test_still_current_preserves_outer_deadline(self):
        peer = BrokerPeer(self.pid, os.getuid(), 55, 'synthetic-boot', 'a' * 32, self.group)
        authenticator = SystemdBrokerAuthenticator()
        with patch.object(authenticator, 'authenticate', return_value=peer) as query:
            self.assertTrue(authenticator.still_current(peer, deadline=10.25))
        query.assert_called_once_with(peer.pid, peer.uid, deadline=10.25)

    def test_exact_systemd_peer_generation_is_pinned(self):
        self.assertEqual(self.authenticate().start_ticks, 55)

    def test_wrong_uid_pid_cgroup_invocation_and_inactive_unit_are_denied(self):
        for field, value in [('MainPID', '999'), ('ControlGroup', '/foreign'),
                             ('InvocationID', 'bad'), ('ActiveState', 'inactive')]:
            with self.subTest(field=field):
                original = self.properties[field]
                self.properties[field] = value
                with self.assertRaises(ScientistAdmissionError):
                    self.authenticate()
                self.properties[field] = original
        with self.assertRaises(ScientistAdmissionError):
            SystemdBrokerAuthenticator().authenticate(self.pid, os.getuid() + 1)

    def test_pid_reuse_or_boot_change_during_lookup_is_denied(self):
        for identity in [(56, 'synthetic-boot', self.group), (55, 'other-boot', self.group)]:
            with self.assertRaises(ScientistAdmissionError):
                self.authenticate([self.identity, identity])

    def test_lost_authentication_is_not_current(self):
        authenticator = SystemdBrokerAuthenticator()
        with patch.object(authenticator, 'authenticate', side_effect=ScientistAdmissionError('gone')):
            self.assertFalse(authenticator.still_current(BrokerPeer(1, 1, 1, '', '', '')))

    def test_service_lookup_failure_is_not_current(self):
        peer = BrokerPeer(self.pid, os.getuid(), 55, 'synthetic-boot', 'a' * 32, self.group)
        authenticator = SystemdBrokerAuthenticator()
        with patch('aos.scientist_transport._process_identity', side_effect=OSError('PID disappeared')):
            self.assertFalse(authenticator.still_current(peer))
