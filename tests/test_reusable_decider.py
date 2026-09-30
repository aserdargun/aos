import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from aos.contracts import AOSFault, ErrorCode, Option, State, digest
from aos.reusable_decider import ReusableDeciderEngine


WORKER = '''import json, sys, time
mode = sys.argv[1]
for index, line in enumerate(sys.stdin):
    request = json.loads(line)
    if mode == 'timeout':
        time.sleep(30)
    if 'operation' in request:
        print(json.dumps({'request_id': request['request_id'], 'deployment_digest': DIGEST,
                          'prepared_cpu': True, 'cuda_initialized': mode == 'cuda_prepared', 'load_ms': 1.0}), flush=True)
        continue
    if mode == 'oversize':
        print('x' * 70000, flush=True)
        continue
    if mode == 'malformed':
        print('{not json', flush=True)
        continue
    if mode == 'partial':
        sys.stdout.write('{"request_id":')
        sys.stdout.flush()
        break
    if mode == 'eof':
        break
    options = request['request']['options']
    selected = options[index % len(options)]['id']
    result = {'request_id': request['request_id'], 'deployment_digest': DIGEST,
              'metrics': {'reused': index > 0},
              'prediction': {'selected_option': selected,
                             'probabilities': {item['id']: 1.0 if item['id'] == selected else 0.0 for item in options}}}
    if mode == 'wrong_id':
        result['request_id'] = '0' * 32
    if mode == 'wrong_deployment':
        result['deployment_digest'] = '0' * 64
    if mode == 'wrong_option':
        result['prediction']['selected_option'] = 'unknown'
    print(json.dumps(result), flush=True)
'''.replace('DIGEST', repr(digest({})))


class ReusableDeciderTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_cpu_preparation_then_fresh_decision(self):
        self.engine.prepare_in_background()
        await self.engine.preparation
        self.assertFalse(self.engine.preparation_metrics['cuda_initialized'])
        process = self.engine.process
        result = await self.engine.decide(self.state, self.options)
        self.assertIn(result.selected_option, {'write', 'ask'})
        self.assertIs(self.engine.process, process)
        self.assertEqual(len(self.processes), 1)
        await self.engine.close()
        self.assertIsNone(self.engine.preparation)

    async def test_gpu_preparation_response_fails_closed(self):
        self.mode = 'cuda_prepared'
        self.engine.prepare_in_background()
        with self.assertRaises(AOSFault):
            await self.engine.decide(self.state, self.options)
        self.assertIsNone(self.engine.process)

    async def test_cancel_while_waiting_for_cpu_preparation_reaps_process(self):
        self.mode = 'timeout'
        self.engine.prepare_in_background()
        pending = asyncio.create_task(self.engine.decide(self.state, self.options))
        while self.engine.process is None:
            await asyncio.sleep(.001)
        process = self.engine.process
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertIsNone(self.engine.process)
        self.assertIsNotNone(process.returncode)
        self.assertIsNone(self.engine.preparation)

    async def test_close_during_cpu_preparation_releases_resources(self):
        self.mode = 'timeout'
        self.engine.prepare_in_background()
        while self.engine.process is None:
            await asyncio.sleep(.001)
        process = self.engine.process
        await self.engine.close()
        self.assertIsNotNone(process.returncode)
        self.assertIsNone(self.engine.preparation)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        manifest = self.root / 'manifest.json'
        manifest.write_text('{}')
        self.worker = self.root / 'synthetic_worker.py'
        self.worker.write_text(WORKER)
        self.engine = ReusableDeciderEngine(manifest, Path(sys.executable), timeout=2)
        self.state = State(task_id='task', run_id='run', step_id='step', runtime_id='runtime',
                           deployment_id='fixture', owner_lease_id='lease')
        self.options = [Option(id='write', label='Write'), Option(id='ask', label='Ask')]
        self.processes = []
        self.mode = 'valid'
        self.original_spawn = asyncio.create_subprocess_exec

        async def spawn(*arguments, **keywords):
            self.assertEqual(arguments[-1], '--serve')
            process = await self.original_spawn(sys.executable, str(self.worker), self.mode, **keywords)
            self.processes.append(process)
            return process

        self.patcher = patch('aos.reusable_decider.asyncio.create_subprocess_exec', side_effect=spawn)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    async def asyncTearDown(self):
        await self.engine.close()
        for process in self.processes:
            self.assertIsNotNone(process.returncode)

    async def test_two_fresh_predictions_share_process_then_close(self):
        first = await self.engine.decide(self.state, self.options)
        self.assertFalse(self.engine.last_metrics['reused'])
        process = self.engine.process
        second = await self.engine.decide(self.state, self.options)
        self.assertTrue(self.engine.last_metrics['reused'])
        self.assertEqual((first.selected_option, second.selected_option), ('write', 'ask'))
        self.assertIs(self.engine.process, process)
        self.assertEqual(len(self.processes), 1)
        await self.engine.close()
        self.assertIsNone(self.engine.process)
        self.assertIsNotNone(process.returncode)
        await self.engine.decide(self.state, self.options)
        self.assertFalse(self.engine.last_metrics['reused'])
        self.assertEqual(len(self.processes), 2)

    async def test_bad_response_is_rejected_and_process_closed(self):
        expected = {'wrong_id': ErrorCode.INVALID_OUTPUT, 'wrong_deployment': ErrorCode.INVALID_OUTPUT,
                    'wrong_option': ErrorCode.INVALID_OUTPUT, 'oversize': ErrorCode.INVALID_OUTPUT,
                    'malformed': ErrorCode.INVALID_OUTPUT, 'partial': ErrorCode.INVALID_OUTPUT,
                    'eof': ErrorCode.MODEL_FAILURE}
        for mode, code in expected.items():
            with self.subTest(mode=mode):
                self.mode = mode
                with self.assertRaises(AOSFault) as caught:
                    await self.engine.decide(self.state, self.options)
                self.assertEqual(caught.exception.code, code)
                self.assertIsNone(self.engine.process)
                self.assertIsNotNone(self.processes[-1].returncode)

    async def test_timeout_closes_process_without_prediction(self):
        self.mode = 'timeout'
        self.engine.timeout = .1
        with self.assertRaises(AOSFault) as caught:
            await self.engine.decide(self.state, self.options)
        self.assertEqual(caught.exception.code, ErrorCode.TIMEOUT)
        self.assertIsNone(self.engine.process)

    async def test_cancellation_and_concurrent_request_are_safe(self):
        self.mode = 'timeout'
        pending = asyncio.create_task(self.engine.decide(self.state, self.options))
        while self.engine.process is None:
            await asyncio.sleep(.001)
        process = self.engine.process
        with self.assertRaises(AOSFault) as caught:
            await self.engine.decide(self.state, self.options)
        self.assertEqual(caught.exception.code, ErrorCode.UNSAFE_ACTION)
        self.assertIs(self.engine.process, process)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertIsNone(self.engine.process)
        self.assertIsNotNone(process.returncode)

    async def test_cancellation_during_spawn_drains_owned_child(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def delayed(*arguments, **keywords):
            process = await self.original_spawn(sys.executable, str(self.worker), 'timeout', **keywords)
            self.processes.append(process)
            entered.set()
            await release.wait()
            return process

        with patch('aos.reusable_decider.asyncio.create_subprocess_exec', side_effect=delayed):
            pending = asyncio.create_task(self.engine.decide(self.state, self.options))
            await entered.wait()
            pending.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await pending
        self.assertIsNone(self.engine.process)
        self.assertIsNotNone(self.processes[-1].returncode)

    async def test_oversized_request_does_not_spawn(self):
        with patch('aos.reusable_decider.decision_request', return_value={'state': 'x' * 70000}):
            with self.assertRaises(AOSFault):
                await self.engine.decide(self.state, self.options)
        self.assertEqual(self.processes, [])

    async def test_oversized_followup_releases_previous_worker(self):
        await self.engine.decide(self.state, self.options)
        process = self.engine.process
        with patch('aos.reusable_decider.decision_request', return_value={'state': 'x' * 70000}):
            with self.assertRaises(AOSFault):
                await self.engine.decide(self.state, self.options)
        self.assertIsNone(self.engine.process)
        self.assertIsNotNone(process.returncode)
