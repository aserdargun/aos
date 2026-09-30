import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from aos.bounded_process import run_bounded
from aos.contracts import AOSFault, REPO_ROOT, canonical, digest
from aos.dataset import build_fixture_dataset, validator
from aos.dataset_adapter_candidate import (ARTIFACT_BYTES, ARTIFACT_MAGIC, WORKER_ENV,
                                           DatasetAdapterCandidateReport, create_candidate, runner_digest,
                                           verify_published_candidate)
from aos.dataset_adapter_runtime_probe import (AdapterRuntimeProbeReport, probe_runtime)
from aos.dataset_adapter_registry_inspect import (AdapterRegistryInspectionReport,
                                                  inspect_registered_candidate)
from aos.dataset_preflight import dataset_input
from aos.decision import DeciderEngine
from aos.registries import AdapterRegistry
from aos.storage import TrajectoryStore


class DatasetAdapterCandidateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='adapter-test-', dir=REPO_ROOT / 'data')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.dataset = self.root / 'dataset'
        build_fixture_dataset(self.dataset, include_synthetic=True)
        output = tempfile.TemporaryDirectory(prefix='adapter-output-', dir=REPO_ROOT / 'data')
        self.addCleanup(output.cleanup)
        self.output = Path(output.name)
        self.manifest = self.root / 'manifest.json'
        self.pins = {'source': 'Mapika/decider-2b', 'checkpoint_revision': 'a' * 40,
                     'tokenizer_revision': 'a' * 40, 'code_revision': 'b' * 40,
                     'model_files': {'model.safetensors': 'c' * 64}}
        self.manifest.write_text(canonical(self.pins))
        self.binding, _, _ = dataset_input(self.dataset)
        self.deployment_sha256 = hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        self.payload = (ARTIFACT_MAGIC + bytes.fromhex(self.binding['dataset_manifest_sha256'])
                        + bytes.fromhex(self.binding['input_sha256']) + bytes.fromhex(self.deployment_sha256)
                        + struct.pack('<III', 4, 6144, 2048) + b'\0' * (32768 * 4))
        self.assertEqual(len(self.payload), ARTIFACT_BYTES)
        self.artifact_sha256 = hashlib.sha256(self.payload).hexdigest()
        self.train = {'mode': 'dataset_s1_adapter_candidate_v1', 'checkpoint_revision': 'a' * 40,
                      'code_revision': 'b' * 40, 'manifest_sha256': self.deployment_sha256,
                      'weights_sha256': 'c' * 64, 'input_sha256': self.binding['input_sha256'],
                      'train_decisions': 1, 'token_budget': 1536, 'adapter_target': 'last_mlp_down_proj',
                      'adapter_rank': 4, 'adapter_parameter_count': 32768, 'seed': 42,
                      'optimizer_step_count': 1, 'train_nll_before': 0.5, 'train_nll_after': 0.4,
                      'gradient_norm': 0.1, 'peak_vram_allocated_bytes': 1024,
                      'peak_vram_reserved_bytes': 2048, 'elapsed_seconds': 1.0,
                      'base_parameters_unchanged': True, 'base_gradient_count': 0,
                      'adapter_parameters_changed': True, 'candidate_checkpoint_written': True,
                      'candidate_artifact_sha256': self.artifact_sha256,
                      'candidate_artifact_bytes': ARTIFACT_BYTES, 'training_ready': False}
        self.replay = {'mode': 'dataset_s1_adapter_replay_v1', 'checkpoint_revision': 'a' * 40,
                       'code_revision': 'b' * 40, 'manifest_sha256': self.deployment_sha256,
                       'weights_sha256': 'c' * 64, 'input_sha256': self.binding['input_sha256'],
                       'artifact_sha256': self.artifact_sha256, 'train_decisions': 1, 'token_budget': 1536,
                       'base_train_nll': 0.5, 'candidate_train_nll': 0.4,
                       'base_train_accuracy': 1.0, 'candidate_train_accuracy': 1.0,
                       'validation_decisions': 0, 'base_validation_nll': None, 'candidate_validation_nll': None,
                       'base_validation_accuracy': None, 'candidate_validation_accuracy': None,
                       'test_decisions': 0, 'base_test_nll': None, 'candidate_test_nll': None,
                       'base_test_accuracy': None, 'candidate_test_accuracy': None,
                       'candidate_loaded': True, 'base_parameters_unchanged': True,
                       'base_gradient_count': 0, 'training_ready': False}
        self.calls = []

    def fake_worker(self, arguments, **options):
        request = json.loads(options['input'])
        self.calls.append(request['mode'])
        self.assertEqual(arguments[0], sys.executable)
        self.assertEqual(request['split_counts'], {'train': 1, 'validation': 0, 'test': 0})
        self.assertEqual(options['env']['HF_HUB_OFFLINE'], '1')
        if len(self.calls) == 1:
            self.assertEqual(options['timeout'], 240)
            Path(arguments[-1]).write_bytes(self.payload)
            return subprocess.CompletedProcess(arguments, 0, canonical(self.train).encode(), b'')
        self.assertEqual(options['timeout'], 180)
        self.assertEqual(request['artifact_sha256'], self.artifact_sha256)
        return subprocess.CompletedProcess(arguments, 0, canonical(self.replay).encode(), b'')

    def run_candidate(self):
        return create_candidate(self.dataset, self.manifest, Path(sys.executable),
                                include_synthetic=True, write_candidate=True, output_root=self.output)

    def test_private_candidate_is_published_only_after_independent_replay(self):
        with patch('aos.dataset_adapter_candidate.run_bounded', side_effect=self.fake_worker):
            report = self.run_candidate()
        self.assertEqual(self.calls, ['dataset_s1_adapter_candidate_v1', 'dataset_s1_adapter_replay_v1'])
        self.assertEqual(report.runner_sha256, runner_digest())
        self.assertFalse(report.training_ready)
        self.assertFalse(report.promotion_authorized)
        artifact = self.output / (self.artifact_sha256 + '.aoslora')
        report_bytes = (canonical(report.model_dump()) + '\n').encode()
        report_file = self.output / (self.artifact_sha256 + '.' + hashlib.sha256(report_bytes).hexdigest() + '.json')
        self.assertEqual(artifact.read_bytes(), self.payload)
        self.assertEqual(artifact.stat().st_mode & 0o777, 0o600)
        self.assertEqual(report_file.read_bytes(), report_bytes)
        self.assertEqual(report_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(verify_published_candidate(self.dataset, self.manifest, report_file), report)
        self.assertEqual({path.name for path in self.output.iterdir()}, {artifact.name, report_file.name})
        self.assertNotIn(str(self.output), report.model_dump_json())
        self.calls.clear()
        with patch('aos.dataset_adapter_candidate.run_bounded', side_effect=self.fake_worker):
            self.assertEqual(self.run_candidate(), report)
        self.assertEqual({path.name for path in self.output.iterdir()}, {artifact.name, report_file.name})

    def test_published_report_verification_rejects_tampering(self):
        with patch('aos.dataset_adapter_candidate.run_bounded', side_effect=self.fake_worker):
            report = self.run_candidate()
        report_bytes = (canonical(report.model_dump()) + '\n').encode()
        report_file = self.output / (self.artifact_sha256 + '.' + hashlib.sha256(report_bytes).hexdigest() + '.json')
        artifact = self.output / (self.artifact_sha256 + '.aoslora')
        with self.assertRaises(ValueError):
            verify_published_candidate(self.dataset, self.manifest, self.root / 'nested' / report_file.name)
        report_file.chmod(0o644)
        with self.assertRaises(ValueError):
            verify_published_candidate(self.dataset, self.manifest, report_file)
        report_file.chmod(0o600)
        report_file.write_bytes(report_bytes.replace(b'"synthetic":true', b'"synthetic":false'))
        with self.assertRaises(ValueError):
            verify_published_candidate(self.dataset, self.manifest, report_file)
        report_file.write_bytes(report_bytes)
        artifact.write_bytes(self.payload[:-1] + b'\x01')
        with self.assertRaises(ValueError):
            verify_published_candidate(self.dataset, self.manifest, report_file)

    def test_cli_verifies_only_an_existing_published_pair(self):
        with patch('aos.dataset_adapter_candidate.run_bounded', side_effect=self.fake_worker):
            report = self.run_candidate()
        report_bytes = (canonical(report.model_dump()) + '\n').encode()
        report_file = self.output / (self.artifact_sha256 + '.' + hashlib.sha256(report_bytes).hexdigest() + '.json')
        result = subprocess.run([sys.executable, '-m', 'aos.dataset_adapter_candidate',
                                 '--dataset', str(self.dataset), '--manifest', str(self.manifest),
                                 '--verify-report', str(report_file)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), report.model_dump())
        refused = subprocess.run([sys.executable, '-m', 'aos.dataset_adapter_candidate',
                                  '--dataset', str(self.dataset), '--manifest', str(self.manifest),
                                  '--verify-report', str(report_file), '--write-candidate'],
                                 capture_output=True, text=True, timeout=10)
        self.assertEqual(refused.returncode, 1)

    def test_report_without_artifact_is_not_a_candidate(self):
        with patch('aos.dataset_adapter_candidate.run_bounded', side_effect=self.fake_worker):
            report = self.run_candidate()
        report_bytes = (canonical(report.model_dump()) + '\n').encode()
        report_file = self.output / (self.artifact_sha256 + '.' + hashlib.sha256(report_bytes).hexdigest() + '.json')
        (self.output / (self.artifact_sha256 + '.aoslora')).unlink()
        with self.assertRaises(OSError):
            verify_published_candidate(self.dataset, self.manifest, report_file)

    def test_artifact_collision_rolls_back_new_report(self):
        artifact = self.output / (self.artifact_sha256 + '.aoslora')
        artifact.write_bytes(self.payload[:-1] + b'\x01')
        artifact.chmod(0o600)
        with patch('aos.dataset_adapter_candidate.run_bounded', side_effect=self.fake_worker), self.assertRaises(ValueError):
            self.run_candidate()
        self.assertEqual([path.name for path in self.output.iterdir()], [artifact.name])

    def test_opt_in_and_private_root_reject_before_worker(self):
        with patch('aos.dataset_adapter_candidate.run_bounded') as child:
            for synthetic, write in ((False, False), (True, False), (False, True)):
                with self.assertRaises(ValueError):
                    create_candidate(self.dataset, self.manifest, Path(sys.executable),
                                     include_synthetic=synthetic, write_candidate=write, output_root=self.output)
            for budget in (True, 63, 1537):
                with self.assertRaises(ValueError):
                    create_candidate(self.dataset, self.manifest, Path(sys.executable),
                                     include_synthetic=True, write_candidate=True,
                                     output_root=self.output, max_tokens=budget)
            with self.assertRaises(ValueError):
                create_candidate(self.dataset, self.manifest, Path(sys.executable),
                                 include_synthetic=True, write_candidate=True,
                                 output_root=self.root / 'nested' / 'candidate')
        child.assert_not_called()

    def test_failed_replay_or_changed_source_never_publishes(self):
        for failure in ('replay_exit', 'replay_hash', 'replay_nll', 'source_change'):
            self.calls.clear()

            def broken(arguments, **options):
                result = self.fake_worker(arguments, **options)
                if len(self.calls) == 2:
                    if failure == 'replay_exit':
                        return subprocess.CompletedProcess(arguments, 1, b'', b'private failure')
                    if failure == 'replay_hash':
                        return subprocess.CompletedProcess(arguments, 0,
                                                           canonical({**self.replay, 'artifact_sha256': 'f' * 64}).encode(), b'')
                    if failure == 'replay_nll':
                        return subprocess.CompletedProcess(arguments, 0,
                                                           canonical({**self.replay, 'candidate_train_nll': 5.0}).encode(), b'')
                    self.manifest.write_bytes(self.manifest.read_bytes() + b'\n')
                return result

            original = self.manifest.read_bytes()
            with patch('aos.dataset_adapter_candidate.run_bounded', side_effect=broken), self.assertRaises(ValueError):
                self.run_candidate()
            self.manifest.write_bytes(original)
            self.assertEqual(list(self.output.iterdir()), [])

    def test_tampered_artifact_fails_before_replay(self):
        for payload in (self.payload[:-1], self.payload[:116] + b'\xff\xff\xff\x7f' + self.payload[120:],
                        b'X' + self.payload[1:]):
            self.calls.clear()

            def tampered(arguments, **options):
                result = self.fake_worker(arguments, **options)
                if len(self.calls) == 1:
                    Path(arguments[-1]).write_bytes(payload)
                return result

            with patch('aos.dataset_adapter_candidate.run_bounded', side_effect=tampered), self.assertRaises(ValueError):
                self.run_candidate()
            self.assertEqual(self.calls, ['dataset_s1_adapter_candidate_v1'])
            self.assertEqual(list(self.output.iterdir()), [])

    def test_schema_and_illustrative_fixture(self):
        fixture = json.loads((REPO_ROOT / 'examples/dataset_adapter_candidate.json').read_text())
        self.assertTrue(fixture['synthetic'])
        self.assertEqual(fixture['note'], 'Illustrative schema fixture; not a model measurement.')
        report = DatasetAdapterCandidateReport.model_validate(fixture['report'])
        validator('dataset_adapter_candidate').validate(report.model_dump())
        self.assertEqual(json.loads((REPO_ROOT / 'schemas/dataset_adapter_candidate.schema.json').read_text()),
                         DatasetAdapterCandidateReport.model_json_schema())

    def test_split_metrics_require_nonempty_split(self):
        report = json.loads((REPO_ROOT / 'examples/dataset_adapter_candidate.json').read_text())['report']
        report['split_counts']['validation'] = 1
        with self.assertRaises(ValueError):
            DatasetAdapterCandidateReport.model_validate(report)
        report['replay_report'].update(validation_decisions=1, base_validation_nll=0.3,
                                       candidate_validation_nll=0.2, base_validation_accuracy=1.0,
                                       candidate_validation_accuracy=0.0)
        validated = DatasetAdapterCandidateReport.model_validate(report)
        validator('dataset_adapter_candidate').validate(validated.model_dump())
        report['replay_report']['candidate_validation_nll'] = None
        with self.assertRaises(ValueError):
            DatasetAdapterCandidateReport.model_validate(report)
        report['replay_report']['candidate_validation_nll'] = float('inf')
        with self.assertRaises(ValueError):
            DatasetAdapterCandidateReport.model_validate(report)
        report['replay_report']['candidate_validation_nll'] = 0.2
        report['replay_report']['candidate_validation_accuracy'] = 2.0
        with self.assertRaises(ValueError):
            DatasetAdapterCandidateReport.model_validate(report)

    def test_isolated_runtime_probe_requires_published_candidate_and_worker_binding(self):
        with patch('aos.dataset_adapter_candidate.run_bounded', side_effect=self.fake_worker):
            candidate = self.run_candidate()
        report_bytes = (canonical(candidate.model_dump()) + '\n').encode()
        report_file = self.output / (self.artifact_sha256 + '.' + hashlib.sha256(report_bytes).hexdigest() + '.json')
        worker = {'mode': 'synthetic_adapter_runtime_probe_v1',
                  'artifact_sha256': self.artifact_sha256,
                  'deployment_manifest_sha256': self.deployment_sha256,
                  'input_sha256': self.binding['input_sha256'],
                  'train_sample_index': 0, 'option_count': 2,
                  'base': {'selected_index': 0, 'gold_probability': 0.9},
                  'candidate': {'selected_index': 0, 'gold_probability': 0.9},
                  'split_results': {'train': {'count': 1, 'base_correct': 1, 'candidate_correct': 1},
                                    'validation': {'count': 0, 'base_correct': 0, 'candidate_correct': 0},
                                    'test': {'count': 0, 'base_correct': 0, 'candidate_correct': 0}},
                  'adapter_loaded': True, 'adapter_hook_calls': 1,
                  'base_parameters_unchanged': True,
                  'active_deployment_changed': False}
        with self.assertRaises(ValueError):
            probe_runtime(self.dataset, self.manifest, report_file, Path(sys.executable))
        missing_opt_in = subprocess.run(
            [sys.executable, '-m', 'aos.dataset_adapter_runtime_probe',
             '--dataset', str(self.dataset), '--manifest', str(self.manifest),
             '--report', str(report_file), '--model-python', sys.executable],
            capture_output=True, text=True, timeout=5)
        self.assertEqual(missing_opt_in.returncode, 1)
        self.assertEqual(missing_opt_in.stdout, '')
        with patch('aos.dataset_adapter_runtime_probe.run_bounded', return_value=subprocess.CompletedProcess(
                [], 0, canonical(worker).encode(), b'')) as subprocess_call:
            report = probe_runtime(self.dataset, self.manifest, report_file, Path(sys.executable),
                                   include_synthetic=True, runtime_smoke=True)
        self.assertTrue(report.runtime_compatible)
        self.assertGreaterEqual(report.adapter_hook_calls, 1)
        self.assertFalse(report.active_deployment_changed)
        self.assertEqual(report.candidate_report_sha256, hashlib.sha256(report_bytes).hexdigest())
        self.assertEqual(json.loads(subprocess_call.call_args.kwargs['input'])['gold_option_ids'], ['option-0'])
        self.assertEqual(report.split_results['train'].base_correct, 1)
        validator('dataset_adapter_runtime_probe').validate(report.model_dump())
        self.assertEqual(json.loads((REPO_ROOT / 'schemas/dataset_adapter_runtime_probe.schema.json').read_text()),
                         AdapterRuntimeProbeReport.model_json_schema())
        fixture = json.loads((REPO_ROOT / 'examples/dataset_adapter_runtime_probe.json').read_text())
        self.assertEqual(fixture['note'], 'Illustrative schema fixture; not a model measurement.')
        validator('dataset_adapter_runtime_probe').validate(
            AdapterRuntimeProbeReport.model_validate(fixture['report']).model_dump())
        for changed in ({**worker, 'artifact_sha256': 'f' * 64},
                        {**worker, 'adapter_hook_calls': 0},
                        {**worker, 'candidate': {'selected_index': 2, 'gold_probability': 0.9}},
                        {**worker, 'base': {'selected_index': 0, 'gold_probability': 1.1}},
                        {**worker, 'split_results': {**worker['split_results'], 'train': {
                            'count': 2, 'base_correct': 1, 'candidate_correct': 1}}},
                        {**worker, 'split_results': {**worker['split_results'], 'train': {
                            'count': 1, 'base_correct': 2, 'candidate_correct': 1}}}):
            with patch('aos.dataset_adapter_runtime_probe.run_bounded', return_value=subprocess.CompletedProcess(
                    [], 0, canonical(changed).encode(), b'')), self.assertRaises(ValueError):
                probe_runtime(self.dataset, self.manifest, report_file, Path(sys.executable),
                              include_synthetic=True, runtime_smoke=True)

    def test_registry_records_only_disabled_synthetic_experiment(self):
        with patch('aos.dataset_adapter_candidate.run_bounded', side_effect=self.fake_worker):
            candidate = self.run_candidate()
        report_bytes = (canonical(candidate.model_dump()) + '\n').encode()
        report_file = self.output / (self.artifact_sha256 + '.'
                                     + hashlib.sha256(report_bytes).hexdigest() + '.json')
        identity = {'deployment_id': 'decider-' + digest(self.pins),
                    'kind': 'decider_native_worker', 'real_model': True, 'pins': self.pins}
        store = TrajectoryStore(self.root / 'registry.db')
        self.addCleanup(store.close)
        registry = AdapterRegistry(store)
        adapter_id, deployment_id = registry.record_synthetic_experiment(
            identity, self.dataset, self.manifest, report_file)
        self.assertEqual((adapter_id, deployment_id), registry.record_synthetic_experiment(
            identity, self.dataset, self.manifest, report_file))
        model = store.connection.execute('SELECT * FROM models').fetchone()
        adapter = store.connection.execute('SELECT * FROM adapters').fetchone()
        deployment = store.connection.execute('SELECT * FROM deployments').fetchone()
        self.assertEqual((model['enabled'], adapter['compatibility'], deployment['status']),
                         (0, 'unknown', 'EXPERIMENTAL'))
        self.assertEqual(adapter['sha256'], self.artifact_sha256)
        self.assertEqual(adapter['base_sha256'], self.pins['model_files']['model.safetensors'])
        self.assertEqual(deployment['adapter_id'], adapter_id)
        self.assertEqual(json.loads(deployment['config_json'])['runtime_enabled'], False)
        self.assertEqual(store.connection.execute('SELECT count(*) FROM active_deployments').fetchone()[0], 0)
        self.assertEqual(store.connection.execute('SELECT count(*) FROM deployment_events').fetchone()[0], 0)
        reader = TrajectoryStore(self.root / 'registry.db', readonly=True)
        try:
            inspected = AdapterRegistry(reader).inspect_synthetic_experiment(
                identity, self.dataset, self.manifest, report_file)
        finally:
            reader.close()
        self.assertEqual((inspected['adapter_id'], inspected['deployment_id'], inspected['status']),
                         (adapter_id, deployment_id, 'available_experimental'))
        self.assertFalse(inspected['runtime_enabled'])
        self.assertFalse(inspected['promotion_authorized'])
        inspected_report = inspect_registered_candidate(self.root / 'registry.db', self.dataset,
                                                        self.manifest, report_file)
        self.assertEqual(inspected_report.status, 'available_experimental')
        self.assertEqual(inspected_report.adapter_id, adapter_id)
        validator('dataset_adapter_registry_inspect').validate(inspected_report.model_dump())
        database_files = [path for path in (self.root / 'registry.db', self.root / 'registry.db-wal')
                          if path.exists()]
        database_hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest()
                           for path in database_files}
        command = subprocess.run(
            [sys.executable, '-m', 'aos.dataset_adapter_registry_inspect',
             '--database', str(self.root / 'registry.db'), '--dataset', str(self.dataset),
             '--manifest', str(self.manifest), '--report', str(report_file)],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(command.returncode, 0, command.stderr)
        self.assertEqual(json.loads(command.stdout), inspected_report.model_dump())
        self.assertNotIn(str(self.root), command.stdout)
        self.assertEqual({path: hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in database_files}, database_hashes)
        with self.assertRaises(ValueError):
            registry.record_synthetic_experiment({**identity, 'deployment_id': 'wrong'},
                                                 self.dataset, self.manifest, report_file)
        with self.assertRaises(ValueError):
            registry.record_synthetic_experiment({**identity, 'real_model': False},
                                                 self.dataset, self.manifest, report_file)
        report_file.chmod(0o644)
        with self.assertRaises(ValueError):
            registry.record_synthetic_experiment(identity, self.dataset, self.manifest, report_file)
        with self.assertRaises(ValueError):
            registry.inspect_synthetic_experiment(identity, self.dataset, self.manifest, report_file)
        report_file.chmod(0o600)
        store.connection.execute('UPDATE deployments SET status=? WHERE deployment_id=?',
                                 ('CANDIDATE', deployment_id))
        store.connection.commit()
        with self.assertRaises(AOSFault):
            registry.record_synthetic_experiment(identity, self.dataset, self.manifest, report_file)
        with self.assertRaises(ValueError):
            registry.inspect_synthetic_experiment(identity, self.dataset, self.manifest, report_file)
        self.assertEqual(store.connection.execute('SELECT count(*) FROM adapters').fetchone()[0], 1)
        store.connection.execute('UPDATE deployments SET status=? WHERE deployment_id=?',
                                 ('EXPERIMENTAL', deployment_id))
        store.connection.execute('UPDATE models SET metadata_json=? WHERE model_id=?',
                                 ('{}', model['model_id']))
        store.connection.commit()
        with self.assertRaises(ValueError):
            registry.inspect_synthetic_experiment(identity, self.dataset, self.manifest, report_file)
        store.connection.execute('UPDATE models SET metadata_json=? WHERE model_id=?',
                                 (model['metadata_json'], model['model_id']))
        store.connection.commit()
        (self.output / (self.artifact_sha256 + '.aoslora')).unlink()
        with self.assertRaises(OSError):
            registry.inspect_synthetic_experiment(identity, self.dataset, self.manifest, report_file)
        refused = subprocess.run(
            [sys.executable, '-m', 'aos.dataset_adapter_registry_inspect',
             '--database', str(self.root / 'registry.db'), '--dataset', str(self.dataset),
             '--manifest', str(self.manifest), '--report', str(report_file)],
            capture_output=True, text=True, timeout=10)
        self.assertEqual((refused.returncode, refused.stdout), (1, ''))

    def test_registry_inspection_schema_and_illustrative_fixture(self):
        fixture = json.loads((REPO_ROOT / 'examples/dataset_adapter_registry_inspect.json').read_text())
        self.assertIs(fixture['synthetic'], True)
        self.assertEqual(fixture['note'], 'Illustrative schema fixture; not a registered model measurement.')
        report = AdapterRegistryInspectionReport.model_validate(fixture['report'])
        validator('dataset_adapter_registry_inspect').validate(report.model_dump())
        self.assertEqual(json.loads((REPO_ROOT / 'schemas/dataset_adapter_registry_inspect.schema.json').read_text()),
                         AdapterRegistryInspectionReport.model_json_schema())

    def test_cli_missing_flags_has_no_effect(self):
        result = subprocess.run([sys.executable, '-m', 'aos.dataset_adapter_candidate', '--dataset', str(self.dataset),
                                 '--manifest', str(self.manifest), '--model-python', sys.executable,
                                 '--output-root', str(self.output)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')
        self.assertEqual(list(self.output.iterdir()), [])


@unittest.skipUnless(os.environ.get('AOS_ADAPTER_CANDIDATE_TESTS') == '1',
                     'Real CUDA candidate requires AOS_ADAPTER_CANDIDATE_TESTS=1')
class DatasetAdapterCandidateRealTests(unittest.TestCase):
    def test_real_train_save_and_independent_reload(self):
        with tempfile.TemporaryDirectory(prefix='adapter-real-', dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            report = create_candidate(REPO_ROOT / 'datasets/fixture-v002',
                                      REPO_ROOT / 'models/decider-manifest.json', Path(os.environ['AOS_MODEL_PYTHON']),
                                      include_synthetic=True, write_candidate=True, output_root=root)
            artifact = root / (report.train_report.candidate_artifact_sha256 + '.aoslora')
            self.assertEqual(artifact.stat().st_size, ARTIFACT_BYTES)
            report_bytes = (canonical(report.model_dump()) + '\n').encode()
            report_file = root / (report.train_report.candidate_artifact_sha256 + '.'
                                  + hashlib.sha256(report_bytes).hexdigest() + '.json')
            self.assertEqual(verify_published_candidate(REPO_ROOT / 'datasets/fixture-v002',
                                                       REPO_ROOT / 'models/decider-manifest.json', report_file), report)
            runtime_result = subprocess.run(
                [sys.executable, '-m', 'aos.dataset_adapter_runtime_probe',
                 '--dataset', str(REPO_ROOT / 'datasets/fixture-v002'),
                 '--manifest', str(REPO_ROOT / 'models/decider-manifest.json'),
                 '--report', str(report_file), '--model-python', os.environ['AOS_MODEL_PYTHON'],
                 '--include-synthetic', '--runtime-smoke'],
                capture_output=True, text=True, timeout=180)
            self.assertEqual(runtime_result.returncode, 0, runtime_result.stderr)
            runtime_probe = AdapterRuntimeProbeReport.model_validate_json(runtime_result.stdout)
            self.assertTrue(runtime_probe.runtime_compatible)
            self.assertGreaterEqual(runtime_probe.adapter_hook_calls, 1)
            self.assertFalse(runtime_probe.active_deployment_changed)
            self.assertEqual(runtime_probe.artifact_sha256, report.train_report.candidate_artifact_sha256)
            store = TrajectoryStore(root / 'adapter-registry.sqlite')
            try:
                adapter_id, deployment_id = AdapterRegistry(store).record_synthetic_experiment(
                    DeciderEngine(REPO_ROOT / 'models/decider-manifest.json', Path(os.environ['AOS_MODEL_PYTHON'])).identity,
                    REPO_ROOT / 'datasets/fixture-v002', REPO_ROOT / 'models/decider-manifest.json', report_file)
                self.assertEqual(store.connection.execute(
                    'SELECT sha256 FROM adapters WHERE adapter_id=?', (adapter_id,)).fetchone()[0],
                                 report.train_report.candidate_artifact_sha256)
                self.assertEqual(store.connection.execute(
                    'SELECT status FROM deployments WHERE deployment_id=?', (deployment_id,)).fetchone()[0],
                                 'EXPERIMENTAL')
                self.assertEqual(store.connection.execute('SELECT count(*) FROM active_deployments').fetchone()[0], 0)
                inspected = AdapterRegistry(store).inspect_synthetic_experiment(
                    DeciderEngine(REPO_ROOT / 'models/decider-manifest.json', Path(os.environ['AOS_MODEL_PYTHON'])).identity,
                    REPO_ROOT / 'datasets/fixture-v002', REPO_ROOT / 'models/decider-manifest.json', report_file)
                self.assertEqual(inspected['status'], 'available_experimental')
                self.assertEqual(inspected['artifact_sha256'], report.train_report.candidate_artifact_sha256)
                inspected_report = inspect_registered_candidate(
                    root / 'adapter-registry.sqlite', REPO_ROOT / 'datasets/fixture-v002',
                    REPO_ROOT / 'models/decider-manifest.json', report_file)
                self.assertEqual(inspected_report.status, 'available_experimental')
                command = subprocess.run(
                    [sys.executable, '-m', 'aos.dataset_adapter_registry_inspect',
                     '--database', str(root / 'adapter-registry.sqlite'),
                     '--dataset', str(REPO_ROOT / 'datasets/fixture-v002'),
                     '--manifest', str(REPO_ROOT / 'models/decider-manifest.json'),
                     '--report', str(report_file)],
                    capture_output=True, text=True, timeout=10)
                self.assertEqual(command.returncode, 0, command.stderr)
                self.assertEqual(json.loads(command.stdout), inspected_report.model_dump())
            finally:
                store.close()
            self.assertTrue(report.replay_report.candidate_loaded)
            self.assertEqual(report.replay_report.validation_decisions, 0)
            self.assertIsNone(report.replay_report.base_validation_nll)
            self.assertIsNone(report.replay_report.base_validation_accuracy)
            self.assertGreaterEqual(report.replay_report.base_train_accuracy, 0)
            self.assertFalse(report.training_ready)
            binding, examples, _ = dataset_input(REPO_ROOT / 'datasets/fixture-v002')
            validation = json.loads(canonical(examples[0]))
            validation['context'] = 'Synthetic validation-only request: list authorized filenames.'
            test_example = json.loads(canonical(examples[0]))
            test_example['context'] = 'Synthetic test-only request: list permitted workspace filenames.'
            evaluation_examples = examples + [validation, test_example]
            payload = artifact.read_bytes()
            evaluation_payload = payload[:40] + bytes.fromhex(digest(evaluation_examples)) + payload[72:]
            evaluation_artifact = root / 'split-probe.aoslora'
            descriptor = os.open(evaluation_artifact, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(evaluation_payload)
            request = {'mode': 'dataset_s1_adapter_replay_v1', 'examples': evaluation_examples,
                       'split_counts': {'train': 1, 'validation': 1, 'test': 1}, 'max_tokens': 1536,
                       'dataset_manifest_sha256': binding['dataset_manifest_sha256'],
                       'artifact_sha256': hashlib.sha256(evaluation_payload).hexdigest(),
                       'deployment_manifest_sha256': report.deployment_manifest_sha256}
            result = run_bounded([os.environ['AOS_MODEL_PYTHON'],
                                  str(REPO_ROOT / 'services/decider/dataset_adapter_replay.py'),
                                  str(REPO_ROOT / 'models/decider-manifest.json'), str(evaluation_artifact)],
                                 input=canonical(request).encode(), env=WORKER_ENV, timeout=180)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
            split_probe = json.loads(result.stdout)
            self.assertEqual(split_probe['validation_decisions'], 1)
            self.assertGreaterEqual(split_probe['base_validation_nll'], 0)
            self.assertGreaterEqual(split_probe['candidate_validation_nll'], 0)
            self.assertGreaterEqual(split_probe['base_validation_accuracy'], 0)
            self.assertLessEqual(split_probe['candidate_validation_accuracy'], 1)
            self.assertEqual(split_probe['test_decisions'], 1)
            self.assertGreaterEqual(split_probe['base_test_nll'], 0)
            self.assertGreaterEqual(split_probe['candidate_test_nll'], 0)
            self.assertGreaterEqual(split_probe['base_test_accuracy'], 0)
            self.assertLessEqual(split_probe['candidate_test_accuracy'], 1)
            decisions = []
            gold_option_ids = []
            for example in evaluation_examples:
                question = example['qs'][0]
                decisions.append({'state': example['context'], 'question': question['text'],
                                  'options': [{'id': 'option-' + str(index), 'label': label}
                                              for index, label in enumerate(question['options'])]})
                gold_option_ids.append('option-' + str(question['gold']))
            runtime_request = {'mode': 'synthetic_adapter_runtime_probe_v1',
                               'artifact_sha256': hashlib.sha256(evaluation_payload).hexdigest(),
                               'dataset_manifest_sha256': binding['dataset_manifest_sha256'],
                               'input_sha256': digest(evaluation_examples),
                               'deployment_manifest_sha256': report.deployment_manifest_sha256,
                               'decisions': decisions, 'gold_option_ids': gold_option_ids,
                               'split_counts': {'train': 1, 'validation': 1, 'test': 1}}
            runtime_result = run_bounded([os.environ['AOS_MODEL_PYTHON'],
                                          str(REPO_ROOT / 'services/decider/dataset_adapter_runtime_probe.py'),
                                          str(REPO_ROOT / 'models/decider-manifest.json'), str(evaluation_artifact)],
                                         input=canonical(runtime_request).encode(), env=WORKER_ENV, timeout=300)
            self.assertEqual(runtime_result.returncode, 0, runtime_result.stderr.decode(errors='replace'))
            runtime_splits = json.loads(runtime_result.stdout)['split_results']
            self.assertEqual({split: result['count'] for split, result in runtime_splits.items()},
                             {'train': 1, 'validation': 1, 'test': 1})
            self.assertTrue(all(0 <= result[source] <= 1 for result in runtime_splits.values()
                                for source in ('base_correct', 'candidate_correct')))
        self.assertFalse(root.exists())


if __name__ == '__main__':
    unittest.main()
