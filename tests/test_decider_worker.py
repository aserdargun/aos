import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from aos.contracts import REPO_ROOT, digest


WORKER = runpy.run_path(str(REPO_ROOT / 'services/decider/worker.py'))
REQUEST = {'state': 'Synthetic state', 'question': 'What is the next action?',
           'options': [{'id': 'write', 'label': 'Write'}, {'id': 'ask', 'label': 'Ask'}]}


class DeciderWorkerTests(unittest.TestCase):
    def test_cpu_preparation_never_infers_or_initializes_cuda_then_transfers_once(self):
        modules, model, infer, prompt = self.model_modules()
        modules['torch'].cuda.is_initialized.return_value = False
        session = WORKER['ModelSession'](Path('/synthetic/model'), {})
        with patch.dict('sys.modules', modules):
            prepared = session.prepare_cpu()
            self.assertTrue(prepared['prepared_cpu'])
            self.assertFalse(prepared['cuda_initialized'])
            model.decide.assert_not_called()
            modules['torch'].cuda.synchronize.assert_not_called()
            first = session.infer(REQUEST)
            second = session.infer(REQUEST)
        infer.Decider.assert_called_once_with('/synthetic/model', device='cpu', use_graphs=False)
        model.m.to.assert_called_once_with('cuda')
        self.assertEqual(model.dev, 'cuda')
        self.assertTrue(first['metrics']['prepared_cpu'])
        self.assertFalse(first['metrics']['reused'])
        self.assertFalse(second['metrics']['prepared_cpu'])
        self.assertTrue(second['metrics']['reused'])
        self.assertEqual(model.decide.call_count, 2)

    def test_cpu_preparation_rejects_existing_cuda_context_or_second_preparation(self):
        modules, model, infer, prompt = self.model_modules()
        with patch.dict('sys.modules', modules):
            modules['torch'].cuda.is_initialized.return_value = True
            with self.assertRaises(ValueError):
                WORKER['ModelSession'](Path('/synthetic/model'), {}).prepare_cpu()
            infer.Decider.assert_not_called()
            modules['torch'].cuda.is_initialized.side_effect = [False, True]
            with self.assertRaises(ValueError):
                WORKER['ModelSession'](Path('/synthetic/model'), {}).prepare_cpu()

    def test_gpu_idle_requires_fresh_pin_admission_before_next_inference(self):
        modules, model, infer, prompt = self.model_modules()
        session = WORKER['ModelSession'](Path('/synthetic/model'), {})
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / 'manifest.json'
            manifest.write_text('{}')
            session.manifest = manifest
            verify = Mock(return_value=Path('/synthetic/model'))
            with patch.dict('sys.modules', modules), patch.dict(
                    session.admit_job_gpu.__globals__, verify_environment=verify):
                session.infer(REQUEST)
                modules['torch'].cuda.is_initialized.return_value = True
                parked = session.prepare_idle_gpu()
                self.assertTrue(parked['job_released'])
                with self.assertRaisesRegex(ValueError, 'fresh job admission'):
                    session.infer(REQUEST)
                manifest.write_text('{"changed":true}')
                with self.assertRaisesRegex(ValueError, 'manifest changed'):
                    session.admit_job_gpu()
                self.assertTrue(session.gpu_idle_prepared)
                manifest.write_text('{}')
                admitted = session.admit_job_gpu()
                self.assertTrue(admitted['gpu_resident'])
                verify.assert_called_once_with({})
                session.infer(REQUEST)
                self.assertEqual(model.decide.call_count, 2)
                infer.Decider.assert_called_once()
                with self.assertRaises(ValueError):
                    session.admit_job_gpu()

    def model_modules(self):
        torch = ModuleType('torch')
        torch.cuda = Mock()
        torch.cuda.is_available.return_value = True
        torch.cuda.max_memory_allocated.return_value = 4096
        model = Mock()
        model.m.tok.encode.return_value = [1, 2, 3]
        model.decide.side_effect = lambda state, questions, **keywords: [
            {'choice': questions[0]['options'][0],
             'probs': {label: float(index == 0) for index, label in enumerate(questions[0]['options'])}}]
        infer = ModuleType('decider.infer')
        infer.Decider = Mock(return_value=model)
        infer.Example = Mock()
        infer.Q = Mock()
        prompt = ModuleType('decider.prompt')
        prompt.build = Mock(return_value={'ids': [1, 2, 3, 4]})
        modules = {'torch': torch, 'decider': ModuleType('decider'), 'decider.infer': infer, 'decider.prompt': prompt}
        return modules, model, infer, prompt

    def test_reuses_one_model_but_infers_every_fresh_request(self):
        modules, model, infer, prompt = self.model_modules()
        session = WORKER['ModelSession'](Path('/synthetic/model'), {'synthetic': True})
        second = {**REQUEST, 'state': 'Different current observation',
                  'options': [{'id': 'save', 'label': 'Save'}, {'id': 'ask', 'label': 'Ask'}]}
        with patch.dict('sys.modules', modules):
            first_result = session.infer(REQUEST)
            second_result = session.infer(second)
        infer.Decider.assert_called_once_with('/synthetic/model', device='cuda', use_graphs=False)
        self.assertEqual(model.decide.call_count, 2)
        self.assertEqual([call.args[0] for call in model.decide.call_args_list], [REQUEST['state'], second['state']])
        self.assertEqual(first_result['prediction']['selected_option'], 'write')
        self.assertEqual(second_result['prediction']['selected_option'], 'save')
        self.assertFalse(first_result['metrics']['reused'])
        self.assertTrue(second_result['metrics']['reused'])
        self.assertEqual(second_result['metrics']['load_ms'], 0.0)
        for result in (first_result, second_result):
            self.assertEqual(result['deployment_digest'], digest({'synthetic': True}))
            self.assertEqual(result['metrics']['input_tokens'], 4)
            self.assertEqual(result['metrics']['peak_vram_bytes'], 4096)
            self.assertGreaterEqual(result['metrics']['inference_ms'], 0)
        for call in model.decide.call_args_list:
            self.assertEqual(call.kwargs, {'max_ctx_tokens': 1536})
        for call in prompt.build.call_args_list:
            self.assertEqual(call.kwargs, {'max_options': 10, 'max_ctx_tokens': 1536})

    def test_invalid_request_never_loads_model(self):
        modules, model, infer, prompt = self.model_modules()
        invalid = [None, {}, {**REQUEST, 'extra': True}, {**REQUEST, 'state': 3},
                   {**REQUEST, 'options': REQUEST['options'][:1]},
                   {**REQUEST, 'options': [REQUEST['options'][0]] * 2},
                   {**REQUEST, 'options': [{'id': 'one', 'label': 'Same'}, {'id': 'two', 'label': 'Same'}]},
                   {**REQUEST, 'options': [{'id': 'one', 'label': ['invalid']}, REQUEST['options'][1]]}]
        session = WORKER['ModelSession'](Path('/synthetic/model'), {})
        with patch.dict('sys.modules', modules):
            for request in invalid:
                with self.subTest(request=request), self.assertRaises(ValueError):
                    session.infer(request)
        infer.Decider.assert_not_called()
        model.decide.assert_not_called()

    def test_token_limit_remains_fail_closed_before_any_inference(self):
        modules, model, infer, prompt = self.model_modules()
        session = WORKER['ModelSession'](Path('/synthetic/model'), {})
        with patch.dict('sys.modules', modules):
            model.m.tok.encode.return_value = list(range(1537))
            with self.assertRaisesRegex(ValueError, 'Token budget'):
                session.infer(REQUEST)
            model.m.tok.encode.return_value = [1]
            prompt.build.return_value = {'ids': list(range(1537))}
            with self.assertRaisesRegex(ValueError, 'Token budget'):
                session.infer(REQUEST)
        infer.Decider.assert_called_once()
        model.decide.assert_not_called()

    def test_request_lines_preserve_buffered_requests_and_bound_partial_idle(self):
        reader, writer = os.pipe()
        try:
            os.write(writer, b'first\nsecond\n')
            lines = WORKER['RequestLines'](reader, idle_seconds=.01)
            self.assertEqual(lines.next(), b'first')
            self.assertEqual(lines.next(), b'second')
            with self.assertRaises(TimeoutError):
                lines.next()
            os.write(writer, b'partial')
            with self.assertRaises(TimeoutError):
                lines.next()
        finally:
            os.close(reader)
            os.close(writer)

    def test_request_lines_reject_oversize_and_truncated_lines(self):
        for content in (b'a' * 65536 + b'\n', b'a' * 65537, b'no-final-newline'):
            with self.subTest(length=len(content)), tempfile.TemporaryFile() as stream:
                stream.write(content)
                stream.seek(0)
                with self.assertRaises(ValueError):
                    WORKER['RequestLines'](stream.fileno()).next()
        with tempfile.TemporaryFile() as stream:
            stream.write(b'a' * 65535 + b'\n')
            stream.seek(0)
            self.assertEqual(len(WORKER['RequestLines'](stream.fileno()).next()), 65535)

    def serve_lines(self, envelopes, session):
        with tempfile.TemporaryFile() as stream:
            stream.write(b''.join(json.dumps(envelope).encode() + b'\n' for envelope in envelopes))
            stream.seek(0)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                WORKER['serve'](session, stream.fileno())
            return [json.loads(line) for line in output.getvalue().splitlines()]

    def test_serve_echoes_ids_with_no_prediction_cache(self):
        session = Mock()
        session.infer.return_value = {'deployment_digest': 'a' * 64, 'prediction': {}, 'metrics': {}}
        responses = self.serve_lines([{'request_id': '1' * 32, 'request': REQUEST},
                                      {'request_id': '2' * 32, 'request': REQUEST}], session)
        self.assertEqual([result['request_id'] for result in responses], ['1' * 32, '2' * 32])
        self.assertEqual(session.infer.call_count, 2)

    def test_serve_rejects_bad_or_replayed_envelopes(self):
        valid = {'request_id': '1' * 32, 'request': REQUEST}
        for envelope in (None, {}, {**valid, 'extra': True}, {**valid, 'request_id': 'ABC'},
                         {**valid, 'request_id': 'a' * 31}, {**valid, 'request_id': 1}):
            session = Mock()
            with self.subTest(envelope=envelope), self.assertRaises(ValueError):
                self.serve_lines([envelope], session)
            session.infer.assert_not_called()
        session = Mock()
        session.infer.return_value = {'prediction': {}, 'metrics': {}}
        with self.assertRaises(ValueError):
            self.serve_lines([valid, valid], session)
        session.infer.assert_called_once_with(REQUEST)

    def test_default_one_shot_envelope_and_serve_verify_environment_once(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / 'manifest.json'
            manifest.write_text('{"synthetic":true}')
            for arguments in ([str(manifest)], [str(manifest), '--serve']):
                session = Mock()
                session.infer.return_value = {'deployment_digest': 'a' * 64, 'prediction': {}, 'metrics': {}}
                verification = Mock(return_value=Path('/synthetic/model'))
                factory = Mock(return_value=session)
                serve = Mock()
                stdin = SimpleNamespace(buffer=io.BytesIO(json.dumps(REQUEST).encode()), fileno=lambda: 3)
                output = io.StringIO()
                with patch.dict(WORKER['main'].__globals__, {'verify_environment': verification, 'ModelSession': factory, 'serve': serve}), \
                        patch('sys.argv', ['worker.py', *arguments]), patch('sys.stdin', stdin), contextlib.redirect_stdout(output):
                    WORKER['main']()
                verification.assert_called_once_with({'synthetic': True})
                factory.assert_called_once_with(Path('/synthetic/model'), {'synthetic': True})
                if len(arguments) == 1:
                    self.assertNotIn('request_id', json.loads(output.getvalue()))
                    session.infer.assert_called_once_with(REQUEST)
                    serve.assert_not_called()
                else:
                    serve.assert_called_once_with(session, 3, idle_seconds=75)
                    session.infer.assert_not_called()

    def test_worker_accepts_only_bounded_explicit_idle_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / 'manifest.json'
            manifest.write_text('{"synthetic":true}')
            stdin = SimpleNamespace(fileno=lambda: 3)
            serve = Mock()
            session = Mock()
            with patch.dict(WORKER['main'].__globals__, {
                    'verify_environment': Mock(return_value=Path('/synthetic/model')),
                    'ModelSession': Mock(return_value=session), 'serve': serve}), patch('sys.stdin', stdin):
                for argument in ('--idle-seconds=0', '--idle-seconds=631', '--idle-seconds=1.5',
                                 '--idle-seconds=-1', '--idle-seconds=300 '):
                    with self.subTest(argument=argument), patch('sys.argv', ['worker.py', str(manifest), argument, '--serve']), self.assertRaises(ValueError):
                        WORKER['main']()
                with patch('sys.argv', ['worker.py', str(manifest), '--idle-seconds=330', '--serve']):
                    WORKER['main']()
                serve.assert_called_once_with(session, 3, idle_seconds=330)

    def test_response_flush_bound_and_idle_bound(self):
        for value in (0, 631, True, '75'):
            with self.assertRaises(ValueError):
                WORKER['RequestLines'](0, idle_seconds=value)
        with patch('builtins.print') as output:
            WORKER['emit']({'synthetic': True})
            output.assert_called_once_with('{"synthetic": true}', flush=True)
        with self.assertRaises(ValueError):
            WORKER['emit']({'oversized': 'a' * 65536})
