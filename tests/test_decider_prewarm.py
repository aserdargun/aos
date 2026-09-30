import asyncio
import json
import math
import os
from pathlib import Path
import signal
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aos.contracts import AOSFault, Option, REPO_ROOT, State, digest
from aos.reusable_decider import ReusableDeciderEngine
from aos.vision import FixtureVisionSupervisor
import test_decider_worker as worker_fixtures
import test_desktop_tasks as desktop_fixtures


WORKER = '''import json, sys, time
mode, log = sys.argv[1:]
for line in sys.stdin:
    request = json.loads(line)
    with open(log, 'a') as stream:
        stream.write(json.dumps(request) + '\\n')
    operation = request.get('operation')
    if mode == 'timeout' or mode == 'admission_timeout' and operation == 'admit_job_cpu':
        time.sleep(30)
    result = {'request_id': request['request_id'], 'deployment_digest': DIGEST}
    if operation == 'admit_job_gpu':
        result.update(job_admitted=True, cuda_initialized=True, gpu_resident=True, pin_verify_ms=1.0)
        if mode == 'wrong_gpu_admission':
            result['deployment_digest'] = '0' * 64
    elif operation == 'prepare_idle_gpu':
        result.update(idle_prepared=True, cuda_initialized=True, job_released=True)
    elif operation == 'admit_job_cpu':
        result.update(job_admitted=True, cuda_initialized=mode == 'cuda_admission', pin_verify_ms=1.0)
        if mode == 'wrong_admission':
            result['deployment_digest'] = '0' * 64
    elif operation:
        result.update(prepared_cpu=True, idle_prepared=operation == 'prepare_idle_cpu',
                      cuda_initialized=mode == 'cuda_prepared', load_ms=1.0)
    else:
        options = request['request']['options']
        result.update(prediction={'selected_option': options[0]['id'],
                                  'probabilities': {item['id']: float(position == 0) for position, item in enumerate(options)}},
                      metrics={'reused': False})
    print(json.dumps(result), flush=True)
'''.replace('DIGEST', repr(digest({})))


