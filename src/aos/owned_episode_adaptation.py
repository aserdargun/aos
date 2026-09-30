"""Explicit source-bound development adapter training, never promotion."""

import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import struct

from .contracts import REPO_ROOT, canonical, digest
from .dataset_preflight import bounded_file
from .learning_event_outbox import _directory
from .owned_episode_learning import HASH, _validate
from .owned_form_candidate_execution import _read_private_child


BUDGET = {'adapter_rank': 4, 'seed': 42, 'learning_rate': .1, 'optimizer_steps': 1,
          'max_tokens': 1536, 'max_examples': 32, 'worker_timeout_seconds': 180, 'microbatch_size': 1}
ARTIFACT_BYTES = 131188


def runner_pin():
    names = ('src/aos/owned_episode_adaptation.py', 'services/decider/owned_episode_adapter_train.py',
             'services/decider/owned_episode_adapter_replay.py', 'services/decider/owned_episode_adapter_core.py',
             'services/decider/dataset_gradient_probe.py', 'services/decider/worker.py',
             'schemas/owned_episode_adaptation_authorization.schema.json',
             'schemas/owned_episode_adaptation_train.schema.json',
             'schemas/owned_episode_adaptation_replay.schema.json',
             'schemas/owned_episode_adaptation.schema.json', 'examples/dataset_converter_pin.json')
    return digest({name: hashlib.sha256(bounded_file(REPO_ROOT / name)).hexdigest() for name in names})


