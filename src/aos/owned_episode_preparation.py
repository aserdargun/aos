"""Source-bound development conversion and isolated CPU tokenizer preparation."""

import asyncio
import hashlib
import json
import os
from pathlib import Path

from .contracts import REPO_ROOT, canonical, digest
from .dataset_preflight import bounded_file, check_probe_pins
from .dataset_tokenizer import validate_probe_report
from .owned_episode_learning import HASH, MAX_BYTES, ROLES, _validate
from .owned_form_candidate_execution import _read_private_child


def converter_pin():
    names = ('src/aos/owned_episode_conversion.py', 'schemas/owned_episode_conversion.schema.json',
             'src/aos/owned_episode_preparation.py', 'src/aos/owned_skill_planner.py',
             'src/aos/contracts.py', 'src/aos/owned_episode_service.py', 'schemas/owned_skill_plan.schema.json',
             'schemas/owned_episode_export_record.schema.json', 'examples/dataset_converter_pin.json')
    return digest({name: hashlib.sha256(bounded_file(REPO_ROOT / name)).hexdigest() for name in names})


def tokenizer_runner_pin():
    names = ('src/aos/owned_episode_preparation.py', 'services/decider/tokenizer_probe.py',
             'src/aos/dataset_tokenizer.py', 'src/aos/dataset_preflight.py',
             'schemas/dataset_tokenizer_probe.schema.json', 'examples/dataset_converter_pin.json')
    return digest({name: hashlib.sha256(bounded_file(REPO_ROOT / name)).hexdigest() for name in names})