class DeciderPrewarmWorkerTests(unittest.TestCase):
    model_modules = worker_fixtures.DeciderWorkerTests.model_modules

    def test_cli_prewarm_requires_explicit_real_reuse_before_runtime_or_model_start(self):
        for arguments in (['--prewarm-decider'], ['--engine', 'fixture', '--prewarm-decider'],
                          ['--engine', 'decider', '--prewarm-decider']):
            result = subprocess.run([sys.executable, str(REPO_ROOT / 'scripts/serve_desktop.py'), *arguments],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 2)
            self.assertIn('CPU prewarm requires --reuse-decider', result.stderr)
            self.assertNotIn('Local token file', result.stdout)
        for arguments in (['--prewarm-idle-seconds', '300'],
                          ['--engine', 'decider', '--reuse-decider', '--prewarm-idle-seconds', '300'],
                          ['--engine', 'decider', '--reuse-decider', '--prewarm-decider', '--prewarm-idle-seconds', '601'],
                          ['--engine', 'decider', '--reuse-decider', '--prewarm-decider', '--prewarm-idle-seconds', '0']):
            result = subprocess.run([sys.executable, str(REPO_ROOT / 'scripts/serve_desktop.py'), *arguments],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 2)
            self.assertIn('CPU idle lifetime requires --prewarm-decider and 1–600 seconds', result.stderr)
            self.assertNotIn('Local token file', result.stdout)
        for arguments in (['--gpu-idle-seconds', '30'],
                          ['--engine', 'decider', '--reuse-decider', '--gpu-idle-seconds', '30'],
                          ['--engine', 'decider', '--reuse-decider', '--prewarm-decider', '--gpu-idle-seconds', '0'],
                          ['--engine', 'decider', '--reuse-decider', '--prewarm-decider', '--gpu-idle-seconds', '121']):
            result = subprocess.run([sys.executable, str(REPO_ROOT / 'scripts/serve_desktop.py'), *arguments],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 2)
            self.assertIn('GPU idle lifetime requires --prewarm-decider and 1–120 seconds', result.stderr)
            self.assertNotIn('Local token file', result.stdout)

    def session(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.manifest = Path(temporary.name) / 'manifest.json'
        self.manifest.write_text('{}')
        session = worker_fixtures.WORKER['ModelSession'](Path('/synthetic/model'), {})
        session.manifest = self.manifest
        return session

    def test_idle_model_cannot_infer_until_full_fresh_manifest_environment_revalidation(self):
        modules, model, infer, prompt = self.model_modules()
        modules['torch'].cuda.is_initialized.return_value = False
        session = self.session()
        verify = Mock(return_value=Path('/synthetic/model'))
        with patch.dict('sys.modules', modules), patch.dict(session.admit_job_cpu.__globals__, verify_environment=verify):
            prepared = session.prepare_cpu(idle=True)
            self.assertTrue(prepared['idle_prepared'])
            self.assertFalse(prepared['cuda_initialized'])
            with self.assertRaisesRegex(ValueError, 'fresh job admission'):
                session.infer(worker_fixtures.REQUEST)
            modules['torch'].cuda.is_available.assert_not_called()
            model.m.to.assert_not_called()
            admitted = session.admit_job_cpu()
            self.assertTrue(admitted['job_admitted'])
            self.assertFalse(admitted['cuda_initialized'])
            verify.assert_called_once_with({})
            self.assertFalse(session.idle_prepared)
            session.infer(worker_fixtures.REQUEST)
            self.assertEqual(model.decide.call_count, 1)
            model.m.to.assert_called_once_with('cuda')
            with self.assertRaises(ValueError):
                session.admit_job_cpu()

    def test_changed_manifest_checksum_environment_or_cuda_fail_before_forward(self):
        for failure in ('manifest', 'artifact_checksum', 'dependencies', 'root', 'cuda'):
            with self.subTest(failure=failure):
                modules, model, infer, prompt = self.model_modules()
                modules['torch'].cuda.is_initialized.return_value = False
                session = self.session()
                verify = Mock(return_value=Path('/synthetic/model'))
                with patch.dict('sys.modules', modules), patch.dict(session.admit_job_cpu.__globals__, verify_environment=verify):
                    session.prepare_cpu(idle=True)
                    if failure == 'manifest':
                        self.manifest.write_text('{"changed":true}')
                    elif failure in ('artifact_checksum', 'dependencies'):
                        verify.side_effect = ValueError(failure)
                    elif failure == 'root':
                        verify.return_value = Path('/other/model')
                    else:
                        modules['torch'].cuda.is_initialized.return_value = True
                    with self.assertRaises(ValueError):
                        session.admit_job_cpu()
                    self.assertTrue(session.idle_prepared)
                    model.m.to.assert_not_called()
                    model.decide.assert_not_called()


class DeciderPrewarmTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manifest = self.root / 'manifest.json'
        self.manifest.write_text('{}')
        self.worker = self.root / 'worker.py'
        self.worker.write_text(WORKER)
        self.log = self.root / 'requests.jsonl'
        self.engine = ReusableDeciderEngine(self.manifest, Path(sys.executable), timeout=2, cpu_prewarm=True)
        self.state = State(task_id='task', run_id='run', step_id='step', runtime_id='runtime',
                           deployment_id='fixture', owner_lease_id='lease')
        self.options = [Option(id='write', label='Write'), Option(id='ask', label='Ask')]
        self.mode = 'valid'
        self.processes = []
        original = asyncio.create_subprocess_exec

        async def spawn(*arguments, **keywords):
            self.assertEqual(arguments[-1], '--serve')
            if self.engine.cpu_prewarm:
                self.assertEqual(arguments[-2],
                                 f'--idle-seconds={math.ceil(max(75, self.engine.idle_seconds + 30, self.engine.gpu_idle_seconds + 30))}')
            else:
                self.assertEqual(arguments[-2], str(self.manifest))
            process = await original(sys.executable, str(self.worker), self.mode, str(self.log), **keywords)
            self.processes.append(process)
            return process

        patcher = patch('aos.reusable_decider.asyncio.create_subprocess_exec', side_effect=spawn)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        await self.engine.close()
        self.assertTrue(all(process.returncode is not None for process in self.processes))

    async def wait_status(self, state):
        async with asyncio.timeout(3):
            while self.engine.prewarm_status()['state'] != state:
                await asyncio.sleep(.001)

    def operations(self):
        return [json.loads(line).get('operation', 'infer') for line in self.log.read_text().splitlines()]

    def test_idle_deadline_and_optin_are_strictly_bounded(self):
        for seconds in (0, 601, True, '600', float('nan')):
            with self.assertRaises(ValueError):
                ReusableDeciderEngine(self.manifest, Path(sys.executable), cpu_prewarm=True, idle_seconds=seconds)
        self.assertEqual(ReusableDeciderEngine(
            self.manifest, Path(sys.executable), cpu_prewarm=True, idle_seconds=300).idle_seconds, 300)
        with self.assertRaises(ValueError):
            ReusableDeciderEngine(self.manifest, Path(sys.executable), cpu_prewarm='true')
        for seconds in (-1, 121, True, '30', float('nan')):
            with self.assertRaises(ValueError):
                ReusableDeciderEngine(self.manifest, Path(sys.executable), cpu_prewarm=True,
                                      gpu_idle_seconds=seconds)
        with self.assertRaises(ValueError):
            ReusableDeciderEngine(self.manifest, Path(sys.executable), gpu_idle_seconds=30)

    async def test_worker_deadline_outlives_maximum_gpu_idle_retention(self):
        self.engine.gpu_idle_seconds = 120
        self.engine.prewarm_idle()
        await self.wait_status('ready')
        self.assertIsNone(self.engine.process.returncode)

    async def test_gpu_idle_reuses_one_worker_only_after_fresh_admission(self):
        self.engine.gpu_idle_seconds = .1
        self.engine.prewarm_idle()
        await self.wait_status('ready')
        self.engine.begin_job()
        await self.engine.decide(self.state, self.options)
        process = self.engine.process
        await self.engine.park_gpu()
        self.assertEqual(self.engine.prewarm_status()['state'], 'ready_gpu')
        self.engine.begin_job()
        await self.engine.preparation
        result = await self.engine.decide(self.state, self.options)
        self.assertEqual(result.selected_option, 'write')
        self.assertIs(self.engine.process, process)
        self.assertEqual(self.operations(), ['prepare_idle_cpu', 'admit_job_cpu', 'infer',
                                             'prepare_idle_gpu', 'admit_job_gpu', 'infer'])

    async def test_gpu_idle_expiry_reaps_worker(self):
        self.engine.gpu_idle_seconds = .02
        self.engine.prewarm_idle()
        await self.wait_status('ready')
        self.engine.begin_job()
        await self.engine.decide(self.state, self.options)
        process = self.engine.process
        await self.engine.park_gpu()
        await self.wait_status('inactive')
        async with asyncio.timeout(2):
            while process.returncode is None:
                await asyncio.sleep(.001)
        self.assertFalse(self.engine.gpu_idle)

    async def test_wrong_gpu_admission_stops_before_next_decision(self):
        self.mode = 'wrong_gpu_admission'
        self.engine.gpu_idle_seconds = .1
        self.engine.prewarm_idle()
        await self.wait_status('ready')
        self.engine.begin_job()
        await self.engine.decide(self.state, self.options)
        await self.engine.park_gpu()
        self.engine.begin_job()
        with self.assertRaises(AOSFault):
            await self.engine.decide(self.state, self.options)
        self.assertIsNone(self.engine.process)

    async def test_public_readiness_requires_ack_then_job_revalidates_once_and_decisions_are_fresh(self):
        self.assertEqual(self.engine.prewarm_status(), {'enabled': True, 'state': 'inactive'})
        self.engine.prewarm_idle()
        self.assertEqual(self.engine.prewarm_status()['state'], 'preparing')
        await self.wait_status('ready')
        process = self.engine.process
        self.assertEqual(self.operations(), ['prepare_idle_cpu'])
        self.engine.begin_job()
        self.assertEqual(self.engine.prewarm_status()['state'], 'inactive')
        await self.engine.preparation
        self.assertTrue(self.engine.preparation_metrics['job_admitted'])
        first = await self.engine.decide(self.state, self.options)
        second = await self.engine.decide(self.state.model_copy(update={'observation': 'Changed observation'}),
                                           [Option(id='save', label='Save'), Option(id='ask', label='Ask')])
        self.assertEqual((first.selected_option, second.selected_option), ('write', 'save'))
        self.assertIs(self.engine.process, process)
        self.assertEqual(self.operations(), ['prepare_idle_cpu', 'admit_job_cpu', 'infer', 'infer'])
        self.assertEqual(len(self.processes), 1)

    async def test_worker_exit_after_idle_ack_uses_fresh_cpu_preparation(self):
        self.engine.prewarm_idle()
        await self.wait_status('ready')
        old_process = self.engine.process
        os.killpg(old_process.pid, signal.SIGKILL)
        await old_process.wait()
        self.assertEqual(self.engine.prewarm_status()['state'], 'inactive')
        self.engine.begin_job()
        await self.engine.preparation
        result = await self.engine.decide(self.state, self.options)
        self.assertEqual(result.selected_option, 'write')
        self.assertEqual(self.operations(), ['prepare_idle_cpu', 'prepare_cpu', 'infer'])
        self.assertEqual(len(self.processes), 2)

    async def test_idle_expiry_reaps_without_automatic_respawn_and_cold_job_remains_available(self):
        self.engine.idle_seconds = .02
        self.engine.prewarm_idle()
        await self.wait_status('ready')
        process = self.engine.process
        await self.wait_status('inactive')
        async with asyncio.timeout(2):
            while process.returncode is None:
                await asyncio.sleep(.001)
        await asyncio.sleep(.03)
        self.assertEqual(len(self.processes), 1)
        self.engine.begin_job()
        await self.engine.decide(self.state, self.options)
        self.assertEqual(self.operations(), ['prepare_idle_cpu', 'prepare_cpu', 'infer'])
        self.assertEqual(len(self.processes), 2)

    async def test_duplicate_warm_or_job_claim_is_rejected(self):
        self.engine.prewarm_idle()
        with self.assertRaises(AOSFault):
            self.engine.prewarm_idle()
        self.engine.begin_job()
        with self.assertRaises(AOSFault):
            self.engine.begin_job()
        await self.engine.preparation
        with self.assertRaises(AOSFault):
            self.engine.prewarm_idle()

    async def test_expiry_cleanup_finishes_before_new_job_worker_without_clobbering_claim(self):
        self.engine.idle_seconds = .01
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.engine.stop_process
        async def delayed():
            await original()
            entered.set()
            await release.wait()
        self.engine.prewarm_idle()
        await self.wait_status('ready')
        with patch.object(self.engine, 'stop_process', side_effect=delayed):
            await asyncio.wait_for(entered.wait(), 2)
            self.engine.begin_job()
            await asyncio.sleep(.01)
            self.assertTrue(self.engine.job_active)
            self.assertEqual(len(self.processes), 1)
            self.assertEqual(self.operations(), ['prepare_idle_cpu'])
            release.set()
            await self.engine.preparation
        self.assertTrue(self.engine.job_active)
        self.assertEqual(len(self.processes), 2)
        await self.engine.decide(self.state, self.options)
        self.assertEqual(self.operations(), ['prepare_idle_cpu', 'prepare_cpu', 'infer'])

    async def test_close_immediately_after_claim_cancels_old_and_new_preparation(self):
        self.mode = 'timeout'
        self.engine.prewarm_idle()
        async with asyncio.timeout(2):
            while self.engine.process is None:
                await asyncio.sleep(.001)
        previous = self.engine.preparation
        self.engine.begin_job()
        current = self.engine.preparation
        await self.engine.close()
        self.assertTrue(previous.done())
        self.assertTrue(current.done())
        self.assertIsNone(self.engine.process)
        self.assertEqual(self.engine.prewarm_status()['state'], 'inactive')

    async def test_expiry_is_consumed_for_admitted_job_and_failure_never_restarts_idle(self):
        self.engine.idle_seconds = .01
        self.engine.prewarm_idle()
        await self.wait_status('ready')
        self.engine.begin_job()
        await self.engine.preparation
        await asyncio.sleep(.03)
        self.assertIsNotNone(self.engine.process)
        await self.engine.decide(self.state, self.options)
        await self.engine.close()
        self.assertEqual(self.engine.prewarm_status()['state'], 'inactive')
        self.mode = 'cuda_prepared'
        self.engine.prewarm_idle()
        await self.wait_status('inactive')
        self.assertIsNone(self.engine.process)
        self.assertEqual(self.operations().count('infer'), 1)

    async def test_invalid_or_cancelled_job_admission_closes_without_prediction(self):
        for mode in ('wrong_admission', 'cuda_admission', 'admission_timeout'):
            with self.subTest(mode=mode):
                self.mode = mode
                self.engine.prewarm_idle()
                await self.wait_status('ready')
                self.engine.begin_job()
                if mode == 'admission_timeout':
                    request = asyncio.create_task(self.engine.decide(self.state, self.options))
                    async with asyncio.timeout(2):
                        while self.operations()[-1] != 'admit_job_cpu':
                            await asyncio.sleep(.001)
                    request.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await request
                else:
                    with self.assertRaises(AOSFault):
                        await self.engine.decide(self.state, self.options)
                self.assertIsNone(self.engine.process)
                self.assertEqual(self.engine.prewarm_status()['state'], 'inactive')
                self.assertNotIn('infer', self.operations())

    async def test_default_reuse_does_not_idle_prepare_or_change_one_job_behavior(self):
        self.engine.cpu_prewarm = False
        self.engine.prewarm_idle()
        self.engine.begin_job()
        self.assertEqual(self.engine.prewarm_status(), {'enabled': False, 'state': 'inactive'})
        self.assertIsNone(self.engine.process)
        await self.engine.decide(self.state, self.options)
        self.assertEqual(self.operations(), ['infer'])


class SchedulerPrewarmTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = desktop_fixtures.DesktopTaskTests.asyncSetUp
    asyncTearDown = desktop_fixtures.DesktopTaskTests.asyncTearDown

    def prepared_engine(self):
        manifest = self.root / 'manifest.json'
        manifest.write_text('{}')
        engine = ReusableDeciderEngine(manifest, Path(sys.executable), cpu_prewarm=True)
        engine.identity = self.scheduler.engine.identity
        engine.prewarm_idle = Mock()
        engine.begin_job = Mock()
        self.scheduler.engine = engine
        return engine

    async def test_success_only_rewarm_follows_gpu_worker_close_and_retains_manual_approvals(self):
        engine = self.prepared_engine()
        calls = []
        original_close = engine.close
        async def closed():
            await original_close()
            calls.append('closed')
        engine.close = closed
        engine.prewarm_idle.side_effect = lambda: calls.append('warm')
        engine.decide = desktop_fixtures.FixtureDecisionEngine().decide
        state = self.controller.state()
        self.scheduler.start(state['lease_id'], state['generation'])
        pending = await desktop_fixtures.DesktopTaskTests.approval(self)
        self.assertFalse((self.runtime.root / 'hello.txt').exists())
        self.scheduler.respond(pending['approval_id'], pending['action_sha256'], True)
        await self.scheduler.task
        self.assertEqual(calls, ['closed', 'warm'])
        engine.begin_job.assert_called_once()
        self.assertEqual(self.scheduler.status()['decider_preparation'], {'enabled': True, 'state': 'inactive'})
        calls.clear()
        self.scheduler.start(state['lease_id'], state['generation'])
        pending = await desktop_fixtures.DesktopTaskTests.approval(self)
        self.scheduler.respond(pending['approval_id'], pending['action_sha256'], False)
        await asyncio.gather(self.scheduler.task, return_exceptions=True)
        self.assertEqual(calls, ['closed'])

    async def test_success_parks_gpu_but_denied_task_closes_it(self):
        engine = self.prepared_engine()
        engine.gpu_idle_seconds = 30
        engine.park_gpu = AsyncMock(side_effect=lambda: setattr(engine, 'gpu_idle', True))
        engine.close = AsyncMock()
        engine.decide = desktop_fixtures.FixtureDecisionEngine().decide
        state = self.controller.state()
        self.scheduler.start(state['lease_id'], state['generation'])
        approval = await desktop_fixtures.DesktopTaskTests.approval(self)
        self.scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
        await self.scheduler.task
        engine.park_gpu.assert_awaited_once()
        engine.close.assert_not_awaited()
        engine.park_gpu.reset_mock()
        self.scheduler.start(state['lease_id'], state['generation'])
        approval = await desktop_fixtures.DesktopTaskTests.approval(self)
        self.scheduler.respond(approval['approval_id'], approval['action_sha256'], False)
        await asyncio.gather(self.scheduler.task, return_exceptions=True)
        engine.park_gpu.assert_not_awaited()
        engine.close.assert_awaited_once()

    async def test_vision_preempts_gpu_idle_before_runtime_start(self):
        engine = self.prepared_engine()
        engine.gpu_idle = True
        engine.gpu_idle_seconds = 30
        calls = []

        async def closed():
            calls.append('close')
            engine.gpu_idle = False

        engine.close = AsyncMock(side_effect=closed)
        engine.begin_job = Mock(side_effect=lambda: calls.append('begin_job'))
        self.scheduler.browser_manifest = REPO_ROOT / 'models/browser-manifest.json'
        self.scheduler.vision_supervisor = FixtureVisionSupervisor()

        def failed_start():
            calls.append('vision_start')
            raise RuntimeError('synthetic startup failure')

        with patch('aos.desktop_tasks.VisionRuntime') as runtime_class:
            runtime_class.return_value.start.side_effect = failed_start
            state = self.controller.state()
            self.scheduler.start(state['lease_id'], state['generation'], kind='vision_canvas')
            await self.scheduler.task
        self.assertEqual(calls[:3], ['close', 'begin_job', 'vision_start'])
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'failed')

    async def test_idle_pause_cancel_and_shutdown_close_even_without_job_or_on_audit_failure(self):
        engine = self.prepared_engine()
        engine.close = unittest.mock.AsyncMock()
        await self.scheduler.pause()
        self.assertEqual(engine.close.await_count, 1)
        await self.scheduler.cancel('take_control')
        self.assertEqual(engine.close.await_count, 2)
        with patch.object(self.scheduler, 'clear_grant', side_effect=OSError('synthetic audit error')):
            with self.assertRaises(OSError):
                await self.scheduler.pause()
            self.assertEqual(engine.close.await_count, 3)
            with self.assertRaises(OSError):
                await self.scheduler.cancel('stop')
            self.assertEqual(engine.close.await_count, 4)
        await self.scheduler.close()
        self.assertEqual(engine.close.await_count, 5)
        engine.prewarm_idle.assert_not_called()
