"""Synthetic CPU transport rearm; callbacks are not physical release proof."""

import asyncio
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from aos.scientist_async import ScientistAsyncTurnClient
from aos.scientist_protocol import ScientistTurnRequest
from aos.scientist_transport import (
    BrokerPeer, ScientistAdmissionError, ScientistTurnClient, ScientistUncertainTurn,
)
from test_scientist_transport import SyntheticBroker


class RearmFixture:
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.request = ScientistTurnRequest(request_id='a' * 32,
            profile_id='aos.decider.turn.v1', deployment_digest='b' * 64,
            payload={'synthetic_cpu_fixture': True})
        self.fresh = self.request.model_copy(update={'request_id': 'c' * 32})
        self.authenticator = Mock()
        self.authenticator.authenticate.return_value = BrokerPeer(
            os.getpid(), os.getuid(), 1, 'synthetic', 'd' * 32, '/synthetic')
        self.authenticator.still_current.return_value = True
        self.admission = Mock(return_value=None)
        self.intent = Mock(return_value=None)
        self.receipt = Mock(return_value=None)
        self.validation = Mock(return_value=None)
        self.verification = Mock(return_value=None)
        self.broker = SyntheticBroker(self.root, 'disconnect')
        self.addCleanup(self.broker.close)

    def client(self, factory=ScientistTurnClient):
        return factory(self.broker.path, authenticator=self.authenticator,
            verify_admission=self.admission, persist_intent=self.intent,
            record_receipt=self.receipt, validate_receipt=self.validation)


class ScientistRearmTests(RearmFixture, unittest.TestCase):
    def uncertain(self):
        client = self.client()
        with self.assertRaises(ScientistUncertainTurn):
            client.infer(self.request)
        return client

    def test_default_wrong_target_noncallable_and_non_none_verification_remain_fenced(self):
        client = self.uncertain()
        attempts = client._attempted
        with patch('aos.scientist_transport.socket.socket') as connect:
            with self.assertRaises(ScientistAdmissionError):
                client.rearm(self.request.request_id)
            for target in (self.fresh.request_id, None, True, 'invalid'):
                with self.subTest(target=target), self.assertRaises(ScientistAdmissionError):
                    client.rearm(target, verify_resolution=self.verification)
            self.verification.assert_not_called()
            for result in (False, True, {}, {'request_id': self.request.request_id}):
                with self.subTest(result=result), self.assertRaises(ScientistAdmissionError):
                    client.rearm(self.request.request_id, verify_resolution=Mock(return_value=result))
            with self.assertRaises(ScientistAdmissionError):
                client.rearm(self.request.request_id, verify_resolution=None)
            connect.assert_not_called()
        self.assertEqual(client.uncertain_request_id, self.request.request_id)
        self.assertIs(client._attempted, attempts)
        self.assertEqual(attempts, {self.request.request_id})

    def test_explicit_rearm_preserves_configuration_replay_and_fresh_admission(self):
        client = self.uncertain()
        before = (client.socket_path, client.authenticator, client.verify_admission,
                  client.persist_intent, client.record_receipt, client.validate_receipt, client._attempted)
        with patch('aos.scientist_transport.socket.socket') as connect:
            client.rearm(self.request.request_id, verify_resolution=self.verification)
            connect.assert_not_called()
        self.verification.assert_called_once_with(self.request.request_id)
        self.assertIsNone(client.uncertain_request_id)
        after = (client.socket_path, client.authenticator, client.verify_admission,
                 client.persist_intent, client.record_receipt, client.validate_receipt, client._attempted)
        for original, retained in zip(before, after):
            self.assertIs(original, retained)
        with self.assertRaises(ScientistAdmissionError):
            client.infer(self.request)
        with self.assertRaises(ScientistAdmissionError):
            client.rearm(self.request.request_id, verify_resolution=self.verification)
        self.broker.mode = 'success'
        self.admission.reset_mock()
        self.assertEqual(client.infer(self.fresh).request_id, self.fresh.request_id)
        self.assertEqual(self.admission.call_count, 3)
        self.assertEqual(len(self.broker.requests), 2)

    def test_verifier_failure_and_reentrant_calls_keep_original_lock_and_latch(self):
        client = self.uncertain()

        def verify(request_id):
            self.assertEqual(request_id, self.request.request_id)
            self.assertTrue(client._lock.locked())
            with self.assertRaises(ScientistAdmissionError):
                client.infer(self.fresh)
            with self.assertRaises(ScientistAdmissionError):
                client.rearm(request_id, verify_resolution=self.verification)
            raise OSError('synthetic proof revocation')

        with self.assertRaises(OSError):
            client.rearm(self.request.request_id, verify_resolution=verify)
        self.assertEqual(client.uncertain_request_id, self.request.request_id)
        self.assertFalse(client._lock.locked())
        self.verification.assert_not_called()

    def test_rearm_never_resets_lifetime_attempt_limit(self):
        client = self.uncertain()
        client._attempted.update(format(index, '032x') for index in range(255))
        client.rearm(self.request.request_id, verify_resolution=self.verification)
        with self.assertRaises(ScientistAdmissionError):
            client.infer(self.fresh)
        self.assertEqual(len(client._attempted), 256)
        self.assertEqual(len(self.broker.requests), 1)

    def test_fresh_client_cannot_rearm_and_active_worker_denies_without_callback(self):
        client = self.client()
        with self.assertRaises(ScientistAdmissionError):
            client.rearm(self.request.request_id, verify_resolution=self.verification)
        client._lock.acquire()
        try:
            with self.assertRaises(ScientistAdmissionError):
                client.rearm(self.request.request_id, verify_resolution=self.verification)
        finally:
            client._lock.release()
        self.verification.assert_not_called()


