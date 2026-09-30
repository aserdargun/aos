import asyncio
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from aos.contracts import AOSFault, digest
from aos.desktop_tasks import DesktopScheduler
from aos.owned_episode_adaptation import ARTIFACT_BYTES, OwnedEpisodeAdaptation
import aos.owned_episode_adaptation as adaptation_module
import test_owned_episode_learning as store_helpers


def synthetic_example():
    return {'context': 'Synthetic owned form',
            'qs': [{'text': 'Choose an action', 'options': ['Ask a human', 'Save the record'], 'gold': 1}],
            'task': 'aos-owned-synthetic-form'}


class OwnedEpisodeAdaptationTests(unittest.TestCase):
    def setUp(self):
        fixture = store_helpers.OwnedEpisodeStoreTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.store = fixture.store
        self.episode_id = fixture.episode_id
        self.conversion_sha = 'a' * 64
        self.examples = [synthetic_example()]
        self.manifest = {'execution': {'execution_source_sha256': 'b' * 64,
                                      'candidate_execution_sha256': 'c' * 64},
                         'source_group_sha256': 'd' * 64,
                         'memberships': {'system1': [{'source_record_sha256': 'e' * 64}]}}
        self.engine = SimpleNamespace(
            python=Path(sys.executable), manifest=Path('/synthetic/decider-manifest.json'),
            identity={'deployment_id': 'decider-synthetic-pinned', 'kind': 'decider_native_worker'})
        self.pins = {'checkpoint_revision': '1' * 40, 'code_revision': '2' * 40,
                     'model_files': {'model.safetensors': 'f' * 64}}
        self.raw_manifest = b'{"synthetic":true}\n'
        self.preparation = SimpleNamespace(
            reserved=False,
            load=Mock(return_value=(self.manifest, {'system1': [{'payload': self.examples[0]}]})),
            inspect=Mock(return_value={'system1': {'tokenizer': 'verified'},
                                      'tokenizer_report_sha256': '3' * 64}),
            native_inputs=Mock(return_value=(self.engine, Path('/synthetic/source'), self.pins,
                                             self.raw_manifest, b'{}')))
        controller = SimpleNamespace(state=lambda: {
            'owner': 'AGENT', 'status': 'running', 'lease_id': 'lease-synthetic', 'generation': 4})
        scheduler = SimpleNamespace(closed=False, restart_quiesced=False, busy=False, paused=False,
                                    sequences=SimpleNamespace(reserved=False), planning_reserved=False,
                                    controller=controller)
        self.learning = SimpleNamespace(store=self.store, preparation=self.preparation, scheduler=scheduler)
        self.service = OwnedEpisodeAdaptation(self.learning)
        self.lease_id = 'lease-synthetic'
        self.generation = 4

    def start(self, preview, **permissions):
        return self.service.start(self.episode_id, self.conversion_sha,
            preview['authorization_sha256'], self.lease_id, self.generation,
            permissions.get('rights_redaction_reviewed', True),
            permissions.get('experimental_training_authorized', True))

    def synthetic_reports(self, authorization):
        checksum = digest(authorization)
        input_sha = digest(self.examples)
        manifest_sha = hashlib.sha256(self.raw_manifest).hexdigest()
        artifact = (b'AOSLORA1' + bytes.fromhex(checksum) + bytes.fromhex(input_sha)
                    + bytes.fromhex(manifest_sha) + struct.pack('<III', 4, 6144, 2048)
                    + b'\0' * (32768 * 4))
        artifact_sha = hashlib.sha256(artifact).hexdigest()
        common = {'checkpoint_revision': self.pins['checkpoint_revision'],
                  'code_revision': self.pins['code_revision'], 'weights_sha256': 'f' * 64,
                  'manifest_sha256': manifest_sha, 'input_sha256': input_sha,
                  'authorization_sha256': checksum, 'conversion_sha256': self.conversion_sha,
                  'development_decisions': len(self.examples), 'token_budget': 1536,
                  'base_parameters_unchanged': True, 'base_gradient_count': 0, 'training_ready': False}
        train = common | {'mode': 'owned_episode_adapter_train_v1', 'adapter_target': 'last_mlp_down_proj',
            'adapter_rank': 4, 'adapter_parameter_count': 32768, 'seed': 42,
            'optimizer_step_count': 1, 'learning_rate': 0.1, 'development_nll_before': 1.2,
            'development_nll_after': 1.1, 'gradient_norm': 0.5, 'peak_vram_allocated_bytes': 1024,
            'peak_vram_reserved_bytes': 2048, 'elapsed_seconds': 0.2,
            'adapter_parameters_changed': True, 'candidate_checkpoint_written': True,
            'candidate_artifact_sha256': artifact_sha, 'candidate_artifact_bytes': ARTIFACT_BYTES}
        replay = common | {'mode': 'owned_episode_adapter_replay_v1', 'artifact_sha256': artifact_sha,
            'base_development_nll': 1.2, 'candidate_development_nll': 1.1,
            'base_development_accuracy': 1.0, 'candidate_development_accuracy': 1.0,
            'candidate_loaded': True, 'evaluation': 'resubstitution'}
        return artifact, train, replay

    def test_preview_is_read_only_requires_two_true_consents_and_exact_hash(self):
        before = list((self.store.root / self.episode_id).iterdir())
        preview = self.service.preview(self.episode_id, self.conversion_sha,
                                       self.lease_id, self.generation)
        self.assertFalse(preview['persisted'])
        self.assertFalse(preview['training_started'])
        self.assertFalse(preview['authorization']['training_ready'])
        self.assertTrue(preview['authorization']['rights_redaction_reviewed'])
        self.assertTrue(preview['authorization']['experimental_training_authorized'])
        self.assertEqual(before, list((self.store.root / self.episode_id).iterdir()))
        with self.assertRaisesRegex(ValueError, 'separate_authorization'):
            self.service.start(self.episode_id, self.conversion_sha, preview['authorization_sha256'],
                self.lease_id, self.generation, True, False)
        with self.assertRaisesRegex(ValueError, 'exact_confirmation'):
            self.service.start(self.episode_id, self.conversion_sha, '0' * 64,
                self.lease_id, self.generation, True, True)
        self.learning.scheduler.busy = True
        with self.assertRaisesRegex(ValueError, 'control_changed'):
            self.service.preview(self.episode_id, self.conversion_sha,
                                 self.lease_id, self.generation)
        self.learning.scheduler.busy = False
        self.assertEqual(before, list((self.store.root / self.episode_id).iterdir()))

    def test_duplicate_authorization_is_not_started_twice(self):
        async def scenario():
            gate = asyncio.Event()
            entered = asyncio.Event()

            async def waiting_worker(*_args):
                entered.set()
                await gate.wait()

            self.service.worker = waiting_worker
            preview = self.service.preview(self.episode_id, self.conversion_sha,
                                           self.lease_id, self.generation)
            self.start(preview)
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                self.start(preview)
            self.assertEqual(self.service.attempts, 1)
            await self.service.cancel()

        asyncio.run(scenario())

    def test_synthetic_train_and_fresh_replay_publish_exact_pins(self):
        async def scenario():
            preview = self.service.preview(self.episode_id, self.conversion_sha,
                                           self.lease_id, self.generation)
            authorization = preview['authorization']
            artifact, train, replay = self.synthetic_reports(authorization)

            async def worker(script, _engine, output, _request):
                if script.endswith('_train.py'):
                    Path(output).write_bytes(artifact)
                    return train
                return replay

            self.service.worker = worker
            started = self.start(preview)
            await self.service.task
            self.assertEqual(started['state'], 'training')
            self.assertEqual(self.service.status()['state'], 'verified')
            report = self.service.inspect(self.episode_id, preview['authorization_sha256'])
            self.assertEqual(report['train'], train)
            self.assertEqual(report['replay'], replay)
            self.assertEqual(report['evaluation'], 'resubstitution')
            self.assertFalse(report['training_ready'])
            self.assertFalse(report['promotion_authorized'])
            self.assertEqual(self.service.attempts, 1)

        asyncio.run(scenario())

    def test_changed_source_or_revoked_tokenizer_fails_before_worker(self):
        async def scenario():
            preview = self.service.preview(self.episode_id, self.conversion_sha,
                                           self.lease_id, self.generation)
            worker = AsyncMock()
            self.service.worker = worker
            self.start(preview)
            self.manifest['execution']['execution_source_sha256'] = '9' * 64
            await self.service.task
            self.assertEqual(self.service.status()['state'], 'failed')
            worker.assert_not_awaited()
            with self.assertRaises(ValueError):
                self.service.inspect(self.episode_id, preview['authorization_sha256'])

        asyncio.run(scenario())

    def test_worker_pin_mismatch_artifact_tamper_and_first_tick_cancel_fail_closed(self):
        async def scenario():
            preview = self.service.preview(self.episode_id, self.conversion_sha,
                                           self.lease_id, self.generation)
            _artifact, train, _replay = self.synthetic_reports(preview['authorization'])

            async def wrong_worker(script, _engine, _output, _request):
                if script.endswith('_train.py'):
                    return train | {'input_sha256': '0' * 64}
                self.fail('replay must not follow mismatched train report')

            self.service.worker = wrong_worker
            self.start(preview)
            await self.service.task
            self.assertEqual(self.service.status()['state'], 'failed')

            preview = self.service.preview(self.episode_id, self.conversion_sha,
                                           self.lease_id, self.generation)
            artifact, train, replay = self.synthetic_reports(preview['authorization'])

            async def worker(script, _engine, output, _request):
                if script.endswith('_train.py'):
                    Path(output).write_bytes(artifact)
                    return train
                return replay

            self.service.worker = worker
            self.start(preview)
            await self.service.task
            artifact_path = (self.store.root / self.episode_id /
                             ('adapter-' + preview['authorization_sha256']) / 'candidate.bin')
            artifact_path.write_bytes(b'tampered')
            with self.assertRaisesRegex(ValueError, 'artifact_changed'):
                self.service.inspect(self.episode_id, preview['authorization_sha256'])

            preview = self.service.preview(self.episode_id, self.conversion_sha,
                                           self.lease_id, self.generation)
            worker = AsyncMock()
            self.service.worker = worker
            self.start(preview)
            await self.service.cancel()
            self.assertEqual(self.service.status()['state'], 'cancelled')
            worker.assert_not_awaited()

        asyncio.run(scenario())

    def test_worker_persisted_authorization_change_stops_before_replay_and_report(self):
        async def scenario():
            preview = self.service.preview(self.episode_id, self.conversion_sha,
                                           self.lease_id, self.generation)
            artifact, train, _replay = self.synthetic_reports(preview['authorization'])
            calls = []

            async def worker(script, _engine, output, _request):
                calls.append(script)
                if script.endswith('_train.py'):
                    Path(output).write_bytes(artifact)
                    with self.service.directory(self.episode_id,
                                                preview['authorization_sha256']) as (descriptor, _path):
                        changed = preview['authorization'] | {'attempt': 2}
                        self.store._put(descriptor, 'authorization.json', changed)
                    return train
                self.fail('replay must not run after authorization mutation')

            self.service.worker = worker
            self.start(preview)
            await self.service.task
            self.assertEqual(self.service.status()['state'], 'failed')
            self.assertEqual(calls, ['owned_episode_adapter_train.py'])
            directory = self.store.root / self.episode_id / ('adapter-' + preview['authorization_sha256'])
            self.assertFalse((directory / 'report.json').exists())

        asyncio.run(scenario())

    def test_worker_cancellation_kills_and_drains_real_child(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                script = root / 'services' / 'decider' / 'owned_episode_adapter_train.py'
                script.parent.mkdir(parents=True)
                artifact = root / 'candidate.bin'
                manifest = root / 'manifest.json'
                pid_path = Path(str(manifest) + '.pid')
                script.write_text('import os,sys,time\nfrom pathlib import Path\n'
                    'Path(sys.argv[1] + ".pid").write_text(str(os.getpid()))\n'
                    'time.sleep(30)\n')
                with unittest.mock.patch.object(adaptation_module, 'REPO_ROOT', root):
                    task = asyncio.create_task(self.service.worker('owned_episode_adapter_train.py',
                        SimpleNamespace(python=Path(sys.executable), manifest=manifest), artifact,
                        {'synthetic': True}))
                    for _ in range(200):
                        if pid_path.exists():
                            break
                        await asyncio.sleep(0.01)
                    self.assertTrue(pid_path.exists())
                    child_pid = int(pid_path.read_text())
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    with self.assertRaises(ProcessLookupError):
                        os.kill(child_pid, 0)

        asyncio.run(scenario())

    def test_cancellation_during_spawn_waits_for_child_then_kills_and_drains(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                script = root / 'services' / 'decider' / 'owned_episode_adapter_train.py'
                script.parent.mkdir(parents=True)
                manifest = root / 'manifest.json'
                pid_path = Path(str(manifest) + '.pid')
                artifact = root / 'candidate.bin'
                script.write_text('import time\ntime.sleep(30)\n')
                original = asyncio.create_subprocess_exec
                spawn_entered, permit_spawn = asyncio.Event(), asyncio.Event()
                spawned = []

                async def delayed_spawn(*args, **kwargs):
                    spawn_entered.set()
                    await permit_spawn.wait()
                    process = await original(*args, **kwargs)
                    spawned.append(process)
                    return process

                with unittest.mock.patch.object(adaptation_module, 'REPO_ROOT', root), \
                     unittest.mock.patch.object(adaptation_module.asyncio, 'create_subprocess_exec',
                                                side_effect=delayed_spawn):
                    task = asyncio.create_task(self.service.worker('owned_episode_adapter_train.py',
                        SimpleNamespace(python=Path(sys.executable), manifest=manifest), artifact,
                        {'synthetic': True}))
                    await asyncio.wait_for(spawn_entered.wait(), timeout=2)
                    task.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(pid_path.exists())
                    permit_spawn.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(task, timeout=5)
                    self.assertEqual(len(spawned), 1)
                    self.assertIsNotNone(spawned[0].returncode)
                    with self.assertRaises(ProcessLookupError):
                        os.kill(spawned[0].pid, 0)

        asyncio.run(scenario())

    def test_cancellation_after_train_prevents_replay_worker_and_report(self):
        async def scenario():
            preview = self.service.preview(self.episode_id, self.conversion_sha,
                                           self.lease_id, self.generation)
            artifact, train, _replay = self.synthetic_reports(preview['authorization'])
            calls = []
            attempts = 0
            original_check_attempt = self.service.check_attempt

            def cancel_after_train(descriptor, authorization):
                nonlocal attempts
                attempts += 1
                if attempts == 2:
                    raise asyncio.CancelledError
                return original_check_attempt(descriptor, authorization)

            async def worker(script, _engine, output, _request):
                calls.append(script)
                Path(output).write_bytes(artifact)
                return train

            self.service.check_attempt = cancel_after_train
            self.service.worker = worker
            self.start(preview)
            with self.assertRaises(asyncio.CancelledError):
                await self.service.task
            self.assertEqual(self.service.status()['state'], 'cancelled')
            self.assertEqual(calls, ['owned_episode_adapter_train.py'])
            directory = self.store.root / self.episode_id / ('adapter-' + preview['authorization_sha256'])
            self.assertFalse((directory / 'report.json').exists())

        asyncio.run(scenario())

    def test_generic_scheduler_start_is_blocked_by_adaptation_reservation(self):
        scheduler = DesktopScheduler.__new__(DesktopScheduler)
        scheduler.owned_skill_planning = SimpleNamespace(
            reserved=False, episode_learning=SimpleNamespace(adaptation=SimpleNamespace(reserved=True)))
        with self.assertRaises(AOSFault) as raised:
            scheduler._start(self.lease_id, self.generation, 'hello')
        self.assertIn('owns task admission', str(raised.exception))

    def test_worker_rejects_excess_output_and_reaps_real_child(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                script = root / 'services' / 'decider' / 'owned_episode_adapter_train.py'
                script.parent.mkdir(parents=True)
                script.write_text('print("x" * 70000)\n')
                with unittest.mock.patch.object(adaptation_module, 'REPO_ROOT', root):
                    with self.assertRaisesRegex(ValueError, 'output_bound'):
                        await self.service.worker('owned_episode_adapter_train.py',
                            SimpleNamespace(python=Path(sys.executable), manifest=root / 'manifest.json'),
                            root / 'candidate.bin', {'synthetic': True})

        asyncio.run(scenario())


if __name__ == '__main__':
    unittest.main()
