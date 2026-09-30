import asyncio
import json
from pathlib import Path
import signal
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aos.contracts import AOSFault, ErrorCode, Option, State, canonical, digest
from aos.decision import DeciderEngine, decision_request
from aos.reusable_decider import ReusableDeciderEngine


class DeciderDispatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.manifest = Path(temporary.name) / 'manifest.json'
        self.manifest.write_text('{}')
        self.state = State(task_id='task', run_id='run', step_id='step', runtime_id='runtime',
                           deployment_id='fixture', owner_lease_id='lease',
                           observation='Synthetic reviewed context: Taslağı kaydedin.')
        self.options = [Option(id='write', label='Write'), Option(id='ask', label='Ask')]
        self.prediction = {'selected_option': 'write', 'probabilities': {'write': 1.0, 'ask': 0.0}}
        self.response = {'deployment_digest': digest({}), 'metrics': {'input_tokens': 123},
                         'prediction': self.prediction}
        self.processes = []

    def process(self):
        process = SimpleNamespace(
            pid=123456 + len(self.processes), returncode=None,
            stdin=Mock(), stdout=SimpleNamespace(readline=AsyncMock()),
            wait=AsyncMock(return_value=0), communicate=AsyncMock(return_value=(b'', None)),
        )
        process.stdin.drain = AsyncMock()
        self.processes.append(process)
        return process

    def native(self, process):
        engine = DeciderEngine(self.manifest, Path('/synthetic/python'))

        async def communicate(payload):
            process.returncode = 0
            return json.dumps(self.response).encode(), None

        process.communicate.side_effect = communicate
        return engine

    def reusable(self, process):
        engine = ReusableDeciderEngine(self.manifest, Path('/synthetic/python'))

        async def readline():
            request = json.loads(process.stdin.write.call_args.args[0])
            if 'operation' in request:
                response = {'deployment_digest': digest({}), 'prepared_cpu': True,
                            'cuda_initialized': False, 'idle_prepared': True}
            else:
                response = dict(self.response)
            return (canonical({'request_id': request['request_id'], **response}) + '\n').encode()

        process.stdout.readline.side_effect = readline
        return engine

    async def test_native_revocation_during_spawn_prevents_communicate_and_reaps_child(self):
        process = self.process()
        engine = self.native(process)
        engine.last_request, engine.last_response = {'old': True}, {'old': True}
        entered, release = asyncio.Event(), asyncio.Event()
        admitted = True

        async def spawn(*arguments, **keywords):
            entered.set()
            await release.wait()
            return process

        def guard(request):
            self.assertEqual(request, decision_request(self.state, self.options))
            if not admitted:
                raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Synthetic source revoked during startup')

        engine.before_decision_dispatch = guard
        with patch('aos.decision.asyncio.create_subprocess_exec', side_effect=spawn), patch(
                'aos.decision.os.killpg') as kill:
            pending = asyncio.create_task(engine.decide(self.state, self.options))
            await entered.wait()
            admitted = False
            release.set()
            with self.assertRaises(AOSFault):
                await pending
            kill.assert_called_once_with(process.pid, signal.SIGKILL)
        process.communicate.assert_not_awaited()
        process.wait.assert_awaited_once()
        self.assertIsNone(engine.last_request)
        self.assertIsNone(engine.last_response)

    async def test_native_callback_errors_and_cancellation_write_no_input(self):
        for error in (ValueError('changed'), RuntimeError('guard failed'), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                process = self.process()
                engine = self.native(process)
                engine.before_decision_dispatch = Mock(side_effect=error)
                with patch('aos.decision.asyncio.create_subprocess_exec',
                           new_callable=AsyncMock, return_value=process), patch('aos.decision.os.killpg') as kill:
                    with self.assertRaises(type(error)):
                        await engine.decide(self.state, self.options)
                    kill.assert_called_once_with(process.pid, signal.SIGKILL)
                process.communicate.assert_not_awaited()
                process.wait.assert_awaited_once()
                self.assertIsNone(engine.last_request)
                self.assertIsNone(engine.last_response)

    async def test_native_captures_exact_input_without_guard_mutating_options(self):
        process = self.process()
        engine = self.native(process)
        identity = json.loads(canonical(engine.identity))
        expected = decision_request(self.state, self.options)

        def guard(request):
            self.assertEqual(request, expected)
            request['options'].clear()
            request['state'] = 'mutated callback argument'

        engine.before_decision_dispatch = Mock(side_effect=guard)
        with patch('aos.decision.asyncio.create_subprocess_exec',
                   new_callable=AsyncMock, return_value=process), patch('aos.decision.os.killpg'):
            actual = await engine.decide(self.state, self.options)
        engine.before_decision_dispatch.assert_called_once()
        self.assertEqual(json.loads(process.communicate.await_args.args[0]), expected)
        self.assertEqual(engine.last_request, expected)
        self.assertEqual(engine.last_response, self.prediction)
        self.assertEqual(actual.model_dump(mode='json'), self.prediction)
        self.assertEqual(engine.identity, identity)

    async def test_native_invalid_response_keeps_dispatch_but_clears_previous_prediction(self):
        process = self.process()
        engine = self.native(process)
        engine.last_response = {'previous': True}
        self.response['prediction'] = {**self.prediction, 'selected_option': 'unknown'}
        with patch('aos.decision.asyncio.create_subprocess_exec',
                   new_callable=AsyncMock, return_value=process), patch('aos.decision.os.killpg') as kill:
            with self.assertRaises(AOSFault):
                await engine.decide(self.state, self.options)
            kill.assert_not_called()
        self.assertEqual(engine.last_request, decision_request(self.state, self.options))
        self.assertIsNone(engine.last_response)
        process.wait.assert_awaited_once()

    async def test_reusable_revocation_during_spawn_prevents_stdin_write_and_drains_child(self):
        process = self.process()
        engine = self.reusable(process)
        engine.last_request, engine.last_response = {'old': True}, {'old': True}
        entered, release = asyncio.Event(), asyncio.Event()
        admitted = True

        async def spawn(*arguments, **keywords):
            entered.set()
            await release.wait()
            return process

        def guard(request):
            self.assertEqual(request, decision_request(self.state, self.options))
            if not admitted:
                raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Synthetic source revoked during startup')

        engine.before_decision_dispatch = guard
        with patch('aos.reusable_decider.asyncio.create_subprocess_exec', side_effect=spawn), patch(
                'aos.reusable_decider.os.killpg') as kill:
            pending = asyncio.create_task(engine.decide(self.state, self.options))
            await entered.wait()
            admitted = False
            release.set()
            with self.assertRaises(AOSFault):
                await pending
            kill.assert_called_once_with(process.pid, signal.SIGKILL)
        process.stdin.write.assert_not_called()
        process.stdin.close.assert_called_once()
        process.communicate.assert_awaited_once_with()
        self.assertIsNone(engine.process)
        self.assertIsNone(engine.last_request)
        self.assertIsNone(engine.last_response)

    async def test_reusable_callback_errors_and_cancellation_write_no_input(self):
        for error in (ValueError('changed'), RuntimeError('guard failed'), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                process = self.process()
                engine = self.reusable(process)
                engine.before_decision_dispatch = Mock(side_effect=error)
                expected_exception = AOSFault if isinstance(error, ValueError) else type(error)
                with patch('aos.reusable_decider.asyncio.create_subprocess_exec',
                           new_callable=AsyncMock, return_value=process), patch(
                               'aos.reusable_decider.os.killpg') as kill:
                    with self.assertRaises(expected_exception):
                        await engine.decide(self.state, self.options)
                    kill.assert_called_once_with(process.pid, signal.SIGKILL)
                process.stdin.write.assert_not_called()
                process.communicate.assert_awaited_once_with()
                self.assertIsNone(engine.process)
                self.assertIsNone(engine.last_request)
                self.assertIsNone(engine.last_response)

    async def test_reusable_captures_inference_request_and_ignores_idle_controls(self):
        process = self.process()
        engine = self.reusable(process)
        expected = decision_request(self.state, self.options)
        identity = json.loads(canonical(engine.identity))

        def guard(request):
            self.assertEqual(request, expected)
            request['options'].clear()

        engine.before_decision_dispatch = Mock(side_effect=guard)
        with patch('aos.reusable_decider.asyncio.create_subprocess_exec',
                   new_callable=AsyncMock, return_value=process), patch('aos.reusable_decider.os.killpg'):
            try:
                result = await engine.decide(self.state, self.options)
                dispatched = json.loads(process.stdin.write.call_args.args[0])
                self.assertEqual(set(dispatched), {'request_id', 'request'})
                self.assertEqual(dispatched['request'], expected)
                self.assertEqual(engine.last_request, expected)
                self.assertEqual(engine.last_response, self.prediction)
                self.assertEqual(result.model_dump(mode='json'), self.prediction)
                await engine.prepare_cpu(idle=True)
                engine.before_decision_dispatch.assert_called_once()
                self.assertEqual(engine.last_request, expected)
                self.assertEqual(engine.last_response, self.prediction)
                self.assertEqual(engine.identity, identity)
            finally:
                await engine.close()

    async def test_reusable_checks_guard_after_background_preparation_await(self):
        process = self.process()
        engine = self.reusable(process)
        engine.process = process
        entered, release = asyncio.Event(), asyncio.Event()
        admitted = True

        async def prepare():
            entered.set()
            await release.wait()

        def guard(request):
            if not admitted:
                raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Synthetic control changed during preparation')

        engine.before_decision_dispatch = guard
        engine.preparation = asyncio.create_task(prepare())
        with patch('aos.reusable_decider.os.killpg') as kill:
            pending = asyncio.create_task(engine.decide(self.state, self.options))
            await entered.wait()
            admitted = False
            release.set()
            with self.assertRaises(AOSFault):
                await pending
            kill.assert_called_once_with(process.pid, signal.SIGKILL)
        process.stdin.write.assert_not_called()
        self.assertIsNone(engine.process)
        self.assertIsNone(engine.preparation)

    async def test_reusable_restored_hook_and_fresh_job_cannot_reuse_previous_context(self):
        process = self.process()
        engine = self.reusable(process)
        original_hook = Mock()
        job_hook = Mock()
        engine.before_decision_dispatch = original_hook
        with patch('aos.reusable_decider.asyncio.create_subprocess_exec',
                   new_callable=AsyncMock, return_value=process), patch('aos.reusable_decider.os.killpg'):
            try:
                with patch.object(engine, 'before_decision_dispatch', job_hook):
                    await engine.decide(self.state, self.options)
                fresh_state = self.state.model_copy(update={'observation': 'Fresh synthetic observation'})
                self.assertIs(engine.before_decision_dispatch, original_hook)
                await engine.decide(fresh_state, self.options)
                job_hook.assert_called_once_with(decision_request(self.state, self.options))
                original_hook.assert_called_once_with(decision_request(fresh_state, self.options))
                self.assertEqual(engine.last_request, decision_request(fresh_state, self.options))
                self.assertNotIn('Taslağı', engine.last_request['state'])
                self.assertEqual(engine.last_response, self.prediction)
            finally:
                await engine.close()

    async def test_reusable_invalid_response_never_becomes_captured_prediction(self):
        process = self.process()
        engine = self.reusable(process)
        engine.last_response = {'previous': True}
        self.response['deployment_digest'] = '0' * 64
        with patch('aos.reusable_decider.asyncio.create_subprocess_exec',
                   new_callable=AsyncMock, return_value=process), patch('aos.reusable_decider.os.killpg'):
            with self.assertRaises(AOSFault):
                await engine.decide(self.state, self.options)
        self.assertEqual(engine.last_request, decision_request(self.state, self.options))
        self.assertIsNone(engine.last_response)
        self.assertIsNone(engine.process)


if __name__ == '__main__':
    unittest.main()
