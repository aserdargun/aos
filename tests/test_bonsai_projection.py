from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import jsonschema

from aos.contracts import REPO_ROOT, digest
from aos.supervisor import BonsaiSupervisor
from test_scientist_broker_workers import BONSAI
from bonsai_projection import (
    PRODUCER_REVISION, PROJECTION_SCHEMA_SHA256, SCHEMA_PATH,
    project_bonsai_response, projection_pin, verify_projection_pin,
)


def response_fixture():
    content = {'diagnosis': 'Synthetic CPU output projection fixture.', 'evidence_refs': ['missing-file'],
               'assumptions': [], 'revised_plan': [], 'verification_criteria': ['exact_file_content'],
               'needs_human': True}
    return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(content)}}],
            'usage': {'prompt_tokens': 12, 'completion_tokens': 34}}


def native_shape_fixture():
    response = response_fixture()
    response.update(model='bonsai-' + 'a' * 64, id='chatcmpl-' + 'S' * 32,
                    object='chat.completion', created=1790798400, system_fingerprint='synthetic-source-shape',
                    timings={'cache_n': 0, 'prompt_n': 12, 'predicted_n': 34,
                             'prompt_ms': 10.0, 'prompt_per_token_ms': 10.0 / 12,
                             'prompt_per_second': 1200.0, 'predicted_ms': 33.0,
                             'predicted_per_token_ms': 1.0, 'predicted_per_second': 1000.0})
    response['choices'][0].update(index=0)
    response['choices'][0]['message'].update(role='assistant')
    response['usage'].update(total_tokens=46, prompt_tokens_details={'cached_tokens': 0})
    return response


