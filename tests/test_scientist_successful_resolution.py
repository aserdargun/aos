import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
import threading
import time
from unittest.mock import AsyncMock, Mock

from aos.contracts import REPO_ROOT
from aos.scientist_async import ScientistAsyncTurnClient
from aos.scientist_desktop import ScientistDesktopBinding
from aos.scientist_protocol import ScientistTurnReceipt
from aos.scientist_successful_resolution import ScientistRetainedResolutionResult
from aos.scientist_transport import ScientistAdmissionError
import test_scientist_resolution as resolution_cases


class ScientistSuccessfulResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = resolution_cases.ScientistResolutionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        original = self.fixture.fixture
        self.request = original.request
        self.receipt = ScientistTurnReceipt.model_validate({'version': 1,
            'request_id': self.request.request_id, 'profile_id': self.request.profile_id,
            'deployment_digest': self.request.deployment_digest, **original.result}, strict=True)
        original.journal.record_receipt(self.receipt, original.peer)
        self.before = deepcopy(original.rows())
        self.factory = Mock()
        self.binding = ScientistDesktopBinding(SimpleNamespace(store=original.store),
            admission_history=original.history, resolver_factory=self.factory)
        self.binding._journal = original.journal
        self.binding._state = object()
        self.binding._owner_loop = asyncio.get_running_loop()
        self.binding._current = Mock(return_value=None)
        self.active = asyncio.create_task(asyncio.sleep(60))
        client = ScientistAsyncTurnClient(Path('/synthetic-unused.sock'))
        client._active = self.active
        client._active_request_id = self.request.request_id
        client._context = (asyncio.get_running_loop(), threading.Event(), time.monotonic() + 30)
        self.binding.engine = SimpleNamespace(client=client)
        self.resolver = SimpleNamespace(resolve_successful=AsyncMock(side_effect=self.resolve))
        self.factory.return_value = self.resolver

    async def asyncTearDown(self):
        self.active.cancel()
        await asyncio.gather(self.active, return_exceptions=True)

    async def resolve(self, request_id):
        self.assertEqual(request_id, self.request.request_id)
        row = self.fixture.resolve()
        return ScientistRetainedResolutionResult(request_id=row['request_id'],
            admission_record_sha256=row['admission_record_sha256'],
            reconcile_control_id=row['control_id'], response_sha256=row['response_sha256'],
            terminal_receipt_sha256=row['terminal_receipt_sha256'],
            current_binding=self.fixture.fixture.binding)

    async def test_success_checks_actual_resolution_and_preserves_original_intent(self):
        await self.binding.resolve_successful(self.request.request_id)
        self.assertEqual(self.fixture.count(), 1)
        self.assertEqual(self.fixture.fixture.rows(), self.before)
        self.factory.assert_called_once_with(self.binding, self.fixture.fixture.binding)
        self.assertEqual(self.binding._current.call_count, 2)

    async def test_synchronous_trusted_resolver_stays_on_original_owner_loop(self):
        def resolved(request_id):
            self.assertIs(asyncio.get_running_loop(), self.binding._owner_loop)
            row = self.fixture.resolve()
            return ScientistRetainedResolutionResult(request_id=row['request_id'],
                admission_record_sha256=row['admission_record_sha256'],
                reconcile_control_id=row['control_id'], response_sha256=row['response_sha256'],
                terminal_receipt_sha256=row['terminal_receipt_sha256'],
                current_binding=self.fixture.fixture.binding)

        self.resolver.resolve_successful = resolved
        await self.binding.resolve_successful(self.request.request_id)
        self.assertEqual(self.fixture.count(), 1)
        self.assertEqual(self.fixture.fixture.rows(), self.before)

    async def test_default_factory_and_wrong_loop_fail_closed_before_factory(self):
        self.binding._resolver_factory = None
        with self.assertRaises(ScientistAdmissionError):
            await self.binding.resolve_successful(self.request.request_id)
        self.binding._resolver_factory = self.factory
        self.binding._owner_loop = object()
        with self.assertRaises(ScientistAdmissionError):
            await self.binding.resolve_successful(self.request.request_id)
        self.factory.assert_not_called()
        self.assertEqual(self.fixture.count(), 0)

    async def test_cleanup_failure_keeps_receipt_unresolved_without_retry(self):
        self.fixture.physical.side_effect = ScientistAdmissionError('Synthetic cleanup unproven')
        with self.assertRaises(ScientistAdmissionError):
            await self.binding.resolve_successful(self.request.request_id)
        self.assertEqual(self.fixture.count(), 0)
        self.assertEqual(self.fixture.fixture.rows(), self.before)
        self.resolver.resolve_successful.assert_awaited_once()

    async def test_wrong_active_request_is_rejected_before_factory(self):
        self.binding.engine.client._active_request_id = 'f' * 32
        with self.assertRaises(ScientistAdmissionError):
            await self.binding.resolve_successful(self.request.request_id)
        self.factory.assert_not_called()
        self.assertEqual(self.fixture.count(), 0)

    async def test_late_completed_task_cannot_clear_current_request_context(self):
        previous = asyncio.create_task(asyncio.sleep(0))
        await previous
        client = self.binding.engine.client
        context = client._context
        client._finish_task(previous)
        self.assertEqual(client._active_request_id, self.request.request_id)
        self.assertIs(client._context, context)
        self.assertIs(client._active, self.active)

    async def test_typed_claim_without_durable_resolution_is_rejected(self):
        self.resolver.resolve_successful = AsyncMock(return_value=ScientistRetainedResolutionResult(
            request_id=self.request.request_id, admission_record_sha256='a' * 64,
            reconcile_control_id='b' * 32, response_sha256='c' * 64,
            terminal_receipt_sha256='d' * 64, current_binding=self.fixture.fixture.binding))
        with self.assertRaises(ScientistAdmissionError):
            await self.binding.resolve_successful(self.request.request_id)
        self.assertEqual(self.fixture.count(), 0)

    async def test_revoked_owner_after_resolution_is_not_returned_as_success(self):
        self.binding._current.side_effect = [None, ScientistAdmissionError('Synthetic owner revoked')]
        with self.assertRaises(ScientistAdmissionError):
            await self.binding.resolve_successful(self.request.request_id)

    async def test_async_hook_runs_on_original_loop_after_receipt_and_before_return(self):
        observed = []
        owner_loop = asyncio.get_running_loop()

        async def resolved(request_id):
            self.assertIs(asyncio.get_running_loop(), owner_loop)
            observed.append('resolution')
            await self.binding.resolve_successful(request_id)

        client = ScientistAsyncTurnClient(Path('/synthetic-unused.sock'), resolve_successful=resolved)
        self.binding.engine.client = client
        client.client.infer = Mock(side_effect=lambda *arguments, **options: self.receipt)
        receipt = await client.infer(self.request)
        observed.append('return')
        self.assertEqual(receipt, self.receipt)
        self.assertEqual(observed, ['resolution', 'return'])
        self.assertIsNone(client.uncertain_request_id)
        self.assertFalse(client.cleanup_pending)
        self.assertIsNone(client._active_request_id)
        self.assertIsNotNone(client._context)

    async def test_timeout_does_not_return_receipt_or_resolve_intent(self):
        async def stalled(request_id):
            await asyncio.sleep(2)

        client = ScientistAsyncTurnClient(Path('/synthetic-unused.sock'),
                                          timeout_seconds=1, resolve_successful=stalled)
        client.client.infer = Mock(return_value=self.receipt)
        with self.assertRaises(ScientistAdmissionError):
            await client.infer(self.request)
        self.assertEqual(self.fixture.count(), 0)
        self.assertEqual(self.fixture.fixture.rows(), self.before)
        client.client.infer.assert_called_once()

    async def test_cancellation_during_resolution_never_repeats_inference(self):
        entered = asyncio.Event()

        async def stalled(request_id):
            entered.set()
            await asyncio.sleep(2)

        client = ScientistAsyncTurnClient(Path('/synthetic-unused.sock'),
                                          timeout_seconds=1, resolve_successful=stalled)
        client.client.infer = Mock(return_value=self.receipt)
        task = asyncio.create_task(client.infer(self.request))
        await entered.wait()
        active = client._active
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with self.assertRaises(ScientistAdmissionError):
            await client.infer(self.request)
        await asyncio.gather(active, return_exceptions=True)
        self.assertEqual(self.fixture.count(), 0)
        client.client.infer.assert_called_once()
        self.assertIsNone(client._active_request_id)

    async def test_canonical_result_schema_matches_typed_model(self):
        schema = json.loads((REPO_ROOT / 'schemas/scientist_successful_resolution.schema.json').read_text())
        self.assertEqual(schema, ScientistRetainedResolutionResult.model_json_schema())
