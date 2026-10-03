import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from aos.contracts import REPO_ROOT, digest
import test_decider_worker as decider_fixtures


specification = importlib.util.spec_from_file_location('synthetic_broker_runtime', REPO_ROOT / 'services/broker_runtime.py')
RUNTIME = importlib.util.module_from_spec(specification)
specification.loader.exec_module(RUNTIME)
decider_module = ModuleType('worker')
for name in ('ModelSession', 'validate_request', 'verify_environment'):
    setattr(decider_module, name, decider_fixtures.WORKER[name])
with patch.dict(sys.modules, {'broker_runtime': RUNTIME, 'worker': decider_module}):
    DECIDER = runpy.run_path(str(REPO_ROOT / 'services/decider/broker_worker.py'))
    BONSAI = runpy.run_path(str(REPO_ROOT / 'services/bonsai/broker_worker.py'))


class ScientistBrokerWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.ready = self.root / 'ready.json'
        self.go = self.root / 'go.json'
        self.manifest = self.root / 'manifest.json'
        self.manifest.write_text('{"synthetic":true}')
        self.environment = {
            'SWAPP_GPU_REQUEST_ID': 'a' * 32, 'SWAPP_GPU_DEPLOYMENT_DIGEST': digest({'synthetic': True}),
            'SWAPP_GPU_TURN_NONCE': 'b' * 64, 'SWAPP_GPU_PROFILE_ID': 'aos.decider.turn.v1',
            'SWAPP_GPU_READY_FILE': str(self.ready), 'SWAPP_GPU_GO_FILE': str(self.go),
            'SWAPP_GPU_ACTIVATION_SECONDS': '2', 'SWAPP_GPU_INFERENCE_SECONDS': '2',
            'SWAPP_GPU_HOST_NETNS_INODE': str(Path('/proc/self/ns/net').stat().st_ino + 1),
            'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'TMPDIR': str(self.root)}
        self.group = '0::/synthetic.slice/swapp-aos-gpu-turn-' + 'c' * 32 + '.service\n'

    def gate(self, profile='aos.decider.turn.v1'):
        environment = self.environment | {'SWAPP_GPU_PROFILE_ID': profile}
        with patch.dict(os.environ, environment), patch.object(Path, 'read_text', return_value=self.group):
            return RUNTIME.TurnGate(self.ready, profile)

    def open_gate(self, gate):
        self.go.write_bytes(RUNTIME.encoded(gate.value))
        self.go.chmod(0o600)

    def test_gate_exact_receipt_and_private_go_file(self):
        gate = self.gate()
        self.open_gate(gate)
        self.assertGreater(gate.admit_inference(), RUNTIME.clock())
        self.assertEqual(json.loads(self.ready.read_bytes()), gate.value)
        self.assertEqual(self.ready.stat().st_mode & 0o777, 0o600)

    def test_wrong_owner_request_nonce_profile_public_file_and_symlink_rejected(self):
        for field in ('request_id', 'nonce', 'profile_id', 'deployment_digest'):
            gate = self.gate()
            self.go.write_bytes(RUNTIME.encoded(gate.value | {field: 'wrong'}))
            self.go.chmod(0o600)
            with self.assertRaises(ValueError):
                gate.admit_inference()
            self.ready.unlink()
        gate = self.gate()
        self.open_gate(gate)
        self.go.chmod(0o644)
        with self.assertRaises(ValueError):
            gate.admit_inference()
        self.ready.unlink()
        target = self.root / 'foreign.json'
        self.go.rename(target)
        self.go.symlink_to(target)
        with self.assertRaises(OSError):
            self.gate().admit_inference()

    def test_gate_timeout_existing_receipt_and_manifest_drift_fail_closed(self):
        gate = self.gate()
        gate.activation_deadline = RUNTIME.clock() - 1
        with self.assertRaises(TimeoutError):
            gate.admit_inference()
        self.assertFalse(self.ready.exists())
        gate = self.gate()
        gate.manifest(self.manifest)
        self.manifest.write_text('{"synthetic":false}')
        with self.assertRaises(ValueError):
            gate.check_manifest(self.manifest)
        self.ready.write_bytes(b'{}')
        with self.assertRaises(FileExistsError):
            gate.admit_inference()

    def test_worker_environment_requires_real_broker_scope_namespace_and_offline(self):
        for field, value in [('SWAPP_GPU_REQUEST_ID', 'wrong'), ('SWAPP_GPU_TURN_NONCE', 'short'),
                             ('SWAPP_GPU_ACTIVATION_SECONDS', '721'), ('TRANSFORMERS_OFFLINE', '0'),
                             ('SWAPP_GPU_HOST_NETNS_INODE', str(Path('/proc/self/ns/net').stat().st_ino))]:
            with self.subTest(field=field), patch.dict(os.environ, self.environment | {field: value}), \
                    patch.object(Path, 'read_text', return_value=self.group), self.assertRaises(ValueError):
                RUNTIME.TurnGate(self.ready, 'aos.decider.turn.v1')
        with patch.dict(os.environ, self.environment), patch.object(Path, 'read_text', return_value='0::/user.slice\n'), self.assertRaises(ValueError):
            RUNTIME.TurnGate(self.ready, 'aos.decider.turn.v1')

    def test_strict_json_duplicate_nonfinite_and_output_bound(self):
        for raw in (b'{"request":{},"request":{}}', b'{"value":NaN}', b'{"value":1e999}', b'[]'):
            with self.assertRaises(ValueError):
                RUNTIME.object_json(raw)
        with self.assertRaises(ValueError):
            RUNTIME.encoded({'text': 'x' * 65536})

    def test_owned_cpu_process_outside_broker_fails_before_model_import_or_loading(self):
        for name, arguments in [('decider', []), ('bonsai', ['recovery'])]:
            environment = os.environ | self.environment | {'SWAPP_GPU_PROFILE_ID':
                'aos.decider.turn.v1' if name == 'decider' else 'aos.bonsai.recovery.v1'}
            result = subprocess.run([sys.executable, str(REPO_ROOT / 'services' / name / 'broker_worker.py'),
                                     str(self.manifest), str(self.ready), *arguments],
                                    input=b'{}', capture_output=True, env=environment,
                                    timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b'')
            self.assertFalse(self.ready.exists())

    def test_decider_gpu_preparation_loads_once_without_inference_using_mock_cuda(self):
        modules, model, infer, prompt = decider_fixtures.DeciderWorkerTests.model_modules(self)
        session = decider_fixtures.WORKER['ModelSession'](Path('/synthetic/model'), {'synthetic': True})
        with patch.dict(sys.modules, modules):
            readiness = session.prepare_gpu()
            model.decide.assert_not_called()
            with self.assertRaises(ValueError):
                session.prepare_gpu()
            response = session.infer(decider_fixtures.REQUEST)
        infer.Decider.assert_called_once_with('/synthetic/model', device='cuda', use_graphs=False)
        self.assertEqual(readiness['deployment_digest'], response['deployment_digest'])
        model.decide.assert_called_once()

    def test_decider_worker_exact_ready_go_infer_output_cpu_mock(self):
        gate = self.gate()
        self.open_gate(gate)
        session = Mock(deployment_digest=gate.value['deployment_digest'])
        session.prepare_gpu.return_value = {'load_ms': 3}

        def infer(request):
            self.assertEqual(json.loads(self.ready.read_bytes()), gate.value)
            return {'deployment_digest': session.deployment_digest, 'prediction': {'synthetic': True}, 'metrics': {}}

        session.infer.side_effect = infer
        output = io.BytesIO()
        with patch.dict(DECIDER['main'].__globals__, {'TurnGate': Mock(return_value=gate),
                'verify_environment': Mock(return_value=Path('/synthetic/model')), 'ModelSession': Mock(return_value=session)}), \
                patch('sys.argv', ['broker_worker.py', str(self.manifest), str(self.ready)]), \
                patch('sys.stdin', SimpleNamespace(buffer=io.BytesIO(RUNTIME.encoded({'request': decider_fixtures.REQUEST})))), \
                patch('sys.stdout', SimpleNamespace(buffer=output)):
            self.assertEqual(DECIDER['main'](), 0)
        self.assertEqual(json.loads(output.getvalue())['metrics']['broker_activation_load_ms'], 3)
        session.infer.assert_called_once()

    def test_bonsai_cleanup_escalation_and_failed_cleanup_no_success(self):
        child = Mock()
        child.poll.side_effect = [None, 0]
        child.wait.side_effect = [subprocess.TimeoutExpired('synthetic', 8), 0]
        BONSAI['cleanup'](child)
        child.terminate.assert_called_once()
        child.kill.assert_called_once()
        bad = Mock()
        bad.poll.return_value = None
        bad.wait.side_effect = subprocess.TimeoutExpired('synthetic', 2)
        with self.assertRaises(subprocess.TimeoutExpired):
            BONSAI['cleanup'](bad)

    def test_bonsai_manifest_exact_artifacts_and_escape_rejection(self):
        model = self.root / 'model'
        runtime = self.root / 'runtime'
        model.mkdir()
        runtime.mkdir()
        for name in ('weights', 'projector'):
            (model / name).write_bytes(b'synthetic')
        server = runtime / 'server'
        server.write_bytes(b'synthetic')
        checksum = hashlib.sha256(b'synthetic').hexdigest()
        pins = {'model_path': str(model), 'model_files': {'weights': checksum, 'projector': checksum},
            'runtime_path': str(runtime), 'runtime_files': {'server': checksum}, 'server_path': str(server),
            'native_libraries': {str(server): checksum}, 'weights_file': 'weights', 'projector_file': 'projector',
            'context_tokens': 4096, 'max_output_tokens': 512, 'gpu_layers': 99, 'parallel': 1}
        BONSAI['verify_manifest'](pins)
        for update in ({'weights_file': '../outside'}, {'server_path': str(model / 'weights')}, {'parallel': 2}):
            with self.assertRaises(ValueError):
                BONSAI['verify_manifest'](pins | update)
        (model / 'weights').write_bytes(b'changed')
        with self.assertRaises(ValueError):
            BONSAI['verify_manifest'](pins)

    def bonsai_flow(self, *, cleanup_failure=False):
        gate = self.gate('aos.bonsai.recovery.v1')
        self.open_gate(gate)
        pins = {'server_path': '/synthetic/server', 'model_path': '/synthetic/model',
                'weights_file': 'weights', 'projector_file': 'projector', 'context_tokens': 4096,
                'gpu_layers': 99, 'max_output_tokens': 512, 'parallel': 1, 'temperature': 0}
        self.manifest.write_bytes(RUNTIME.encoded(pins))
        payload = {'model': 'bonsai-' + gate.value['deployment_digest'], 'temperature': 0,
            'max_tokens': 64, 'stream': False, 'chat_template_kwargs': {'enable_thinking': False},
            'messages': [{'role': 'system', 'content': 'Synthetic'}, {'role': 'user', 'content': 'Synthetic'}],
            'response_format': {'type': 'json_schema', 'json_schema':
                {'name': 'aos_recovery_plan', 'strict': True, 'schema': {'type': 'object'}}}}
        child = Mock()
        child.poll.return_value = None
        key_paths = []

        def launch(command, **arguments):
            key_path = Path(command[command.index('--api-key-file') + 1])
            key_paths.append(key_path)
            self.assertEqual(key_path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(key_path.read_text(), command)
            return child

        def request(port, token, route, body, deadline):
            self.assertEqual(token, key_paths[0].read_text())
            if route == '/v1/models':
                self.assertFalse(self.ready.exists())
                return {'data': [{'id': payload['model']}]}
            self.assertEqual(json.loads(self.ready.read_bytes()), gate.value)
            self.assertEqual(body, payload)
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': '{}'}}],
                    'usage': {'prompt_tokens': 4, 'completion_tokens': 2}}

        if cleanup_failure:
            child.wait.side_effect = subprocess.TimeoutExpired('synthetic', 2)
        else:
            child.wait.side_effect = lambda **arguments: setattr(child.poll, 'return_value', 0)
        output = io.BytesIO()
        with patch.dict(BONSAI['main'].__globals__, {'TurnGate': Mock(return_value=gate),
                    'verify_manifest': Mock(), 'request_json': request}), \
                patch.object(subprocess, 'Popen', side_effect=launch), patch.dict(os.environ, {'TMPDIR': str(self.root)}), \
                patch('sys.argv', ['broker_worker.py', str(self.manifest), str(self.ready), 'recovery']), \
                patch('sys.stdin', SimpleNamespace(buffer=io.BytesIO(RUNTIME.encoded(payload)))), \
                patch('sys.stdout', SimpleNamespace(buffer=output)):
            if cleanup_failure:
                with self.assertRaises(subprocess.TimeoutExpired):
                    BONSAI['main']()
            else:
                self.assertEqual(BONSAI['main'](), 0)
        self.assertFalse(key_paths[0].exists())
        return output.getvalue()

    def test_bonsai_fixed_command_health_gate_infer_and_cleanup_before_output_cpu_mock(self):
        self.assertEqual(json.loads(self.bonsai_flow())['usage']['completion_tokens'], 2)

    def test_bonsai_failed_cleanup_never_publishes_inference_success(self):
        self.assertEqual(self.bonsai_flow(cleanup_failure=True), b'')

    def test_bonsai_alias_token_budget_stream_and_role_mutations_rejected(self):
        gate = self.gate('aos.bonsai.recovery.v1')
        pins = {'temperature': 0, 'max_output_tokens': 512}
        payload = {'model': 'bonsai-' + gate.value['deployment_digest'], 'temperature': 0,
            'max_tokens': 64, 'stream': False, 'chat_template_kwargs': {'enable_thinking': False},
            'messages': [{'role': 'system', 'content': 'Synthetic'}, {'role': 'user', 'content': 'Synthetic'}],
            'response_format': {'type': 'json_schema', 'json_schema':
                {'name': 'aos_recovery_plan', 'strict': True, 'schema': {'type': 'object'}}}}
        BONSAI['validate_payload'](payload, pins, gate, 'recovery')
        for update in ({'model': 'wrong'}, {'max_tokens': True}, {'max_tokens': 513},
                       {'stream': True}, {'temperature': 1}, {'messages': []}):
            with self.assertRaises(ValueError):
                BONSAI['validate_payload'](payload | update, pins, gate, 'recovery')
