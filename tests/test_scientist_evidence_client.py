"""Real private CPU Unix sockets with synthetic peers; no Scientist process or GPU."""

from copy import deepcopy
from contextlib import closing
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from aos.scientist_evidence_client import ScientistEvidenceClient, _failure_locations
from aos.scientist_terminal import canonical
from aos.scientist_transport import BrokerPeer, ScientistAdmissionError, ScientistUncertainTurn
from test_scientist_evidence_transport import (
    evidence_codec_fixture, evidence_request_fixture, evidence_response_fixture,
)


class ScientistFailureLocationsTests(unittest.TestCase):
    def test_deep_trace_preserves_boundary_and_inner_failure_without_private_values(self):
        def fail_inner():
            raise ScientistAdmissionError('SYNTHETIC_PRIVATE_ERROR_VALUE')

        def nested_call(depth):
            if depth:
                nested_call(depth - 1)
            else:
                fail_inner()

        try:
            nested_call(16)
        except ScientistAdmissionError as error:
            locations = _failure_locations(error)

        self.assertEqual(len(locations), 1)
        self.assertEqual(locations[0]['type'], 'ScientistAdmissionError')
        frames = locations[0]['frames']
        self.assertEqual(len(frames), 8)
        self.assertEqual(frames[0]['function'], self._testMethodName)
        self.assertEqual(frames[-1]['function'], 'fail_inner')
        self.assertTrue(all(set(frame) == {'function', 'line'} for frame in frames))
        self.assertNotIn('SYNTHETIC_PRIVATE_ERROR_VALUE', json.dumps(locations))
        self.assertNotIn(__file__, json.dumps(locations))

    def test_short_trace_and_exception_chain_remain_bounded(self):
        def fail_inner():
            raise ValueError('SYNTHETIC_PRIVATE_CAUSE')

        try:
            try:
                fail_inner()
            except ValueError as cause:
                raise ScientistAdmissionError('SYNTHETIC_PRIVATE_WRAPPER') from cause
        except ScientistAdmissionError as error:
            locations = _failure_locations(error)

        self.assertEqual([item['type'] for item in locations], ['ScientistAdmissionError', 'ValueError'])
        self.assertEqual(len(locations[0]['frames']), 1)
        self.assertEqual(len(locations[1]['frames']), 2)
        self.assertEqual(locations[1]['frames'][-1]['function'], 'fail_inner')
        self.assertNotIn('SYNTHETIC_PRIVATE', json.dumps(locations))


class SyntheticEvidenceServer:
    def __init__(self, directory, *, mode='success', inspect=None, frame_seconds=1):
        self.path = directory / 'evidence.sock'
        self.mode, self.inspect = mode, inspect
        self.frame_seconds = frame_seconds
        self.requests, self.errors = [], []
        self.entered, self.release, self.stopped = threading.Event(), threading.Event(), threading.Event()
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
                connection, _address = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                with connection:
                    connection.settimeout(self.frame_seconds)
                    raw = bytearray()
                    while b'\n' not in raw:
                        chunk = connection.recv(8192)
                        if not chunk:
                            break
                        raw.extend(chunk)
                    if not raw or b'\n' not in raw:
                        continue
                    self.requests.append(bytes(raw))
                    self.entered.set()
                    if self.inspect is not None:
                        self.inspect(bytes(raw))
                    if self.mode == 'hold':
                        self.release.wait(2)
                    if self.mode == 'disconnect':
                        continue
                    request = json.loads(raw)
                    response = evidence_response_fixture(request, evidence_request_fixture()[1])
                    if self.mode == 'negative':
                        response.update(ok=False, capability_sha256=None, data=None,
                                        error={'code': 'busy', 'retryable': True})
                    if self.mode == 'foreign':
                        response['control_id'] = 'f' * 32
                    encoded = canonical(response).encode() + b'\n'
                    if self.mode == 'missing_lf':
                        encoded = encoded[:-1]
                    if self.mode == 'multiple':
                        encoded += b'{}\n'
                    if self.mode == 'oversize':
                        encoded = b'x' * (128 * 1024 + 1)
                    connection.sendall(encoded)
                    if self.mode == 'split_multiple':
                        time.sleep(.01)
                        connection.sendall(b'{}\n')
                    if self.mode == 'no_eof':
                        self.release.wait(2)
            except OSError:
                pass
            except BaseException as error:
                self.errors.append(error)

    def close(self):
        self.stopped.set()
        self.release.set()
        self.listener.close()
        self.thread.join(3)
        if self.thread.is_alive() or self.errors:
            raise AssertionError(f'Synthetic evidence server cleanup failed: {self.errors}')


class ScientistEvidenceClientTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.codec = evidence_codec_fixture()
        self.request, self.binding = evidence_request_fixture()
        self.peer = BrokerPeer(os.getpid(), os.getuid(), 1, 'synthetic-cpu-peer', 'd' * 32, '/synthetic')
        self.authenticator = Mock()
        self.authenticator.authenticate.return_value = self.peer
        self.authenticator.still_current.return_value = True
        self.authorize = Mock(return_value=None)
        self.persist = Mock(return_value=None)

    def server(self, **options):
        server = SyntheticEvidenceServer(self.root, **options)
        self.addCleanup(server.close)
        return server

    def client(self, server, **options):
        arguments = {'codec': self.codec, 'authenticator': self.authenticator,
                     'authorize': self.authorize, 'persist_intent': self.persist}
        return ScientistEvidenceClient(server.path, **(arguments | options))

    def test_defaults_deny_before_any_socket_and_invalid_configuration_is_rejected(self):
        for configured in ({}, {'authorize': self.authorize}, {'persist_intent': self.persist}):
            client = ScientistEvidenceClient(self.root / 'absent.sock', codec=self.codec, **configured)
            with patch('aos.scientist_evidence_client.socket.socket') as factory:
                with self.assertRaises(ScientistAdmissionError):
                    client.exchange(self.request)
                factory.assert_not_called()
        for timeout in (True, 0, -1, 10.1, float('inf'), float('nan')):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                ScientistEvidenceClient(self.root / 'absent.sock', codec=self.codec, timeout_seconds=timeout)
        with self.assertRaises(ValueError):
            ScientistEvidenceClient(Path('relative.sock'), codec=self.codec)

    def test_actual_socket_roundtrip_persists_exact_bytes_before_send_and_forbids_replay(self):
        path = self.root / 'synthetic-control.sqlite3'
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute('CREATE TABLE synthetic_intents(frame BLOB, sha TEXT, deadline REAL)')

        def persist(frame, fingerprint, deadline, peer):
            self.assertEqual(peer, self.peer)
            self.assertEqual(fingerprint, hashlib.sha256(frame).hexdigest())
            self.assertLess(time.monotonic(), deadline)
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute('INSERT INTO synthetic_intents VALUES (?,?,?)', (frame, fingerprint, deadline))

        def inspect(raw):
            with closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True)) as connection:
                rows = connection.execute('SELECT frame,sha FROM synthetic_intents').fetchall()
            self.assertEqual(rows, [(raw[:-1], hashlib.sha256(raw[:-1]).hexdigest())])

        server = self.server(inspect=inspect)
        client = self.client(server, persist_intent=persist)
        response = client.exchange(self.request)
        self.assertEqual(response, evidence_response_fixture(self.request, self.binding))
        self.assertEqual(server.requests, [self.codec.encode_request(self.request) + b'\n'])
        self.assertEqual(self.authorize.call_count, 5)
        self.assertIsNone(client.uncertain_control_id)
        with self.assertRaises(ScientistAdmissionError):
            client.exchange(self.request)

    def test_reconcile_is_authorized_control_not_automatic_proof_or_resolution(self):
        request, binding = evidence_request_fixture('reconcile')
        server = self.server()
        response = self.client(server).exchange(request)
        self.assertEqual(response, evidence_response_fixture(request, binding))
        self.persist.assert_called_once()
        self.assertIn('synthetic_transport_only', response['data']['terminal_canonical'])

    def test_slow_durable_preparation_does_not_consume_server_frame_window(self):
        persisted = []

        def persist(frame, fingerprint, deadline, peer):
            self.assertEqual(peer, self.peer)
            self.assertEqual(hashlib.sha256(frame).hexdigest(), fingerprint)
            self.assertGreater(deadline, time.monotonic())
            time.sleep(.3)
            persisted.append(frame)

        def inspect(frame):
            self.assertEqual(persisted, [frame[:-1]])

        server = self.server(frame_seconds=.1, inspect=inspect)
        response = self.client(server, persist_intent=persist).exchange(self.request)
        self.assertEqual(response, evidence_response_fixture(self.request, self.binding))
        self.assertEqual(len(server.requests), 1)

    def test_new_dispatch_connection_cannot_adopt_changed_peer_generation(self):
        server = self.server()
        self.authenticator.authenticate.side_effect = [
            self.peer, replace(self.peer, invocation_id='e' * 32)]
        client = self.client(server)
        with self.assertRaises(ScientistAdmissionError):
            client.exchange(self.request)
        self.persist.assert_called_once()
        self.assertEqual(server.requests, [])
        self.assertIsNone(client.uncertain_control_id)
        with self.assertRaises(ScientistAdmissionError):
            client.exchange(self.request)
        self.assertEqual(self.authenticator.authenticate.call_count, 2)

    def test_revocation_during_new_connection_authentication_prevents_send(self):
        server = self.server()
        observations = []

        def authenticate(*_arguments):
            observations.append(True)
            return self.peer

        def authorize(*_arguments):
            if len(observations) == 2:
                raise ScientistAdmissionError('Synthetic target revoked before dispatch')

        self.authenticator.authenticate.side_effect = authenticate
        client = self.client(server, authorize=authorize)
        with self.assertRaises(ScientistAdmissionError):
            client.exchange(self.request)
        self.persist.assert_called_once()
        self.assertEqual(server.requests, [])
        self.assertIsNone(client.uncertain_control_id)

    def test_failure_diagnostic_excludes_messages_paths_and_request_contents(self):
        server = self.server()

        def persist(*_arguments):
            exec(compile("raise ValueError('synthetic-private-error-secret')",
                         '/synthetic-private-path-secret.py', 'exec'), {})

        with self.assertLogs('aos.scientist_evidence_client', level='WARNING') as captured:
            with self.assertRaises(ValueError):
                self.client(server, persist_intent=persist).exchange(self.request)
        self.assertEqual(len(captured.records), 1)
        message = captured.records[0].getMessage()
        value = json.loads(message.split(': ', 1)[1])
        self.assertEqual(value['stage'], 'intent_persistence')
        self.assertFalse(value['attempted'])
        self.assertEqual(value['locations'][0]['type'], 'ValueError')
        self.assertLess(len(message), 10000)
        self.assertNotIn('synthetic-private-error-secret', message)
        self.assertNotIn('synthetic-private-path-secret', message)
        self.assertNotIn(self.request['control_id'], message)
        self.assertEqual(server.requests, [])

    def test_valid_negative_response_consumes_id_without_retry(self):
        server = self.server(mode='negative')
        client = self.client(server)
        response = client.exchange(self.request)
        self.assertEqual(response['error'], {'code': 'busy', 'retryable': True})
        self.assertIsNone(client.uncertain_control_id)
        with self.assertRaises(ScientistAdmissionError):
            client.exchange(self.request)
        self.assertEqual(len(server.requests), 1)

    def test_default_real_credentials_binding_rejects_authenticator_forgery(self):
        server = self.server()
        self.authenticator.authenticate.return_value = replace(self.peer, pid=self.peer.pid + 1)
        with self.assertRaises(ScientistAdmissionError):
            self.client(server).exchange(self.request)
        self.persist.assert_not_called()
        self.authorize.assert_not_called()
        self.assertEqual(server.requests, [])

    def test_public_socket_or_parent_and_symlink_are_denied_before_connect(self):
        server = self.server()
        for target in (server.path, self.root):
            original = target.stat().st_mode & 0o777
            target.chmod(0o777)
            try:
                with self.assertRaises(ScientistAdmissionError):
                    self.client(server).exchange(self.request)
            finally:
                target.chmod(original)
        link = self.root / 'link.sock'
        link.symlink_to(server.path)
        client = self.client(server)
        client.socket_path = link
        with self.assertRaises(ScientistAdmissionError):
            client.exchange(self.request)
        self.assertEqual(server.requests, [])

    def test_authorization_and_persistence_failures_never_dispatch(self):
        server = self.server()
        self.authorize.side_effect = ScientistAdmissionError('synthetic unauthorized')
        with self.assertRaises(ScientistAdmissionError):
            self.client(server).exchange(self.request)
        self.persist.assert_not_called()
        self.authorize.side_effect = None
        self.persist.side_effect = RuntimeError('synthetic durable write denied')
        client = self.client(server)
        with self.assertRaises(RuntimeError):
            client.exchange(self.request)
        self.assertIsNone(client.uncertain_control_id)
        with self.assertRaises(ScientistAdmissionError):
            client.exchange(self.request)
        self.assertEqual(server.requests, [])

    def test_post_persistence_authority_revoke_and_late_callback_prevent_send(self):
        server = self.server()
        self.authorize.side_effect = [None, ScientistAdmissionError('revoked')]
        client = self.client(server)
        with self.assertRaises(ScientistAdmissionError):
            client.exchange(self.request)
        self.assertIsNone(client.uncertain_control_id)
        self.authorize.side_effect = None
        client = self.client(server, timeout_seconds=.03,
                             persist_intent=lambda *_args: time.sleep(.04))
        with self.assertRaises(TimeoutError):
            client.exchange(self.request)
        self.assertEqual(server.requests, [])

    def test_callback_mutations_do_not_change_request_peer_or_wire(self):
        server = self.server()
        original = deepcopy(self.request)

        def authorize(request, peer):
            request['target']['request_id'] = 'f' * 32
            object.__setattr__(peer, 'pid', 999)

        def persist(raw, fingerprint, deadline, peer):
            object.__setattr__(peer, 'pid', 888)

        response = self.client(server, authorize=authorize, persist_intent=persist).exchange(self.request)
        self.assertEqual(self.request, original)
        self.assertEqual(self.peer.pid, os.getpid())
        self.assertEqual(response['control_id'], original['control_id'])
        self.assertEqual(server.requests, [self.codec.encode_request(original) + b'\n'])

    def test_socket_replacement_after_response_causes_sticky_uncertainty(self):
        server = self.server()
        replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(replacement.close)
        calls = 0

        def authorize(_request, _peer):
            nonlocal calls
            calls += 1
            if calls == 5:
                server.path.rename(self.root / 'original.sock')
                replacement.bind(str(server.path))
                server.path.chmod(0o600)

        client = self.client(server, authorize=authorize)
        with self.assertRaises(ScientistUncertainTurn):
            client.exchange(self.request)
        self.assertEqual(client.uncertain_control_id, self.request['control_id'])

    def test_post_response_authority_and_peer_changes_latch_uncertainty(self):
        server = self.server()
        for change in ('authority', 'peer'):
            self.authorize.side_effect = [None, None, None, ScientistAdmissionError('revoked')] if change == 'authority' else None
            self.authenticator.still_current.side_effect = [True, True, False] if change == 'peer' else None
            client = self.client(server)
            with self.subTest(change=change), self.assertRaises(ScientistUncertainTurn):
                client.exchange(self.request)
            self.assertEqual(client.uncertain_control_id, self.request['control_id'])

    def test_disconnect_malformed_correlation_frames_and_missing_eof_are_uncertain(self):
        for mode in ('disconnect', 'foreign', 'multiple', 'split_multiple', 'missing_lf', 'oversize', 'no_eof'):
            directory = self.root / mode
            directory.mkdir(mode=0o700)
            server = SyntheticEvidenceServer(directory, mode=mode)
            self.addCleanup(server.close)
            client = self.client(server, timeout_seconds=.2)
            with self.subTest(mode=mode), self.assertRaises(ScientistUncertainTurn):
                client.exchange(self.request)
            self.assertEqual(client.uncertain_control_id, self.request['control_id'])
            with self.assertRaises(ScientistAdmissionError):
                client.exchange({**self.request, 'control_id': 'e' * 32})
            self.assertEqual(len(server.requests), 1)

    def test_singleflight_and_local_cancel_do_not_cancel_remote_or_allow_replay(self):
        server = self.server(mode='hold')
        client = self.client(server)
        cancel = threading.Event()
        errors = []

        def exchange():
            try:
                client.exchange(self.request, cancel_event=cancel)
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=exchange)
        thread.start()
        try:
            self.assertTrue(server.entered.wait(1))
            with self.assertRaises(ScientistAdmissionError):
                client.exchange({**self.request, 'control_id': 'e' * 32})
            cancel.set()
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], ScientistUncertainTurn)
            self.assertEqual(client.uncertain_control_id, self.request['control_id'])
            self.assertEqual(len(server.requests), 1)
        finally:
            cancel.set()
            server.release.set()
            thread.join(2)

    def test_presend_cancellation_and_client_id_bound_prevent_connection(self):
        server = self.server()
        client = self.client(server)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(ScientistAdmissionError):
            client.exchange(self.request, cancel_event=cancel)
        client._attempted.update(f'{number:032x}' for number in range(256))
        with self.assertRaises(ScientistAdmissionError):
            client.exchange(self.request)
        self.assertEqual(server.requests, [])

    def test_authority_revoked_during_slow_peer_observation_cannot_dispatch_or_publish(self):
        server = self.server()
        for revoke_call in (2, 3):
            calls, revoked = 0, False

            def current(_peer):
                nonlocal calls, revoked
                calls += 1
                if calls == revoke_call:
                    revoked = True
                return True

            def authorize(_request, _peer):
                if revoked:
                    raise ScientistAdmissionError('Synthetic current target capability expired during peer observation')

            self.authenticator.still_current.side_effect = current
            client = self.client(server, authorize=authorize)
            expected = ScientistAdmissionError if revoke_call == 2 else ScientistUncertainTurn
            with self.subTest(revoke_call=revoke_call), self.assertRaises(expected):
                client.exchange(self.request)
            self.assertEqual(len(server.requests), 0 if revoke_call == 2 else 1)
            self.assertEqual(client.uncertain_control_id, None if revoke_call == 2 else self.request['control_id'])

    def test_socket_replacement_during_persist_denies_before_send(self):
        server = self.server()
        replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(replacement.close)

        def persist(*_arguments):
            server.path.rename(self.root / 'original.sock')
            replacement.bind(str(server.path))
            server.path.chmod(0o600)

        client = self.client(server, persist_intent=persist)
        with self.assertRaises(ScientistAdmissionError):
            client.exchange(self.request)
        self.assertIsNone(client.uncertain_control_id)
        self.assertEqual(server.requests, [])

    def test_partial_send_is_attempted_before_sendall_raises(self):
        server = self.server()
        real_socket = socket.socket
        sent = []

        class PartialSocket:
            def __init__(self, *arguments):
                self.connection = real_socket(*arguments)

            def __enter__(self):
                return self

            def __exit__(self, *_arguments):
                self.connection.close()

            def __getattr__(self, name):
                return getattr(self.connection, name)

            def sendall(self, frame):
                sent.append(frame[:16])
                self.connection.sendall(frame[:16])
                raise OSError('Synthetic partial write')

        client = self.client(server)
        def socket_factory(*arguments, **keywords):
            return real_socket(*arguments, **keywords) if 'fileno' in keywords else PartialSocket(*arguments)

        with patch('aos.scientist_evidence_client.socket.socket', socket_factory):
            with self.assertRaises(ScientistUncertainTurn):
                client.exchange(self.request)
        self.assertEqual(sent, [self.codec.encode_request(self.request)[:16]])
        self.assertEqual(client.uncertain_control_id, self.request['control_id'])