class OwnedEpisodePreparation:
    def __init__(self, learning):
        self.learning = learning
        self.task = None
        self.current = None
        self.attempts = 0

    @property
    def reserved(self):
        return self.task is not None and not self.task.done()

    def status(self):
        return self.current or {'state': 'idle', 'training_ready': False}

    def material(self, episode_id, export_sha256):
        from .owned_episode_conversion import convert_rows

        inspected = self.learning.inspect(episode_id, read_only=True)
        if not inspected['reviewable']:
            raise ValueError('episode_conversion_source_not_reviewable')
        store = self.learning.store
        execution = store.execution(episode_id) | {
            'execution_source_sha256': inspected['execution_source_sha256']}
        exported, original = store.load_export(episode_id, export_sha256, inspected['candidates'], execution)
        converted = {role: convert_rows(role, [json.loads(line) for line in
                      original[role + '.jsonl'].splitlines()]) for role in ROLES}
        files = {role + '.converted.jsonl': ''.join(canonical(row) + '\n' for row in converted[role]).encode()
                 for role in ROLES}
        manifest = {'schema_version': '1.0', 'format': 'owned-episode-conversion-v1',
                    'episode_id': episode_id, 'export_sha256': export_sha256,
                    'execution': execution, 'review_receipts': exported['review_receipts'],
                    'converter_sha256': converter_pin(), 'source_group_sha256': exported['source_group_sha256'],
                    'counts': exported['counts'], 'split': 'development_only', 'synthetic': True,
                    'training_ready': False,
                    'memberships': {role: [{'source_record_sha256': row['source_record_sha256'],
                                          'converted_record_sha256': digest(row)} for row in converted[role]]
                                    for role in ROLES},
                    'files': {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}}
        _validate('owned_episode_conversion_manifest', manifest)
        return manifest, files, converted

    def preview(self, episode_id, export_sha256):
        manifest, _files, converted = self.material(episode_id, export_sha256)
        return self.report(manifest, converted, persisted=False)

    def publish(self, episode_id, export_sha256, confirm_sha256):
        manifest, files, _converted = self.material(episode_id, export_sha256)
        checksum = digest(manifest)
        if confirm_sha256 != checksum:
            raise ValueError('episode_conversion_confirmation_required')
        store = self.learning.store
        with store.directory(episode_id) as descriptor:
            for name, raw in files.items():
                store._put_bytes(descriptor, checksum + '-' + name, raw)
            store._put(descriptor, checksum + '-conversion.json', manifest)
        return self.inspect(episode_id, checksum)

    def load(self, episode_id, conversion_sha256):
        if type(conversion_sha256) is not str or HASH.fullmatch(conversion_sha256) is None:
            raise ValueError('episode_conversion_pin_invalid')
        store = self.learning.store
        with store.directory(episode_id) as descriptor:
            manifest = store._read(descriptor, conversion_sha256 + '-conversion.json')
            _validate('owned_episode_conversion_manifest', manifest)
            if digest(manifest) != conversion_sha256 or manifest['episode_id'] != episode_id:
                raise ValueError('episode_conversion_manifest_changed')
            files = {role + '.converted.jsonl': _read_private_child(
                descriptor, conversion_sha256 + '-' + role + '.converted.jsonl', MAX_BYTES) for role in ROLES}
        expected, expected_files, converted = self.material(episode_id, manifest['export_sha256'])
        if manifest != expected or files != expected_files:
            raise ValueError('episode_conversion_source_changed')
        return manifest, converted

    def report(self, manifest, converted, *, persisted, tokenizer=None):
        blockers = ['synthetic_development_only', 'independent_validation_test_missing',
                    'rights_redaction_review_missing', 'training_authorization_missing',
                    'system1_forward_loss_missing', 'system2_tokenizer_trainer_unverified',
                    'promotion_unavailable']
        if tokenizer is None:
            blockers.append('system1_tokenizer_missing')
        try:
            self.native_inputs(manifest['episode_id'], converted)
            tokenizer_available = True
        except (ValueError, OSError, KeyError, AttributeError, TypeError):
            tokenizer_available = False
            blockers.append('system1_tokenizer_prerequisites_missing')
        report = {'schema_version': '1.0', 'available': True, 'episode_id': manifest['episode_id'],
                'export_sha256': manifest['export_sha256'], 'conversion_sha256': digest(manifest),
                'persisted': persisted, 'counts': manifest['counts'],
                'source_group_sha256': manifest['source_group_sha256'], 'split': 'development_only',
                'system1': {'format': 'decider-example-q-v1', 'converter_verified': True,
                            'tokenizer': 'verified' if tokenizer is not None else 'missing',
                            'tokenizer_start_available': tokenizer_available},
                'system2': {'format': 'aos-owned-plan-messages-v1', 'converter_verified': True,
                            'tokenizer': 'unverified', 'trainer': 'unverified'},
                'tokenizer_report_sha256': digest(tokenizer) if tokenizer is not None else None,
                'blockers': sorted(blockers), 'training_ready': False, 'promotion_authorized': False}
        _validate('owned_episode_readiness', report)
        return report

    def inspect(self, episode_id, conversion_sha256):
        manifest, converted = self.load(episode_id, conversion_sha256)
        store = self.learning.store
        with store.directory(episode_id) as descriptor:
            try:
                tokenizer = store._read(descriptor, conversion_sha256 + '-' + tokenizer_runner_pin() + '-tokenizer.json')
            except FileNotFoundError:
                tokenizer = None
        if tokenizer is not None:
            self.validate_tokenizer(tokenizer, manifest, converted)
        return self.report(manifest, converted, persisted=True, tokenizer=tokenizer)

    def native_inputs(self, episode_id, converted):
        from .decision import DeciderEngine

        engine = self.learning.scheduler.engine
        if not isinstance(engine, DeciderEngine) or engine.identity['kind'] != 'decider_native_worker':
            raise ValueError('episode_tokenizer_native_decider_required')
        raw = bounded_file(engine.manifest)
        pins = json.loads(raw)
        if pins != engine.pins:
            raise ValueError('episode_tokenizer_deployment_changed')
        sources = [candidate['source'] for candidate in self.learning.store.candidates(episode_id)
                   if candidate['role'] == 'system1']
        if (len(sources) != len(converted['system1']) or any(
                source['deployment_id'] != engine.identity['deployment_id']
                or source['deployment_sha256'] != digest(engine.identity) for source in sources)):
            raise ValueError('episode_tokenizer_source_deployment_changed')
        expected = json.loads(bounded_file(REPO_ROOT / 'examples/dataset_converter_pin.json'))
        source = Path(os.environ.get('AOS_DECIDER_DATA_SOURCE',
            REPO_ROOT / 'models' / ('decider-data-' + expected['revision'])))
        core = bounded_file(source / 'core.py')
        if hashlib.sha256(core).hexdigest() != expected['files']['core.py']:
            raise ValueError('episode_tokenizer_upstream_changed')
        examples = [row['payload'] for row in converted['system1']]
        if not 1 <= len(examples) <= 32:
            raise ValueError('episode_tokenizer_all_examples_required')
        request = canonical({'examples': examples, 'max_tokens': 1536}).encode()
        if len(request) > 1048576:
            raise ValueError('episode_tokenizer_input_bound')
        return engine, source, pins, raw, request

    def validate_tokenizer(self, report, manifest, converted):
        _validate('owned_episode_tokenizer', report)
        engine, _source, pins, raw, request = self.native_inputs(manifest['episode_id'], converted)
        if (report['conversion_sha256'] != digest(manifest)
                or report['input_sha256'] != hashlib.sha256(request).hexdigest()
                or report['deployment_manifest_sha256'] != hashlib.sha256(raw).hexdigest()
                or report['runner_sha256'] != tokenizer_runner_pin()
                or report['deployment_id'] != engine.identity['deployment_id']):
            raise ValueError('episode_tokenizer_binding_changed')
        validate_probe_report(report['probe'], manifest['counts']['system1'], 1536)
        check_probe_pins(report['probe'], pins)

    def start(self, episode_id, conversion_sha256, confirm_sha256, lease_id, generation):
        if self.reserved or self.attempts >= 8 or confirm_sha256 != conversion_sha256:
            raise ValueError('episode_tokenizer_start_unavailable')
        manifest, converted = self.load(episode_id, conversion_sha256)
        native = self.native_inputs(episode_id, converted)
        self.attempts += 1
        current = {'state': 'pending', 'episode_id': episode_id,
                   'conversion_sha256': conversion_sha256, 'training_ready': False}
        self.current = current
        self.task = asyncio.create_task(self._run(current, manifest, native, lease_id, generation))
        return current.copy()

    async def _run(self, current, manifest, native, lease_id, generation):
        process = None
        engine, source, pins, raw, request = native
        try:
            runner = tokenizer_runner_pin()
            spawning = asyncio.create_task(asyncio.create_subprocess_exec(
                str(engine.python), str(REPO_ROOT / 'services/decider/tokenizer_probe.py'),
                str(engine.manifest), str(source), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                env={'PATH': '/usr/bin:/bin', 'HOME': str(Path.home()), 'HF_HUB_OFFLINE': '1',
                     'TRANSFORMERS_OFFLINE': '1', 'TOKENIZERS_PARALLELISM': 'false',
                     'CUDA_VISIBLE_DEVICES': '', 'OMP_NUM_THREADS': '1'}, limit=65537))
            try:
                process = await asyncio.shield(spawning)
            except asyncio.CancelledError:
                process = await spawning
                raise
            async with asyncio.timeout(120):
                process.stdin.write(request)
                await process.stdin.drain()
                process.stdin.close()
                output = bytearray()
                while chunk := await process.stdout.read(min(8192, 65537 - len(output))):
                    output.extend(chunk)
                    if len(output) > 65536:
                        raise ValueError('episode_tokenizer_output_bound')
                if await process.wait():
                    raise ValueError('episode_tokenizer_worker_failed')
            control = self.learning.scheduler.controller.state()
            if (self.current is not current or current['state'] != 'pending'
                    or control['owner'] != 'AGENT' or control['status'] != 'running'
                    or control['lease_id'] != lease_id or control['generation'] != generation
                    or self.learning.scheduler.reserved):
                raise ValueError('episode_tokenizer_control_changed')
            verified, converted = self.load(current['episode_id'], current['conversion_sha256'])
            if verified != manifest or tokenizer_runner_pin() != runner:
                raise ValueError('episode_tokenizer_source_changed')
            report = {'schema_version': '1.0', 'conversion_sha256': digest(manifest),
                      'input_sha256': hashlib.sha256(request).hexdigest(),
                      'deployment_manifest_sha256': hashlib.sha256(raw).hexdigest(),
                      'deployment_id': engine.identity['deployment_id'], 'runner_sha256': runner,
                      'probe': json.loads(output), 'synthetic': True, 'training_ready': False}
            self.validate_tokenizer(report, manifest, converted)
            with self.learning.store.directory(current['episode_id']) as descriptor:
                self.learning.store._put(descriptor, digest(manifest) + '-' + runner + '-tokenizer.json', report)
            current['state'] = 'verified'
        except asyncio.CancelledError:
            current['state'] = 'cancelled'
            raise
        except Exception:
            current['state'] = 'failed'
        finally:
            if process is not None:
                if process.returncode is None:
                    process.kill()
                await process.wait()

    async def cancel(self):
        if self.current is not None and self.current['state'] == 'pending':
            self.current['state'] = 'cancelled'
        task = self.task
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.done():
                    await task