class ScientistAsyncRearmTests(RearmFixture, unittest.IsolatedAsyncioTestCase):
    async def settle(self, client):
        if client._active is not None:
            completed, pending = await asyncio.wait({client._active}, timeout=2)
            self.assertFalse(pending)
            for task in completed:
                if not task.cancelled():
                    task.exception()

    async def test_uncertain_async_client_requires_explicit_rearm_then_fresh_admission(self):
        client = self.client(ScientistAsyncTurnClient)
        with self.assertRaises(ScientistUncertainTurn):
            await client.infer(self.request)
        with self.assertRaises(ScientistAdmissionError):
            client.rearm(self.request.request_id)
        with self.assertRaises(ScientistAdmissionError):
            await client.infer(self.fresh)
        original = client.client
        callbacks = client._callbacks
        client.rearm(self.request.request_id, verify_resolution=self.verification)
        self.assertIs(client.client, original)
        self.assertIs(client._callbacks, callbacks)
        self.broker.mode = 'success'
        self.assertEqual((await client.infer(self.fresh)).request_id, self.fresh.request_id)

    async def test_cancelled_wire_wait_stays_fenced_until_terminal_and_verified(self):
        self.broker.mode = 'hold'
        client = self.client(ScientistAsyncTurnClient)
        pending = asyncio.create_task(client.infer(self.request))
        self.assertTrue(await asyncio.to_thread(self.broker.entered.wait, 1))
        with self.assertRaises(ScientistAdmissionError):
            client.rearm(self.request.request_id, verify_resolution=self.verification)
        self.verification.assert_not_called()
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        await self.settle(client)
        self.assertTrue(client._cancelled)
        with self.assertRaises(ScientistAdmissionError):
            await client.infer(self.fresh)
        with self.assertRaises(ScientistAdmissionError):
            client.rearm(self.fresh.request_id, verify_resolution=self.verification)
        self.verification.side_effect = OSError('synthetic current rights revoked')
        with self.assertRaises(OSError):
            client.rearm(self.request.request_id, verify_resolution=self.verification)
        self.assertTrue(client._cancelled)
        self.assertEqual(client.uncertain_request_id, self.request.request_id)
        self.verification.side_effect = None
        client.rearm(self.request.request_id, verify_resolution=self.verification)
        self.assertFalse(client._cancelled)
        self.assertIsNone(client.uncertain_request_id)
        with self.assertRaises(ScientistAdmissionError):
            await client.infer(self.request)
        self.broker.release.set()
        self.broker.mode = 'success'
        self.assertEqual((await client.infer(self.fresh)).request_id, self.fresh.request_id)

    async def test_cancellation_before_send_retains_exact_target_and_requires_verification(self):
        self.broker.mode = 'success'
        client = self.client(ScientistAsyncTurnClient)
        pending = asyncio.create_task(client.infer(self.request))

        def cancel_after_intent(*arguments):
            pending.cancel()

        self.intent.side_effect = cancel_after_intent
        with self.assertRaises(asyncio.CancelledError):
            await pending
        await self.settle(client)
        self.assertIsNone(client.uncertain_request_id)
        self.assertTrue(client._cancelled)
        self.assertEqual(client.client._attempted, set())
        self.assertEqual(self.broker.requests, [])
        with self.assertRaises(ScientistAdmissionError):
            client.rearm(self.request.request_id)
        with self.assertRaises(ScientistAdmissionError):
            client.rearm(self.fresh.request_id, verify_resolution=self.verification)
        self.verification.assert_not_called()
        client.rearm(self.request.request_id, verify_resolution=self.verification)
        self.intent.side_effect = None
        self.assertEqual((await client.infer(self.fresh)).request_id, self.fresh.request_id)

    async def test_cancelled_terminal_task_does_not_hide_live_thread_or_elapsed_timeout(self):
        client = self.client(ScientistAsyncTurnClient)
        entered, release, exited = threading.Event(), threading.Event(), threading.Event()

        def worker(request, *, cancel_event, deadline=None):
            try:
                with client.client._lock:
                    entered.set()
                    release.wait(2)
                    raise ScientistAdmissionError('synthetic worker finished after cancellation')
            finally:
                exited.set()

        with patch.object(client.client, 'infer', side_effect=worker):
            pending = asyncio.create_task(client.infer(self.request))
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            active = client._active
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
            self.assertFalse(client.cleanup_pending)
            try:
                with patch('aos.scientist_async.time.monotonic', return_value=10 ** 12):
                    with self.assertRaises(ScientistAdmissionError):
                        client.rearm(self.request.request_id, verify_resolution=self.verification)
                self.verification.assert_not_called()
                self.assertTrue(client._cancelled)
            finally:
                release.set()
                self.assertTrue(await asyncio.to_thread(exited.wait, 1))
        client.rearm(self.request.request_id, verify_resolution=self.verification)
        self.assertFalse(client._cancelled)
        self.assertIsNone(client._active)

    async def test_old_queued_host_callback_cannot_use_rearmed_context(self):
        client = self.client(ScientistAsyncTurnClient)
        queued = []
        old_loop = Mock()
        old_loop.call_soon_threadsafe.side_effect = queued.append
        old_cancelled = threading.Event()
        client._context = (old_loop, old_cancelled, time.monotonic() + .02)
        client._cancelled = True
        client._cancelled_request_id = self.request.request_id
        failures = []

        def host_worker():
            with client.client._lock:
                try:
                    client._host(0, (self.request,))
                except TimeoutError as error:
                    failures.append(error)

        thread = threading.Thread(target=host_worker)
        thread.start()
        await asyncio.to_thread(thread.join, 1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertTrue(old_cancelled.is_set())
        client.rearm(self.request.request_id, verify_resolution=self.verification)
        current_cancelled = threading.Event()
        client._context = (asyncio.get_running_loop(), current_cancelled, time.monotonic() + 1)
        self.assertEqual(len(queued), 1)
        queued[0]()
        self.admission.assert_not_called()
        self.assertFalse(current_cancelled.is_set())
        self.assertEqual(self.broker.requests, [])
