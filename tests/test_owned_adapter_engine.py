import asyncio
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aos.contracts import AOSFault, Option, State, digest
from aos.decision import DeciderEngine
from aos.owned_adapter_engine import (
    OwnedAdapterDecisionEngine,
    runtime_worker_pin,
    validate_binding as validate_engine_binding,
)
import aos.owned_adapter_engine as engine_module

SERVICE = Path(__file__).resolve().parents[1] / 'services' / 'decider'
if str(SERVICE) not in sys.path:
    sys.path.insert(0, str(SERVICE))
import owned_adapter_worker as worker


def make_binding(base_deployment_id):
    return {'protocol': 'owned-adapter-runtime-v1', 'authorization_sha256': 'a' * 64,
            'adaptation_report_sha256': 'b' * 64, 'artifact_sha256': 'c' * 64,
            'input_sha256': 'd' * 64, 'deployment_manifest_sha256': 'e' * 64,
            'base_deployment_id': base_deployment_id, 'runtime_worker_sha256': runtime_worker_pin()}


def decision_state():
    return State(task_id='task-synthetic', run_id='run-synthetic', step_id='step-synthetic',
                 runtime_id='runtime-synthetic', deployment_id='decider-synthetic',
                 owner_lease_id='lease-synthetic', normalized_goal='Save the synthetic record',
                 observation='Synthetic form is open')


class OwnedAdapterEngineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.manifest = self.root / 'manifest.json'
        self.pins = {'synthetic': True}
        self.manifest.write_text(json.dumps(self.pins, indent=2) + '\n')
        self.base = DeciderEngine(self.manifest, Path(sys.executable))
        self.binding = make_binding(self.base.identity['deployment_id'])
        self.artifact = self.root / 'candidate.bin'

    def test_engine_identity_pins_binding_without_mutating_base(self):
        original = json.loads(json.dumps(self.base.identity))
        engine = OwnedAdapterDecisionEngine(self.base, self.artifact, self.binding,
                                            validate_source=lambda: None)
        self.assertEqual(engine.identity['kind'], 'owned_episode_adapter_runtime')
        self.assertTrue(engine.identity['real_model'])
        self.assertEqual(engine.identity['deployment_id'], 'owned-adapter-' + digest(self.binding))
        self.assertEqual(engine.identity['pins']['owned_adapter_runtime'], self.binding)
        self.assertEqual({key: engine.identity['pins'][key] for key in self.base.pins}, self.base.pins)
        self.assertEqual(self.base.identity, original)
        self.assertEqual(engine.pins, self.base.pins)
        with self.assertRaisesRegex(ValueError, 'validator_required'):
            OwnedAdapterDecisionEngine(self.base, self.artifact, self.binding)

    def test_engine_checks_source_before_and_after_response_and_requires_hook_metrics(self):
        checks = []
        engine = OwnedAdapterDecisionEngine(self.base, self.artifact, self.binding,
                                            validate_source=lambda: checks.append('current'))
        options = [Option(id='first', label='Ask a human'), Option(id='second', label='Save the record')]
        state = decision_state()
        response = {'deployment_digest': digest(self.binding),
                    'prediction': {'selected_option': 'second',
                                   'probabilities': {'first': 0.1, 'second': 0.9}},
                    'metrics': {'adapter_loaded': True, 'adapter_hook_calls': 3,
                                'adapter_sha256': self.binding['artifact_sha256'],
                                'base_parameters_unchanged': True}}
        engine.request = AsyncMock(return_value=response)
        result = asyncio.run(engine.decide(state, options))
        self.assertEqual(result.selected_option, 'second')
        self.assertEqual(checks, ['current', 'current'])
        self.assertEqual(engine.last_metrics['adapter_hook_calls'], 3)

        engine.request = AsyncMock(return_value=response | {'metrics': response['metrics'] | {
            'adapter_hook_calls': 0}})
        with self.assertRaises(AOSFault):
            asyncio.run(engine.decide(state, options))

    def test_worker_binding_artifact_and_request_contracts_fail_closed(self):
        manifest_sha = hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        self.binding['deployment_manifest_sha256'] = manifest_sha
        self.binding['runtime_worker_sha256'] = worker.runtime_worker_pin()
        self.assertEqual(worker.runtime_worker_pin(), runtime_worker_pin())
        self.assertEqual(worker.validate_binding(self.binding, self.pins, manifest_sha), self.binding)
        self.assertEqual(validate_engine_binding(self.binding, self.base.identity['deployment_id']), self.binding)
        for changed in (self.binding | {'unexpected': False},
                        self.binding | {'base_deployment_id': 'decider-' + '0' * 64},
                        self.binding | {'artifact_sha256': 'z' * 64}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                worker.validate_binding(changed, self.pins, manifest_sha)

        payload = (b'AOSLORA1' + bytes.fromhex(self.binding['authorization_sha256'])
                   + bytes.fromhex(self.binding['input_sha256']) + bytes.fromhex(manifest_sha)
                   + struct.pack('<III', 4, 6144, 2048) + b'\0' * (32768 * 4))
        self.binding['artifact_sha256'] = hashlib.sha256(payload).hexdigest()
        weights = worker.validate_artifact_payload(payload, self.binding)
        self.assertEqual(len(weights), 32768)
        with self.assertRaisesRegex(ValueError, 'artifact_binding'):
            worker.validate_artifact_payload(payload[:-1], self.binding)

        request = {'state': 'Synthetic state', 'question': 'Choose',
                   'options': [{'id': 'a', 'label': 'A'}, {'id': 'b', 'label': 'B'}]}
        envelope = {'request_id': '1' * 32, 'request': request}
        self.assertEqual(worker.validate_envelope(envelope, set()), envelope)
        for invalid in (envelope | {'operation': 'admit_job_cpu'},
                        {'request_id': '1' * 32, 'request': request | {'extra': True}},
                        {'request_id': 'bad', 'request': request}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                worker.validate_envelope(invalid, set())
        with self.assertRaises(ValueError):
            worker.validate_envelope(envelope, {'1' * 32})

    def test_spawn_cancellation_waits_for_process_and_stops_it(self):
        async def scenario():
            engine = OwnedAdapterDecisionEngine(self.base, self.artifact, self.binding,
                                                validate_source=lambda: None)
            entered, release = asyncio.Event(), asyncio.Event()
            process = SimpleNamespace(returncode=None)
            stopped = AsyncMock()
            engine.stop_process = stopped

            async def delayed_spawn(*_args, **_kwargs):
                entered.set()
                await release.wait()
                return process

            with unittest.mock.patch.object(engine_module.asyncio, 'create_subprocess_exec',
                                             side_effect=delayed_spawn):
                task = asyncio.create_task(engine.start())
                await asyncio.wait_for(entered.wait(), timeout=2)
                task.cancel()
                await asyncio.sleep(0)
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=2)
            self.assertIs(engine.process, process)
            stopped.assert_awaited_once_with()

        asyncio.run(scenario())

    def test_engine_budget_survives_worker_exit_and_forbids_restart(self):
        async def scenario():
            engine = OwnedAdapterDecisionEngine(self.base, self.artifact, self.binding,
                                                validate_source=lambda: None)
            engine.decision_count = 6
            engine.request = AsyncMock()
            with self.assertRaisesRegex(AOSFault, 'budget exhausted'):
                await engine.decide(decision_state(), [])
            engine.request.assert_not_awaited()
            engine.worker_started = True
            with unittest.mock.patch.object(engine_module.asyncio, 'create_subprocess_exec',
                                            new_callable=AsyncMock) as spawn:
                with self.assertRaisesRegex(AOSFault, 'cannot restart'):
                    await engine.start()
                spawn.assert_not_awaited()
        asyncio.run(scenario())

    def test_cpu_prepare_precedes_cuda_free_memory_query(self):
        events = []
        cuda = SimpleNamespace(is_initialized=lambda: False, is_available=lambda: True,
                               mem_get_info=lambda: events.append('cuda_query') or (0, 16 * 1024 ** 3))
        session = SimpleNamespace(prepare_cpu=lambda: events.append('cpu_prepare'))
        with unittest.mock.patch.dict(sys.modules, {'torch': SimpleNamespace(cuda=cuda)}), \
                unittest.mock.patch.object(worker, 'read_manifest', return_value=b'{}'), \
                unittest.mock.patch.object(worker, 'private_manifest_pins', return_value=({}, None)), \
                unittest.mock.patch.object(worker, 'validate_binding'), \
                unittest.mock.patch.object(worker, 'verify_environment'), \
                unittest.mock.patch.object(worker, 'read_private_artifact'), \
                unittest.mock.patch.object(worker, 'validate_artifact_payload'), \
                unittest.mock.patch.object(worker, 'ModelSession', return_value=session):
            with self.assertRaisesRegex(ValueError, '8gib_free_cuda'):
                worker.create_inference(self.manifest, self.artifact, self.binding)
        self.assertEqual(events, ['cpu_prepare', 'cuda_query'])


if __name__ == '__main__':
    unittest.main()
