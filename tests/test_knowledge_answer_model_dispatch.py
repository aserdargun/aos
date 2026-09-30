import asyncio
import hashlib
import json
from pathlib import Path
import signal
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aos.contracts import AOSFault, ErrorCode
from aos.knowledge_answer_model import BonsaiKnowledgeAnswerer


class KnowledgeAnswerModelDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest = Path(temporary) / 'manifest.json'
            manifest.write_text(json.dumps({
                'temperature': 0.0, 'max_output_tokens': 4096,
                'server_path': '/synthetic/llama-server', 'model_path': '/synthetic/model',
                'weights_file': 'synthetic.gguf', 'projector_file': 'synthetic-projector.gguf',
                'context_tokens': 16384, 'gpu_layers': 99,
            }))
            self.answerer = BonsaiKnowledgeAnswerer(manifest)
        text = 'Sentetik kaynak: Taslağı kaydedin.'
        self.evidence = [{
            'id': 'c1', 'document_sha256': hashlib.sha256(b'synthetic document').hexdigest(),
            'chunk_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(), 'text': text,
        }]
        self.question = 'Taslağı nasıl kaydederim?'
        self.selection = {
            'schema_version': '1.0', 'needs_human': False,
            'quotes': [{'citation_id': 'c1', 'text': 'Taslağı kaydedin.'}],
        }
        self.process = SimpleNamespace(pid=123456, returncode=None, wait=AsyncMock(return_value=0))
        self.routes = []
        self.posts = []
        self.response = {
            'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(self.selection)}}],
            'usage': {'prompt_tokens': 321, 'completion_tokens': 32},
        }
        self.verify = AsyncMock()
        self.spawn = AsyncMock(return_value=self.process)

    def http(self, port, token, route, body=None, timeout=5):
        self.routes.append(route)
        if route == '/v1/models':
            return {'data': [{'id': self.answerer.identity['deployment_id']}]}
        if route == '/v1/chat/completions':
            self.posts.append(body)
            return self.response
        raise AssertionError('Unexpected model route')

    async def call(self):
        with patch.object(self.answerer, 'verify_pins_for_call', self.verify), patch(
                'aos.supervisor.asyncio.create_subprocess_exec', self.spawn), patch(
                'aos.supervisor.request_json', side_effect=self.http), patch(
                'aos.supervisor.os.killpg') as kill:
            try:
                return await self.answerer.plan(self.question, self.evidence)
            finally:
                self.verify.assert_awaited_once()
                self.spawn.assert_awaited_once()
                kill.assert_called_once_with(self.process.pid, signal.SIGTERM)
                self.process.wait.assert_awaited_once()

    async def test_source_guard_after_readiness_prevents_post_and_cleans_own_process(self):
        self.answerer.last_request = {'stale': 'previous request'}
        self.answerer.last_response = {'stale': 'previous response'}

        def source_guard():
            self.assertEqual(self.routes, ['/v1/models'])
            self.assertEqual(self.posts, [])
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Synthetic source changed during startup')

        self.answerer.before_model_call = source_guard
        with self.assertRaises(AOSFault) as caught:
            await self.call()
        self.assertEqual(caught.exception.code, ErrorCode.UNSAFE_ACTION)
        self.assertEqual(self.routes, ['/v1/models'])
        self.assertEqual(self.posts, [])
        self.assertIsNone(self.answerer.last_request)
        self.assertIsNone(self.answerer.last_response)
        self.assertEqual(self.answerer.last_metrics, {})

    async def test_callback_value_error_prevents_post_and_cleans_own_process(self):
        self.answerer.before_model_call = Mock(side_effect=ValueError('Synthetic control changed'))
        with self.assertRaises(AOSFault) as caught:
            await self.call()
        self.assertEqual(caught.exception.code, ErrorCode.INVALID_OUTPUT)
        self.answerer.before_model_call.assert_called_once_with()
        self.assertEqual(self.posts, [])
        self.assertIsNone(self.answerer.last_request)
        self.assertIsNone(self.answerer.last_response)

    async def test_callback_cancellation_prevents_post_and_cleans_own_process(self):
        self.answerer.before_model_call = Mock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await self.call()
        self.assertEqual(self.posts, [])
        self.assertIsNone(self.answerer.last_request)
        self.assertIsNone(self.answerer.last_response)

    async def test_unexpected_callback_error_still_cleans_own_process(self):
        self.answerer.before_model_call = Mock(side_effect=RuntimeError('Synthetic guard failure'))
        with self.assertRaises(RuntimeError):
            await self.call()
        self.assertEqual(self.posts, [])
        self.assertIsNone(self.answerer.last_request)
        self.assertIsNone(self.answerer.last_response)

    async def test_success_records_exact_dispatched_request_and_validated_response(self):
        expected_request = self.answerer.request_body(self.question, self.evidence)

        def source_guard():
            self.assertEqual(self.routes, ['/v1/models'])
            self.assertIsNone(self.answerer.last_request)
            self.assertIsNone(self.answerer.last_response)

        self.answerer.before_model_call = Mock(side_effect=source_guard)
        result = await self.call()
        self.answerer.before_model_call.assert_called_once_with()
        self.assertEqual(self.routes, ['/v1/models', '/v1/chat/completions'])
        self.assertEqual(self.posts, [expected_request])
        self.assertEqual(self.answerer.last_request, expected_request)
        self.assertIsNot(self.answerer.last_request, self.posts[0])
        self.assertEqual(self.answerer.last_response, self.selection)
        self.assertEqual(result.model_dump(mode='json'), self.selection)
        self.assertEqual(self.answerer.last_metrics['input_tokens'], 321)
        self.assertEqual(self.answerer.last_metrics['output_tokens'], 32)
        self.posts[0]['messages'][1]['content'] = 'changed after dispatch'
        self.assertEqual(self.answerer.last_request, expected_request)

    async def test_truncated_finish_retains_request_but_no_validated_response(self):
        self.answerer.last_response = {'stale': 'previous response'}
        self.response['choices'][0]['finish_reason'] = 'length'
        with self.assertRaises(AOSFault) as caught:
            await self.call()
        self.assertEqual(caught.exception.code, ErrorCode.INVALID_OUTPUT)
        self.assertEqual(self.routes, ['/v1/models', '/v1/chat/completions'])
        self.assertEqual(self.answerer.last_request, self.posts[0])
        self.assertIsNone(self.answerer.last_response)
        self.assertEqual(self.answerer.last_metrics, {})

    async def test_nonverbatim_response_never_becomes_validated_response_data(self):
        self.response['choices'][0]['message']['content'] = json.dumps({
            **self.selection, 'quotes': [{'citation_id': 'c1', 'text': 'Invented claim'}],
        })
        with self.assertRaises(AOSFault) as caught:
            await self.call()
        self.assertEqual(caught.exception.code, ErrorCode.INVALID_OUTPUT)
        self.assertEqual(self.answerer.last_request, self.posts[0])
        self.assertIsNone(self.answerer.last_response)


if __name__ == '__main__':
    unittest.main()
