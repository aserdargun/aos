"""A per-run Decider engine with a verified private adapter hook."""

import asyncio
import hashlib
import inspect
import json
from pathlib import Path
import re

from pydantic import ValidationError

from .contracts import AOSFault, ErrorCode, REPO_ROOT, Prediction, canonical, digest
from .decision import DeciderEngine
from .reusable_decider import ReusableDeciderEngine


PROTOCOL = 'owned-adapter-runtime-v1'
BINDING_KEYS = {'protocol', 'authorization_sha256', 'adaptation_report_sha256', 'artifact_sha256',
                'input_sha256', 'deployment_manifest_sha256', 'base_deployment_id',
                'runtime_worker_sha256'}
HASH = re.compile(r'^[a-f0-9]{64}$')


def runtime_worker_pin():
    paths = ('src/aos/owned_adapter_engine.py', 'services/decider/owned_adapter_worker.py',
             'services/decider/owned_episode_adapter_core.py', 'services/decider/worker.py',
             'src/aos/reusable_decider.py', 'src/aos/decision.py')
    return digest({name: hashlib.sha256((REPO_ROOT / name).read_bytes()).hexdigest() for name in paths})


def validate_binding(binding, base_deployment_id):
    if not isinstance(binding, dict) or set(binding) != BINDING_KEYS:
        raise ValueError('owned_adapter_binding_invalid')
    if (binding['protocol'] != PROTOCOL
            or any(type(binding[key]) is not str or HASH.fullmatch(binding[key]) is None
                   for key in ('authorization_sha256', 'adaptation_report_sha256', 'artifact_sha256',
                               'input_sha256', 'deployment_manifest_sha256', 'runtime_worker_sha256'))
            or type(binding['base_deployment_id']) is not str
            or binding['base_deployment_id'] != base_deployment_id
            or binding['runtime_worker_sha256'] != runtime_worker_pin()):
        raise ValueError('owned_adapter_binding_invalid')
    return binding


class OwnedAdapterDecisionEngine(ReusableDeciderEngine):
    def __init__(self, base_engine: DeciderEngine, artifact: Path, binding: dict, *, validate_source=None):
        if (not isinstance(base_engine, DeciderEngine)
                or base_engine.identity.get('kind') != 'decider_native_worker'
                or base_engine.identity.get('real_model') is not True):
            raise ValueError('owned_adapter_native_base_required')
        if not callable(validate_source):
            raise ValueError('owned_adapter_source_validator_required')
        checked = validate_binding(binding, base_engine.identity['deployment_id'])
        if not isinstance(artifact, Path) or not artifact.is_absolute():
            raise ValueError('owned_adapter_artifact_path_invalid')
        super().__init__(base_engine.manifest, base_engine.python, timeout=base_engine.timeout,
                         cpu_prewarm=False, idle_seconds=75, gpu_idle_seconds=0)
        self.artifact = artifact
        self.binding = dict(checked)
        self.validate_source = validate_source
        self.adapter_digest = digest(self.binding)
        self.decision_count = 0
        self.worker_started = False
        self.identity = {'deployment_id': 'owned-adapter-' + self.adapter_digest,
                         'kind': 'owned_episode_adapter_runtime', 'real_model': True,
                         'base_deployment_id': base_engine.identity['deployment_id'],
                         'adapter_binding_sha256': self.adapter_digest,
                         'pins': dict(base_engine.pins) | {'owned_adapter_runtime': dict(self.binding)}}

    def assert_source_current(self):
        result = self.validate_source()
        if inspect.isawaitable(result):
            close = getattr(result, 'close', None)
            if callable(close):
                close()
            raise ValueError('owned_adapter_source_validator_must_be_sync')

    async def start(self):
        if self.worker_started:
            raise AOSFault(ErrorCode.MODEL_FAILURE, 'Owned adapter worker cannot restart within a task')
        self.worker_started = True
        spawning = asyncio.create_task(asyncio.create_subprocess_exec(
            str(self.python), str(REPO_ROOT / 'services/decider/owned_adapter_worker.py'),
            str(self.manifest), str(self.artifact), canonical(self.binding), '--serve',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={'PATH': '/usr/bin:/bin', 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
                 'TOKENIZERS_PARALLELISM': 'false', 'OMP_NUM_THREADS': '1'},
            start_new_session=True, limit=65536))
        try:
            self.process = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            self.process = await spawning
            await self.stop_process()
            raise

    async def decide(self, state, options):
        if self.decision_count >= 6:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Owned adapter decision budget exhausted')
        self.decision_count += 1
        self.assert_source_current()
        return await super().decide(state, options)

    def parse_response(self, stdout: bytes, options):
        try:
            response = json.loads(stdout)
            if response['deployment_digest'] != self.adapter_digest:
                raise ValueError('identity mismatch')
            metrics = response['metrics']
            if (not isinstance(metrics, dict) or metrics.get('adapter_loaded') is not True
                    or metrics.get('base_parameters_unchanged') is not True
                    or type(metrics.get('adapter_hook_calls')) is not int
                    or metrics['adapter_hook_calls'] < 1
                    or metrics.get('adapter_sha256') != self.binding['artifact_sha256']):
                raise ValueError('adapter metrics mismatch')
            prediction = Prediction.model_validate(response['prediction'])
            prediction.validate_options(options)
            self.assert_source_current()
            self.last_metrics = metrics
            return prediction
        except (ValueError, KeyError, TypeError, ValidationError):
            raise AOSFault(ErrorCode.INVALID_OUTPUT, 'Invalid owned adapter worker response') from None
