"""Synthetic typed staging guards; socket/thread lifecycle is tested separately."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import AsyncMock, Mock

from aos.scientist_admission_history import ScientistAdmissionHistory
from aos.scientist_desktop import ScientistDesktopBinding, create_scientist_desktop_scheduler
from aos.scientist_transport import ScientistAdmissionError

import test_scientist_bootstrap as bootstrap_cases


class ScientistBootstrapDesktopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = bootstrap_cases.ScientistBootstrapTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.capture = self.fixture.capture(self.fixture.server())
        self.capture.prepare_async = AsyncMock(return_value=None)
        self.history = ScientistAdmissionHistory(self.fixture.store, capture=self.capture, record_version='2.0')
        self.controller = SimpleNamespace(store=self.fixture.store)
        self.peer_reader = Mock(return_value=self.fixture.peer)
        self.binding = ScientistDesktopBinding(self.controller, admission_history=self.history,
            bootstrap_capture=self.capture, expected_bootstrap_peer=self.peer_reader)
        self.binding._journal = SimpleNamespace(binding=self.fixture.binding.model_copy(deep=True))
        self.binding.verify_admission = Mock(return_value=None)
        self.cancelled = threading.Event()

    async def stage(self):
        await self.binding.prepare_infer(self.fixture.request, cancel_event=self.cancelled,
                                         deadline=asyncio.get_running_loop().time() + 2)

    async def test_exact_typed_capture_is_staged_and_peer_is_rechecked(self):
        await self.stage()
        self.assertEqual(self.peer_reader.call_count, 2)
        self.capture.prepare_async.assert_awaited_once()
        request, binding, peer = self.capture.prepare_async.call_args.args
        self.assertEqual(request, self.fixture.request)
        self.assertEqual(binding, self.fixture.binding)
        self.assertEqual(peer, self.fixture.peer)

    async def test_peer_change_after_staging_denies(self):
        self.peer_reader.side_effect = [self.fixture.peer, replace(self.fixture.peer, start_ticks=2)]
        with self.assertRaises(ScientistAdmissionError):
            await self.stage()

    async def test_boolean_peer_denies_before_network_stage(self):
        self.peer_reader.return_value = True
        with self.assertRaises(ScientistAdmissionError):
            await self.stage()
        self.capture.prepare_async.assert_not_awaited()

    async def test_journal_or_capture_rebinding_after_stage_denies(self):
        async def mutate(*arguments, **options):
            self.binding._journal = SimpleNamespace(binding=self.fixture.binding.model_copy(deep=True))

        self.capture.prepare_async.side_effect = mutate
        with self.assertRaises(ScientistAdmissionError):
            await self.stage()

    async def test_history_version_change_during_staging_denies(self):
        async def mutate(*arguments, **options):
            self.history._record_version = '1.0'

        self.capture.prepare_async.side_effect = mutate
        with self.assertRaises(ScientistAdmissionError):
            await self.stage()

    async def test_cancelled_or_open_sql_never_stages_network(self):
        self.cancelled.set()
        with self.assertRaises(ScientistAdmissionError):
            await self.stage()
        self.capture.prepare_async.assert_not_awaited()
        self.cancelled.clear()
        self.fixture.store.connection.execute('BEGIN IMMEDIATE')
        try:
            with self.assertRaises(ScientistAdmissionError):
                await self.stage()
            self.capture.prepare_async.assert_not_awaited()
        finally:
            self.fixture.store.connection.rollback()

    async def test_factory_rejects_unpaired_or_different_capture_before_client_creation(self):
        for options in ({'bootstrap_capture': self.capture},
                        {'expected_bootstrap_peer': self.peer_reader},
                        {'bootstrap_capture': self.capture, 'expected_bootstrap_peer': self.peer_reader,
                         'admission_history': self.history}):
            with self.subTest(options=tuple(options)):
                with self.assertRaises(ScientistAdmissionError):
                    create_scientist_desktop_scheduler(self.controller, None, {}, self.fixture.root / 'infer.sock',
                                                       **options)
