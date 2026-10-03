"""Owned CPU sockets and host-thread SQLite staging; no Scientist or GPU."""

import asyncio
from dataclasses import replace
import json
import socket
import threading
import time
import unittest
from unittest.mock import AsyncMock

from aos.scientist_admission_history import ScientistAdmissionHistory
from aos.scientist_async import ScientistAsyncTurnClient
from aos.scientist_intents import ScientistIntentJournal
from aos.scientist_terminal import canonical
from aos.scientist_transport import ScientistAdmissionError, ScientistUncertainTurn

import test_scientist_bootstrap as bootstrap_cases


class SyntheticInferEndpoint:
    def __init__(self, root, receipt):
        self.path = root / 'synthetic-infer.sock'
        self.receipt = receipt
        self.accepts, self.requests = [], []
        self.stopped = threading.Event()
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
            self.accepts.append(time.monotonic())
            with connection:
                connection.settimeout(1)
                raw = bytearray()
                try:
                    while b'\n' not in raw:
                        chunk = connection.recv(8192)
                        if not chunk:
                            break
                        raw.extend(chunk)
                    if raw:
                        self.requests.append(bytes(raw))
                        connection.sendall(canonical(self.receipt.model_dump(mode='json')).encode() + b'\n')
                except OSError:
                    pass

    def close(self):
        self.stopped.set()
        self.listener.close()
        self.thread.join(2)
        if self.thread.is_alive():
            raise AssertionError('Owned synthetic inference endpoint did not close')


class ScientistBootstrapAsyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = bootstrap_cases.ScientistBootstrapTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.host_thread = threading.get_ident()
        self.events = []
        self.endpoint = SyntheticInferEndpoint(self.fixture.root, self.fixture.fixture.receipt)
        self.addCleanup(self.endpoint.close)

    def on_host(self, name, callback):
        def invoke(*arguments):
            self.assertEqual(threading.get_ident(), self.host_thread)
            self.events.append((name, self.fixture.store.connection.in_transaction))
            return callback(*arguments)
        return invoke

    def setup_client(self, *, mode='success', timeout=2, peer=None, control_timeout=None):
        self.server = self.fixture.server(mode=mode)
        self.capture = self.fixture.capture(self.server, timeout_seconds=timeout if control_timeout is None else control_timeout,
            verify_current=self.on_host('current', self.fixture.current),
            verify_capture=self.on_host('capture', self.fixture.expected),
            persist_intent=self.on_host('durable', self.fixture.persist),
            clock=self.on_host('clock', lambda: self.fixture.now))
        self.history = ScientistAdmissionHistory(self.fixture.store, capture=self.capture,
            record_version='2.0', verify_current=self.on_host('history', lambda *_args: None),
            clock=lambda: self.fixture.now)
        self.journal = ScientistIntentJournal(self.fixture.store, self.fixture.binding, admission_history=self.history)
        async def prepare(request, **lifecycle):
            await self.capture.prepare_async(request, self.fixture.binding, peer or self.fixture.peer, **lifecycle)
        self.client = ScientistAsyncTurnClient(self.endpoint.path, timeout_seconds=timeout,
            authenticator=self.fixture.authenticator,
            verify_admission=self.on_host('admission', lambda *_args: None),
            persist_intent=self.on_host('infer_intent', self.journal.persist_intent),
            record_receipt=self.on_host('receipt', self.journal.record_receipt), prepare_infer=prepare)

    async def wait_until(self, predicate):
        async with asyncio.timeout(2):
            while not predicate():
                await asyncio.sleep(.005)

    def assert_no_original_intent(self):
        self.assertEqual(self.fixture.store.connection.execute('SELECT count(*) FROM scientist_turn_intents').fetchone()[0], 0)
        self.assertFalse(self.fixture.store.connection.in_transaction)

    async def test_post_response_verification_uses_original_infer_deadline(self):
        self.setup_client(control_timeout=.15)
        original = self.capture._verify_capture

        def slow_verification(*arguments):
            if not self.fixture.store.connection.in_transaction:
                time.sleep(.2)
            return original(*arguments)

        self.capture._verify_capture = slow_verification
        receipt = await self.client.infer(self.fixture.request)
        self.assertEqual(receipt, self.fixture.fixture.receipt)
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(len(self.endpoint.requests), 1)
        deadline = self.fixture.store.connection.execute('SELECT deadline FROM scientist_turn_intents').fetchone()[0]
        self.assertEqual(deadline, self.client._context[2])

    async def test_post_response_outer_expiry_blocks_infer_and_history(self):
        self.setup_client(timeout=1, control_timeout=.12)
        original = self.capture._verify_capture

        def expired_verification(*arguments):
            time.sleep(1.1)
            return original(*arguments)

        self.capture._verify_capture = expired_verification
        with self.assertRaises(ScientistAdmissionError):
            await self.client.infer(self.fixture.request)
        self.assert_no_original_intent()
        self.assertEqual(len(self.endpoint.accepts), 0)
        self.assertIsNone(self.capture._prepared)

    async def assert_late_verification_denied(self, change):
        self.setup_client(control_timeout=.15)
        original = self.capture._verify_capture

        def changed_verification(*arguments):
            time.sleep(.2)
            change()
            return original(*arguments)

        self.capture._verify_capture = changed_verification
        with self.assertRaises(ScientistAdmissionError):
            await self.client.infer(self.fixture.request)
        self.assert_no_original_intent()
        self.assertEqual(self.fixture.store.connection.execute('SELECT count(*) FROM scientist_admission_history').fetchone()[0], 0)
        self.assertEqual(len(self.endpoint.accepts), 0)
        self.assertIsNone(self.capture._prepared)

    async def test_post_response_capability_expiry_still_blocks_dispatch(self):
        await self.assert_late_verification_denied(lambda: setattr(self.fixture, 'now', 150))

    async def test_post_response_cancellation_still_blocks_dispatch(self):
        await self.assert_late_verification_denied(lambda: self.client._context[1].set())

    async def test_post_response_source_change_still_blocks_dispatch(self):
        await self.assert_late_verification_denied(lambda: self.fixture.stable['source_fingerprints'].update(aos='f' * 64))

    async def test_post_response_generation_change_still_blocks_dispatch(self):
        await self.assert_late_verification_denied(lambda: setattr(self.fixture, 'peer', replace(self.fixture.peer, invocation_id='e' * 32)))

    async def test_without_outer_deadline_post_response_keeps_control_limit(self):
        self.setup_client(control_timeout=.12)
        original = self.capture._verify_capture

        def slow_verification(*arguments):
            time.sleep(.2)
            return original(*arguments)

        self.capture._verify_capture = slow_verification
        with self.assertRaises(ScientistAdmissionError):
            await self.capture.prepare_async(self.fixture.request, self.fixture.binding, self.fixture.peer)
        self.assert_no_original_intent()
        self.assertEqual(len(self.endpoint.accepts), 0)
        self.assertIsNone(self.capture._prepared)

    async def test_actual_prefetch_infer_and_capture_keep_all_sql_callbacks_on_host(self):
        self.setup_client()
        receipt = await self.client.infer(self.fixture.request)
        self.assertEqual(receipt, self.fixture.fixture.receipt)
        record, _checksum = self.history.read(self.fixture.request.request_id)
        self.assertEqual(record.schema_version, '2.0')
        self.assertEqual(record.admission_binding.model_dump(mode='json'), self.fixture.stable)
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(len(self.endpoint.accepts), 2)
        self.assertEqual(json.loads(self.endpoint.requests[0]), self.fixture.request.model_dump(mode='json'))
        self.assertIn(('capture', False), self.events)
        self.assertIn(('capture', True), self.events)
        stored_deadline = self.fixture.store.connection.execute('SELECT deadline FROM scientist_turn_intents').fetchone()[0]
        self.assertEqual(stored_deadline, self.client._context[2])
        self.assertEqual([event for event in self.events if event[0] == 'durable'], [('durable', False)])
        self.assertFalse(self.client.cleanup_pending)
        self.assertIsNone(self.capture._prepared)

    async def test_stalled_bootstrap_keeps_loop_responsive_and_never_accepts_infer(self):
        self.setup_client(mode='hold')
        task = asyncio.create_task(self.client.infer(self.fixture.request))
        await self.wait_until(self.server.entered.is_set)
        beats = []
        for tick in range(5):
            await asyncio.sleep(.01)
            beats.append(tick)
        self.assertEqual(len(beats), 5)
        self.assertEqual(self.endpoint.accepts, [])
        self.assertTrue(self.client.cleanup_pending)
        with self.assertRaises(ScientistAdmissionError):
            await self.client.infer(self.fixture.request)
        self.server.release.set()
        await task
        self.assertEqual(len(self.endpoint.accepts), 2)
        self.assertEqual(len(self.endpoint.requests), 1)

    async def test_pause_during_bootstrap_cannot_dispatch_or_write_late_callbacks(self):
        self.setup_client(mode='hold')
        task = asyncio.create_task(self.client.infer(self.fixture.request))
        await self.wait_until(self.server.entered.is_set)
        before = list(self.events)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(self.client.cleanup_pending)
        with self.assertRaises(ScientistAdmissionError):
            await self.client.infer(self.fixture.request)
        with self.assertRaises(ScientistAdmissionError):
            self.client.rearm(self.fixture.request.request_id, verify_resolution=lambda *_args: None)
        self.server.release.set()
        await self.wait_until(lambda: not self.client.cleanup_pending)
        self.assertEqual(self.events, before)
        self.assertEqual(self.endpoint.accepts, [])
        self.assertTrue(self.capture._blocked)
        self.assertIsNone(self.capture._prepared)
        self.assert_no_original_intent()
        with self.assertRaises(ScientistAdmissionError):
            await self.capture.prepare_async(self.fixture.request, self.fixture.binding, self.fixture.peer)

    async def test_direct_stage_cancellation_retains_lock_until_worker_finishes(self):
        self.setup_client(mode='hold')
        task = asyncio.create_task(self.capture.prepare_async(self.fixture.request, self.fixture.binding, self.fixture.peer))
        await self.wait_until(self.server.entered.is_set)
        self.assertTrue(self.capture._lock.locked())
        with self.assertRaises(ScientistAdmissionError):
            await self.capture.prepare_async(self.fixture.request, self.fixture.binding, self.fixture.peer)
        task.cancel()
        await asyncio.sleep(0)
        self.assertTrue(self.capture._lock.locked())
        self.server.release.set()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertFalse(self.capture._lock.locked())
        self.assert_no_original_intent()

    async def test_stage_expected_peer_mismatch_denies_before_infer_connect(self):
        self.setup_client(peer=replace(self.fixture.peer, start_ticks=self.fixture.peer.start_ticks + 1))
        with self.assertRaises(ScientistAdmissionError):
            await self.client.infer(self.fixture.request)
        self.assertEqual(self.server.requests, [])
        self.assertEqual(self.endpoint.accepts, [])
        self.assert_no_original_intent()

    async def test_changed_final_infer_peer_cannot_consume_prefetched_ack(self):
        self.setup_client()
        prepare = self.client._prepare_infer
        async def change_peer(request, **lifecycle):
            await prepare(request, **lifecycle)
            self.fixture.authenticator.authenticate.return_value = replace(self.fixture.peer, start_ticks=self.fixture.peer.start_ticks + 1)
        self.client._prepare_infer = change_peer
        with self.assertRaises(ScientistAdmissionError):
            await self.client.infer(self.fixture.request)
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(self.endpoint.requests, [])
        self.assert_no_original_intent()

    async def test_stage_error_or_non_none_result_never_opens_infer_socket(self):
        self.setup_client()
        for prepare in (AsyncMock(side_effect=ScientistAdmissionError('Synthetic denied')), AsyncMock(return_value=True)):
            self.client._prepare_infer = prepare
            with self.assertRaises(ScientistAdmissionError):
                await self.client.infer(self.fixture.request)
        self.assertEqual(self.endpoint.accepts, [])
        self.assertEqual(self.server.requests, [])
        self.assert_no_original_intent()

    async def test_shared_deadline_expires_before_infer_connect(self):
        self.setup_client(mode='hold', timeout=1)
        with self.assertRaises((ScientistAdmissionError, ScientistUncertainTurn)):
            await self.client.infer(self.fixture.request)
        self.assertEqual(self.endpoint.accepts, [])
        self.assertTrue(self.capture._blocked)
        self.assert_no_original_intent()

    async def test_invalid_or_expired_explicit_transport_deadline_denies_before_connect(self):
        self.setup_client()
        for deadline in (True, 'invalid', float('nan'), float('inf'), time.monotonic() - 1):
            with self.subTest(deadline=deadline), self.assertRaises((ScientistAdmissionError, TimeoutError)):
                self.client.client.infer(self.fixture.request, deadline=deadline)
        self.assertEqual(self.endpoint.accepts, [])
        self.assertEqual(self.server.requests, [])
        self.assert_no_original_intent()

    async def test_cancellation_during_final_capture_validation_publishes_no_ack(self):
        self.setup_client()
        at_revocation = []
        def revoke(*arguments):
            self.fixture.verify_capture(*arguments)
            self.client._context[1].set()
            at_revocation.extend(self.events)
        self.fixture.expected.side_effect = revoke
        with self.assertRaises(ScientistAdmissionError):
            await self.client.infer(self.fixture.request)
        self.assertEqual(self.events, at_revocation)
        self.assertEqual(self.endpoint.accepts, [])
        self.assertIsNone(self.capture._prepared)
        self.assertTrue(self.capture._blocked)
        self.assert_no_original_intent()


if __name__ == '__main__':
    unittest.main()
