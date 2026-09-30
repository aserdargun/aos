import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from aos.contracts import REPO_ROOT, canonical
from aos.owned_episode_preparation import tokenizer_runner_pin
import test_owned_episode_preparation as preparation_helpers
import test_owned_episode_learning as store_helpers


class OwnedEpisodeTokenizerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    prepared = preparation_helpers.OwnedEpisodePreparationTests.prepared
    accept = store_helpers.OwnedEpisodeStoreTests.accept

    def setUp(self):
        store_helpers.OwnedEpisodeStoreTests.setUp(self)
        self.export_sha256 = self.prepared()
        preview = self.preparation.preview(self.episode_id, self.export_sha256)
        self.conversion_sha256 = preview['conversion_sha256']
        self.preparation.publish(self.episode_id, self.export_sha256, self.conversion_sha256)
        self.control = {'owner': 'AGENT', 'status': 'running', 'lease_id': 'synthetic-lease', 'generation': 1}
        self.learning.scheduler.controller = SimpleNamespace(state=lambda: self.control)
        self.learning.scheduler.reserved = False
        self.engine = SimpleNamespace(python=Path(sys.executable), manifest=self.root / 'unused',
                                      identity={'deployment_id': 'decider-' + 'e' * 64})
        self.preparation.native_inputs = Mock(return_value=(self.engine, self.root, {}, b'{}', b'{}'))
        self.probe = json.loads((REPO_ROOT / 'examples/dataset_tokenizer_probe.json').read_text())['report']
        self.probe.update(examples=1, variants=40)
        self.children = []

    async def asyncTearDown(self):
        await self.preparation.cancel()
        for process in self.children:
            self.assertIsNotNone(process.returncode)

    def launch(self, program):
        actual = asyncio.create_subprocess_exec

        async def spawning(*arguments, **keywords):
            process = await actual(sys.executable, '-c', program, **keywords)
            self.children.append(process)
            return process

        return patch('aos.owned_episode_preparation.asyncio.create_subprocess_exec', spawning)

    def start(self):
        return self.preparation.start(self.episode_id, self.conversion_sha256,
                                      self.conversion_sha256, self.control['lease_id'], self.control['generation'])

    def report_path(self):
        return self.store.root / self.episode_id / (self.conversion_sha256 + '-' + tokenizer_runner_pin() + '-tokenizer.json')

    async def test_fixture_worker_publication_is_bound_and_duplicate_start_rejected(self):
        program = 'import sys; sys.stdin.buffer.read(); print(' + repr(canonical(self.probe)) + ')'
        with self.launch(program), patch('aos.owned_episode_preparation.check_probe_pins'):
            self.assertEqual(self.start()['state'], 'pending')
            with self.assertRaises(ValueError):
                self.start()
            await self.preparation.task
        self.assertEqual(self.preparation.status()['state'], 'verified')
        self.assertTrue(self.report_path().is_file())

    async def test_cancellation_drains_live_child_and_never_publishes(self):
        with self.launch('import sys,time; sys.stdin.buffer.read(); time.sleep(60)'):
            self.start()
            for _attempt in range(100):
                if self.children:
                    break
                await asyncio.sleep(.01)
            self.assertTrue(self.children)
            await asyncio.wait_for(self.preparation.cancel(), 5)
        self.assertEqual(self.preparation.status()['state'], 'cancelled')
        self.assertFalse(self.report_path().exists())

    async def test_first_tick_dependency_failure_never_leaves_pending(self):
        with patch('aos.owned_episode_preparation.tokenizer_runner_pin', side_effect=OSError('synthetic-missing')):
            self.start()
            await self.preparation.task
        self.assertEqual(self.preparation.status()['state'], 'failed')
        self.assertFalse(self.children)

    async def test_changed_control_or_reserved_scheduler_rejects_late_result(self):
        for changed in ('lease', 'reserved'):
            with self.subTest(changed=changed), self.launch('import sys; sys.stdin.buffer.read(); print("{}")'):
                self.control['generation'] = 1
                self.learning.scheduler.reserved = False
                self.start()
                if changed == 'lease':
                    self.control['generation'] = 2
                else:
                    self.learning.scheduler.reserved = True
                await self.preparation.task
                self.assertEqual(self.preparation.status()['state'], 'failed')
                self.assertFalse(self.report_path().exists())

    async def test_excess_output_kills_only_owned_worker(self):
        with self.launch('import sys,time; sys.stdin.buffer.read(); sys.stdout.write("x"*100000); sys.stdout.flush(); time.sleep(60)'):
            self.start()
            await asyncio.wait_for(self.preparation.task, 5)
        self.assertEqual(self.preparation.status()['state'], 'failed')
        self.assertFalse(self.report_path().exists())