class BonsaiProjectionTests(unittest.TestCase):
    def project(self, response):
        return project_bonsai_response(response, deployment_digest='a' * 64,
                                      context_tokens=4096, max_output_tokens=64)

    def test_schema_is_exact_counterpart_projection_and_pin_is_separate_from_content(self):
        raw = SCHEMA_PATH.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),
                         '09e301a8430aff4164bc63a4489456de8aa819de1ed55c642632d7f4d1012efd')
        schema = json.loads(raw)
        jsonschema.Draft202012Validator.check_schema(schema)
        self.assertEqual(digest(schema), PROJECTION_SCHEMA_SHA256)
        pin = projection_pin()
        self.assertEqual(pin['adapter_sha256'], hashlib.sha256(
            (REPO_ROOT / 'services/bonsai_projection.py').read_bytes()).hexdigest())
        self.assertTrue(verify_projection_pin({'bonsai_output_projection': pin, 'code_revision': PRODUCER_REVISION,
                                               'recovery_schema_sha256': 'b' * 64}))
        jsonschema.validate(self.project(response_fixture()), schema)

    def test_optional_model_and_role_are_preserved_without_synthesis(self):
        for optional in (False, True):
            response = response_fixture()
            if optional:
                response['model'] = 'bonsai-' + 'a' * 64
                response['choices'][0]['message']['role'] = 'assistant'
            original = deepcopy(response)
            projected = self.project(response)
            self.assertEqual(projected, original)
            projected['usage']['prompt_tokens'] = 99
            self.assertEqual(response, original)

    def test_only_explicit_empty_reasoning_channel_is_omitted(self):
        for empty in (None, ''):
            response = response_fixture()
            response['choices'][0]['message']['reasoning_content'] = empty
            self.assertEqual(self.project(response), response_fixture())
        for field, value in [('reasoning_content', 'private reasoning'), ('reasoning_content', []),
                             ('tool_calls', []), ('tool_calls', [{'name': 'execute'}]),
                             ('refusal', None), ('refusal', 'refused'), ('function_call', {})]:
            response = response_fixture()
            response['choices'][0]['message'][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.project(response)

    def test_unknown_or_unrequested_raw_metadata_is_denied(self):
        for path, field, value in [((), '__verbose', {}), ((), 'unknown_metadata', {}),
                                   ((), 'timings', {}), (('usage',), 'completion_tokens_details', {}),
                                   (('choices', 0), 'logprobs', None),
                                   (('choices', 0, 'message'), 'unknown_semantic_extension', {})]:
            response = response_fixture()
            target = response
            for part in path:
                target = target[part]
            target[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.project(response)

    def test_exact_pinned_source_shape_is_validated_then_metadata_is_discarded(self):
        response = native_shape_fixture()
        original = deepcopy(response)
        expected = response_fixture()
        expected['model'] = response['model']
        expected['choices'][0]['message']['role'] = 'assistant'
        self.assertEqual(self.project(response), expected)
        self.assertEqual(response, original)
        response['timings'].update(draft_n=10, draft_n_accepted=3)
        self.assertEqual(self.project(response), expected)

    def test_pinned_native_metadata_nested_types_and_correlations_are_checked(self):
        cases = [((), 'id', 'native-id'), ((), 'object', 'text_completion'), ((), 'created', True),
                 ((), 'system_fingerprint', {}), (('choices', 0), 'index', 1),
                 (('choices', 0), 'index', False), (('usage',), 'total_tokens', 45),
                 (('usage',), 'total_tokens', 46.0), (('usage', 'prompt_tokens_details'), 'cached_tokens', 13),
                 (('usage', 'prompt_tokens_details'), 'extra', 0), (('timings',), 'extra', 0),
                 (('timings',), 'cache_n', True), (('timings',), 'predicted_n', 65),
                 (('timings',), 'prompt_ms', float('nan')), (('timings',), 'draft_n', 10)]
        for path, field, value in cases:
            response = native_shape_fixture()
            target = response
            for part in path:
                target = target[part]
            target[field] = value
            with self.subTest(path=path, field=field), self.assertRaises(ValueError):
                self.project(response)

    def test_incomplete_foreign_and_malformed_content_are_rejected(self):
        for field, value in [('finish_reason', 'length'), ('finish_reason', 'tool_calls')]:
            response = response_fixture()
            response['choices'][0][field] = value
            with self.assertRaises(ValueError):
                self.project(response)
        for content in ['[]', '{"x":1,"x":2}', '{"x":NaN}', '{"x":1e999}', '{} {}',
                        '```json\n{}\n```', '{"text":"' + 'ü' * 40000 + '"}', None]:
            response = response_fixture()
            response['choices'][0]['message']['content'] = content
            with self.subTest(content_type=type(content)), self.assertRaises(ValueError):
                self.project(response)
        response = response_fixture()
        response['model'] = 'bonsai-' + 'b' * 64
        with self.assertRaises(ValueError):
            self.project(response)
        response = response_fixture()
        response['choices'][0]['message']['role'] = 'tool'
        with self.assertRaises(ValueError):
            self.project(response)

    def test_actual_request_and_context_usage_limits_are_enforced(self):
        for usage in [{'prompt_tokens': True, 'completion_tokens': 1},
                      {'prompt_tokens': 12.0, 'completion_tokens': 34},
                      {'prompt_tokens': -1, 'completion_tokens': 34},
                      {'prompt_tokens': 12, 'completion_tokens': 65},
                      {'prompt_tokens': 4080, 'completion_tokens': 34}]:
            response = response_fixture()
            response['usage'] = usage
            with self.subTest(usage=usage), self.assertRaises(ValueError):
                self.project(response)

    def test_legacy_missing_pin_is_distinct_from_invalid_explicit_pin(self):
        self.assertFalse(verify_projection_pin({}))
        valid = projection_pin()
        for invalid in [None, {}, valid | {'version': True}, valid | {'version': 1.0},
                        valid | {'schema_sha256': 'f' * 64}, valid | {'adapter_sha256': 'f' * 64},
                        valid | {'unknown': True}]:
            with self.subTest(pin=invalid), self.assertRaises(ValueError):
                verify_projection_pin({'bonsai_output_projection': invalid, 'code_revision': PRODUCER_REVISION})
        with self.assertRaises(ValueError):
            verify_projection_pin({'bonsai_output_projection': valid, 'code_revision': 'f' * 40})

    def test_source_schema_tamper_and_symlink_are_denied(self):
        pin = projection_pin()
        with tempfile.TemporaryDirectory() as directory:
            altered = Path(directory) / 'schema.json'
            altered.write_text('{"type":"object"}')
            with patch('bonsai_projection.SCHEMA_PATH', altered), self.assertRaises(ValueError):
                verify_projection_pin({'bonsai_output_projection': pin, 'code_revision': PRODUCER_REVISION})
            altered.unlink()
            altered.symlink_to(SCHEMA_PATH)
            with patch('bonsai_projection.SCHEMA_PATH', altered), self.assertRaises(OSError):
                verify_projection_pin({'bonsai_output_projection': pin, 'code_revision': PRODUCER_REVISION})

    def test_opt_in_pin_changes_bonsai_deployment_identity_without_inner_pin_repurpose(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / 'manifest.json'
            pins = {'temperature': 0, 'max_output_tokens': 512, 'code_revision': PRODUCER_REVISION}
            manifest.write_text(json.dumps(pins))
            legacy = BonsaiSupervisor(manifest)
            manifest.write_text(json.dumps(pins | {'bonsai_output_projection': projection_pin()}))
            projected = BonsaiSupervisor(manifest)
            self.assertNotEqual(projected.identity['deployment_id'], legacy.identity['deployment_id'])
            self.assertEqual(projected.pins['recovery_schema_sha256'], legacy.pins['recovery_schema_sha256'])


class BonsaiProjectionWorkerTests(unittest.TestCase):
    def run_worker(self, *, pin=True, transform=None, cleanup_failure=False, pin_drift=False, schema_drift=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            schema_path = root / 'projection.schema.json'
            schema_path.write_bytes(SCHEMA_PATH.read_bytes())
            pins = {'server_path': '/synthetic/server', 'model_path': '/synthetic/model',
                    'weights_file': 'weights', 'projector_file': 'projector', 'context_tokens': 4096,
                    'gpu_layers': 99, 'max_output_tokens': 512, 'parallel': 1, 'temperature': 0,
                    'code_revision': PRODUCER_REVISION}
            if pin is not False:
                pins['bonsai_output_projection'] = projection_pin() if pin is True else pin
            deployment = digest(pins)
            payload = {'model': 'bonsai-' + deployment, 'temperature': 0, 'max_tokens': 64, 'stream': False,
                       'chat_template_kwargs': {'enable_thinking': False},
                       'messages': [{'role': 'system', 'content': 'Synthetic'},
                                    {'role': 'user', 'content': 'Synthetic'}],
                       'response_format': {'type': 'json_schema', 'json_schema': {
                           'name': 'aos_recovery_plan', 'strict': True, 'schema': {'type': 'object'}}}}
            gate = Mock(value={'deployment_digest': deployment}, activation_deadline=time.clock_gettime(time.CLOCK_BOOTTIME) + 2)
            gate.manifest.return_value = pins
            gate.admit_inference.return_value = time.clock_gettime(time.CLOCK_BOOTTIME) + 2
            output = io.BytesIO()
            response = response_fixture()
            response['model'] = payload['model']
            if transform:
                transform(response)
            child = Mock()
            child.poll.return_value = None
            launch = Mock(return_value=child)
            artifact_check = Mock()
            cleanups = []
            def request(port, token, route, body, deadline):
                if route == '/v1/models':
                    return {'data': [{'id': payload['model']}]}
                if pin_drift:
                    pins['bonsai_output_projection']['adapter_sha256'] = 'f' * 64
                if schema_drift:
                    schema_path.write_text('{"type":"object"}')
                return response
            def cleanup(process):
                self.assertIs(process, child)
                cleanups.append(process)
                self.assertEqual(output.getvalue(), b'')
                if cleanup_failure:
                    raise RuntimeError('Synthetic cleanup not proven')
            error = None
            with patch.dict(BONSAI['main'].__globals__, {'TurnGate': Mock(return_value=gate),
                    'verify_manifest': artifact_check, 'request_json': request, 'cleanup': cleanup}), \
                    patch('subprocess.Popen', launch), patch.dict(os.environ, {'TMPDIR': directory}), \
                    patch.dict(BONSAI['main'].__globals__['verify_projection_pin'].__globals__,
                               {'SCHEMA_PATH': schema_path}), \
                    patch('sys.argv', ['broker_worker.py', str(root / 'manifest.json'), str(root / 'ready'), 'recovery']), \
                    patch('sys.stdin', SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode()))), \
                    patch('sys.stdout', SimpleNamespace(buffer=output)):
                try:
                    BONSAI['main']()
                except (ValueError, RuntimeError) as caught:
                    error = caught
            self.assertEqual(cleanups, [child] if launch.call_count else [])
            return output.getvalue(), error, launch.call_count, artifact_check.call_count

    def test_opt_in_worker_projects_only_after_checked_pin_and_before_cleanup_publication(self):
        raw, error, launches, checks = self.run_worker(
            transform=lambda value: value['choices'][0]['message'].update(reasoning_content=''))
        self.assertIsNone(error)
        self.assertEqual((launches, checks), (1, 1))
        self.assertNotIn('reasoning_content', json.loads(raw)['choices'][0]['message'])
        jsonschema.validate(json.loads(raw), json.loads(SCHEMA_PATH.read_bytes()))

    def test_source_derived_native_envelope_projects_through_the_whole_mock_worker(self):
        def native_response(value):
            model = value['model']
            value.clear()
            value.update(native_shape_fixture())
            value['model'] = model
        raw, error, launches, checks = self.run_worker(transform=native_response)
        self.assertIsNone(error)
        self.assertEqual((launches, checks), (1, 1))
        projected = json.loads(raw)
        self.assertEqual(set(projected), {'choices', 'usage', 'model'})
        self.assertEqual(set(projected['usage']), {'prompt_tokens', 'completion_tokens'})
        self.assertEqual(set(projected['choices'][0]), {'finish_reason', 'message'})
        jsonschema.validate(projected, json.loads(SCHEMA_PATH.read_bytes()))

    def test_bad_pin_fails_before_artifact_model_or_process_startup(self):
        for pin in (None, projection_pin() | {'adapter_sha256': 'f' * 64}):
            raw, error, launches, checks = self.run_worker(pin=pin)
            self.assertIsInstance(error, ValueError)
            self.assertEqual((raw, launches, checks), (b'', 0, 0))

    def test_pin_drift_invalid_output_and_cleanup_failure_never_emit_success(self):
        for options in ({'pin_drift': True}, {'schema_drift': True}, {'cleanup_failure': True},
                        {'transform': lambda value: value.update(timings={})}):
            with self.subTest(options=options):
                raw, error, launches, checks = self.run_worker(**options)
                self.assertIsNotNone(error)
                self.assertEqual((raw, launches, checks), (b'', 1, 1))

    def test_unpinned_worker_preserves_legacy_raw_payload(self):
        raw, error, launches, checks = self.run_worker(pin=False, transform=lambda value: value.update(timings={}))
        self.assertIsNone(error)
        self.assertEqual(json.loads(raw)['timings'], {})
        self.assertEqual((launches, checks), (1, 1))