class OwnedEpisodeAdaptation:
    def __init__(self, learning):
        self.learning = learning
        self.task = None
        self.current = None
        self.authorization = None
        self.attempts = 0

    @property
    def reserved(self):
        return self.task is not None and not self.task.done()

    def status(self):
        return self.current or {'state': 'idle', 'training_ready': False, 'promotion_authorized': False}

    def control(self, authority):
        scheduler = self.learning.scheduler
        current = scheduler.controller.state()
        if (scheduler.closed or scheduler.restart_quiesced or scheduler.busy or scheduler.paused
                or scheduler.sequences.reserved or scheduler.planning_reserved
                or current['owner'] != 'AGENT' or current['status'] != 'running'
                or any(current[key] != authority[key] for key in ('lease_id', 'generation'))):
            raise ValueError('episode_adaptation_control_changed')

    def permits_audit(self, execution_sha256):
        try:
            if (not self.reserved or asyncio.current_task() is not self.task
                    or self.current['state'] not in {'training', 'evaluating'}
                    or self.authorization['candidate_execution_sha256'] != execution_sha256):
                return False
            self.control(self.authorization['authority'])
            return True
        except (RuntimeError, ValueError, KeyError, TypeError):
            return False

    def material(self, episode_id, conversion_sha256, authority, attempt):
        preparation = self.learning.preparation
        manifest, converted = preparation.load(episode_id, conversion_sha256)
        readiness = preparation.inspect(episode_id, conversion_sha256)
        if readiness['system1']['tokenizer'] != 'verified' or readiness['tokenizer_report_sha256'] is None:
            raise ValueError('episode_adaptation_verified_tokenizer_required')
        engine, _source, pins, raw, _request = preparation.native_inputs(episode_id, converted)
        examples = [row['payload'] for row in converted['system1']]
        authorization = {'schema_version': '1.0', 'episode_id': episode_id,
            'conversion_sha256': conversion_sha256, 'tokenizer_report_sha256': readiness['tokenizer_report_sha256'],
            'execution_source_sha256': manifest['execution']['execution_source_sha256'],
            'candidate_execution_sha256': manifest['execution']['candidate_execution_sha256'],
            'source_group_sha256': manifest['source_group_sha256'],
            'memberships_sha256': digest(manifest['memberships']['system1']),
            'development_decisions': len(examples), 'input_sha256': digest(examples),
            'deployment_id': engine.identity['deployment_id'],
            'deployment_manifest_sha256': hashlib.sha256(raw).hexdigest(), 'runner_sha256': runner_pin(),
            'authority': authority, 'attempt': attempt, 'budget': BUDGET.copy(),
            'scope': 'owned_synthetic_development_adapter', 'split': 'development_only',
            'rights_redaction_reviewed': True, 'experimental_training_authorized': True,
            'network_export_authorized': False, 'promotion_authorized': False, 'training_ready': False}
        _validate('owned_episode_adaptation_authorization', authorization)
        return authorization, examples, engine, pins

    def preview(self, episode_id, conversion_sha256, lease_id, generation):
        authority = {'lease_id': lease_id, 'generation': generation}
        self.control(authority)
        if self.reserved or self.learning.preparation.reserved or self.attempts >= 4:
            raise ValueError('episode_adaptation_unavailable')
        authorization, _examples, _engine, _pins = self.material(
            episode_id, conversion_sha256, authority, self.attempts + 1)
        return {'authorization_sha256': digest(authorization), 'authorization': authorization,
                'persisted': False, 'training_started': False}

    @contextmanager
    def directory(self, episode_id, checksum, *, create=False):
        if type(checksum) is not str or HASH.fullmatch(checksum) is None:
            raise ValueError('episode_adaptation_hash_invalid')
        store = self.learning.store
        name = 'adapter-' + checksum
        with store.directory(episode_id) as parent:
            if create:
                os.mkdir(name, 0o700, dir_fd=parent)
                os.fsync(parent)
            path = store.root / episode_id / name
            descriptor = _directory(path, create=False)
            try:
                opened = os.fstat(descriptor)
                linked = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino):
                    raise ValueError('episode_adaptation_directory_changed')
                yield descriptor, path
                linked = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino):
                    raise ValueError('episode_adaptation_directory_changed')
            finally:
                os.close(descriptor)

    def start(self, episode_id, conversion_sha256, confirm_sha256, lease_id, generation,
              rights_redaction_reviewed, experimental_training_authorized):
        if rights_redaction_reviewed is not True or experimental_training_authorized is not True:
            raise ValueError('episode_adaptation_separate_authorization_required')
        preview = self.preview(episode_id, conversion_sha256, lease_id, generation)
        if preview['authorization_sha256'] != confirm_sha256:
            raise ValueError('episode_adaptation_exact_confirmation_required')
        authorization = preview['authorization']
        with self.directory(episode_id, confirm_sha256, create=True) as (descriptor, _path):
            self.learning.store._put(descriptor, 'authorization.json', authorization)
            output = os.open('candidate.bin', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=descriptor)
            os.close(output)
            os.fsync(descriptor)
        self.attempts += 1
        self.authorization = authorization
        current = {'state': 'training', 'episode_id': episode_id, 'conversion_sha256': conversion_sha256,
                   'authorization_sha256': confirm_sha256, 'training_ready': False, 'promotion_authorized': False}
        self.current = current
        self.task = asyncio.create_task(self._run(current, authorization))
        return current.copy()

    def recheck(self, authorization):
        self.control(authorization['authority'])
        actual, examples, engine, pins = self.material(authorization['episode_id'],
            authorization['conversion_sha256'], authorization['authority'], authorization['attempt'])
        if actual != authorization:
            raise ValueError('episode_adaptation_source_changed')
        return examples, engine, pins

    async def worker(self, script, engine, artifact, request):
        process = None
        raw = canonical(request).encode()
        if len(raw) > 1048576:
            raise ValueError('episode_adaptation_input_bound')
        try:
            async with asyncio.timeout(BUDGET['worker_timeout_seconds']):
                spawning = asyncio.create_task(asyncio.create_subprocess_exec(
                    str(engine.python), str(REPO_ROOT / 'services/decider' / script),
                    str(engine.manifest), str(artifact), stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=65537,
                    env={'PATH': '/usr/bin:/bin', 'HOME': str(Path.home()), 'HF_HUB_OFFLINE': '1',
                         'TRANSFORMERS_OFFLINE': '1', 'TOKENIZERS_PARALLELISM': 'false', 'OMP_NUM_THREADS': '1'}))
                try:
                    process = await asyncio.shield(spawning)
                except asyncio.CancelledError:
                    process = await spawning
                    raise
                process.stdin.write(raw)
                await process.stdin.drain()
                process.stdin.close()
                output = bytearray()
                while chunk := await process.stdout.read(min(8192, 65537 - len(output))):
                    output.extend(chunk)
                    if len(output) > 65536:
                        raise ValueError('episode_adaptation_output_bound')
                if await process.wait():
                    raise ValueError('episode_adaptation_worker_failed')
                return json.loads(output)
        finally:
            if process is not None:
                if process.returncode is None:
                    process.kill()
                await process.wait()

    def validate_worker(self, result, authorization, pins, *, replay=False):
        _validate('owned_episode_adaptation_replay' if replay else 'owned_episode_adaptation_train', result)
        bindings = {'authorization_sha256': digest(authorization),
                    'conversion_sha256': authorization['conversion_sha256'],
                    'manifest_sha256': authorization['deployment_manifest_sha256'],
                    'input_sha256': authorization['input_sha256'],
                    'development_decisions': authorization['development_decisions'],
                    'checkpoint_revision': pins['checkpoint_revision'], 'code_revision': pins['code_revision'],
                    'weights_sha256': pins['model_files']['model.safetensors']}
        if any(result[key] != value for key, value in bindings.items()):
            raise ValueError('episode_adaptation_worker_binding_changed')
        if not replay and result['peak_vram_allocated_bytes'] > result['peak_vram_reserved_bytes']:
            raise ValueError('episode_adaptation_memory_invalid')

    def artifact(self, descriptor, authorization, checksum):
        raw = _read_private_child(descriptor, 'candidate.bin', ARTIFACT_BYTES)
        if (len(raw) != ARTIFACT_BYTES or raw[:8] != b'AOSLORA1'
                or raw[8:40] != bytes.fromhex(digest(authorization))
                or raw[40:72] != bytes.fromhex(authorization['input_sha256'])
                or raw[72:104] != bytes.fromhex(authorization['deployment_manifest_sha256'])
                or struct.unpack_from('<III', raw, 104) != (4, 6144, 2048)
                or hashlib.sha256(raw).hexdigest() != checksum):
            raise ValueError('episode_adaptation_artifact_changed')
        return raw

    def check_attempt(self, descriptor, authorization):
        with self.directory(authorization['episode_id'], digest(authorization)) as (current, _path):
            previous_info, current_info = os.fstat(descriptor), os.fstat(current)
            if (previous_info.st_dev, previous_info.st_ino) != (current_info.st_dev, current_info.st_ino):
                raise ValueError('episode_adaptation_attempt_replaced')
            if self.learning.store._read(current, 'authorization.json') != authorization:
                raise ValueError('episode_adaptation_authorization_changed')

    async def _run(self, current, authorization):
        try:
            from .reusable_decider import ReusableDeciderEngine

            examples, engine, pins = self.recheck(authorization)
            if isinstance(engine, ReusableDeciderEngine):
                await engine.close()
            examples, engine, pins = self.recheck(authorization)
            request = {'mode': 'owned_episode_adapter_train_v1', 'examples': examples,
                       'authorization_sha256': digest(authorization),
                       'conversion_sha256': authorization['conversion_sha256'], 'max_tokens': 1536}
            with self.directory(authorization['episode_id'], digest(authorization)) as (descriptor, path):
                self.check_attempt(descriptor, authorization)
                train = await self.worker('owned_episode_adapter_train.py', engine, path / 'candidate.bin', request)
                self.recheck(authorization)
                self.validate_worker(train, authorization, pins)
                checksum = train['candidate_artifact_sha256']
                self.artifact(descriptor, authorization, checksum)
                self.check_attempt(descriptor, authorization)
                current['state'] = 'evaluating'
                replay = await self.worker('owned_episode_adapter_replay.py', engine, path / 'candidate.bin',
                    request | {'mode': 'owned_episode_adapter_replay_v1', 'artifact_sha256': checksum,
                               'deployment_manifest_sha256': authorization['deployment_manifest_sha256']})
                self.recheck(authorization)
                self.validate_worker(replay, authorization, pins, replay=True)
                if replay['artifact_sha256'] != checksum:
                    raise ValueError('episode_adaptation_replay_artifact_changed')
                self.artifact(descriptor, authorization, checksum)
                self.check_attempt(descriptor, authorization)
                report = {'schema_version': '1.0', 'authorization_sha256': digest(authorization),
                    'authorization': authorization, 'artifact_sha256': checksum, 'artifact_bytes': ARTIFACT_BYTES,
                    'train': train, 'replay': replay, 'evaluation': 'resubstitution', 'synthetic': True,
                    'training_ready': False, 'promotion_authorized': False}
                _validate('owned_episode_adaptation', report)
                self.learning.store._put(descriptor, 'report.json', report)
            current['state'] = 'verified'
        except asyncio.CancelledError:
            current['state'] = 'cancelled'
            raise
        except Exception:
            current['state'] = 'failed'

    def inspect(self, episode_id, authorization_sha256):
        with self.directory(episode_id, authorization_sha256) as (descriptor, _path):
            authorization = self.learning.store._read(descriptor, 'authorization.json')
            _validate('owned_episode_adaptation_authorization', authorization)
            if digest(authorization) != authorization_sha256 or authorization['episode_id'] != episode_id:
                raise ValueError('episode_adaptation_authorization_changed')
            current, _examples, _engine, pins = self.material(episode_id, authorization['conversion_sha256'],
                authorization['authority'], authorization['attempt'])
            if current != authorization:
                raise ValueError('episode_adaptation_source_changed')
            report = self.learning.store._read(descriptor, 'report.json')
            _validate('owned_episode_adaptation', report)
            if report['authorization'] != authorization or report['authorization_sha256'] != authorization_sha256:
                raise ValueError('episode_adaptation_report_changed')
            self.validate_worker(report['train'], authorization, pins)
            self.validate_worker(report['replay'], authorization, pins, replay=True)
            if (report['artifact_sha256'] != report['train']['candidate_artifact_sha256']
                    or report['artifact_sha256'] != report['replay']['artifact_sha256']):
                raise ValueError('episode_adaptation_report_artifact_changed')
            self.artifact(descriptor, authorization, report['artifact_sha256'])
            return report

    async def cancel(self):
        if self.current is not None and self.current['state'] in {'training', 'evaluating'}:
            self.current['state'] = 'cancelled'
        task = self.task
        if task is not None and not task.done():
            if not task.cancelling():
                task.cancel()
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.done():
                    await task
