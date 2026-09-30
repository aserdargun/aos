import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from aos.contracts import REPO_ROOT, canonical
from aos.dataset import build_fixture_dataset, validator
from aos.dataset_bound_loss import DatasetLossReport, dataset_loss_preflight, verify_dataset_loss_report
from aos.dataset_preflight import dataset_input
from aos.dataset_readiness import readiness


class DatasetBoundLossTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.dataset = self.root / 'dataset'
        build_fixture_dataset(self.dataset, include_synthetic=True)
        self.manifest = self.root / 'synthetic-manifest.json'
        self.pins = {'checkpoint_revision': 'a' * 40, 'code_revision': 'b' * 40, 'model_files': {'model.safetensors': 'c' * 64}}
        self.manifest.write_text(canonical(self.pins))
        self.probe = {
            'mode': 'dataset_s1_forward_only_v1', 'checkpoint_revision': 'a' * 40, 'code_revision': 'b' * 40,
            'manifest_sha256': hashlib.sha256(self.manifest.read_bytes()).hexdigest(), 'weights_sha256': 'c' * 64,
            'input_sha256': dataset_input(self.dataset)[0]['input_sha256'], 'token_budget': 1536,
            'split_metrics': [{'split': 'train', 'layout': layout, 'decisions': 1, 'max_padded_tokens': 64,
                               'mean_nll': 0.5, 'correct_choices': 1} for layout in ('state_first', 'schema_first')],
            'peak_vram_allocated_bytes': 1, 'peak_vram_reserved_bytes': 1, 'elapsed_seconds': 1.0,
            'weights_loaded': True, 'parameters_unchanged': True, 'gradient_run': False, 'optimizer_run': False, 'training_ready': False}

    def fake_worker(self, arguments, **options):
        self.assertEqual(arguments[0], sys.executable)
        request = json.loads(options['input'])
        self.assertEqual(len(request['examples']), 1)
        self.assertEqual(request['split_counts'], {'train': 1, 'validation': 0, 'test': 0})
        self.assertEqual(options['env']['HF_HUB_OFFLINE'], '1')
        self.assertEqual(options['timeout'], 180)
        return subprocess.CompletedProcess(arguments, 0, canonical(self.probe).encode(), b'')

    def run_probe(self):
        return dataset_loss_preflight(self.dataset, self.manifest, Path(sys.executable), self.root,
                                      include_synthetic=True, forward_only=True)

    def report(self):
        with patch('aos.dataset_bound_loss.run_bounded', side_effect=self.fake_worker):
            return self.run_probe()

    def test_mocked_actual_dataset_input_and_read_only_verification(self):
        before = {path: path.read_bytes() for path in self.dataset.rglob('*') if path.is_file()}
        report = self.report()
        self.assertEqual(verify_dataset_loss_report(report.model_dump(), self.dataset, self.manifest), report)
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        self.assertNotIn(str(self.dataset), report.model_dump_json())
        self.assertFalse(report.training_ready)
        self.assertEqual(len(report.forward_report.split_metrics), 2)

    def test_explicit_flags_and_budget_reject_before_child(self):
        with patch('aos.dataset_bound_loss.run_bounded') as child:
            for synthetic, forward in ((False, False), (True, False), (False, True)):
                with self.assertRaises(ValueError):
                    dataset_loss_preflight(self.dataset, self.manifest, Path(sys.executable), self.root,
                                          include_synthetic=synthetic, forward_only=forward)
            for budget in (True, 63, 1537):
                with self.assertRaises(ValueError):
                    dataset_loss_preflight(self.dataset, self.manifest, Path(sys.executable), self.root,
                                          include_synthetic=True, forward_only=True, max_tokens=budget)
        child.assert_not_called()

    def test_changed_sources_and_worker_failures(self):
        for target in (self.manifest, self.dataset / 'manifest.json'):
            before = target.read_bytes()

            def change(arguments, **options):
                result = self.fake_worker(arguments, **options)
                target.write_bytes(before + b'\n')
                return result

            with patch('aos.dataset_bound_loss.run_bounded', side_effect=change), self.assertRaises(ValueError):
                self.run_probe()
            target.write_bytes(before)
        for output in (subprocess.CompletedProcess([], 1, b'', b'private stderr'), subprocess.CompletedProcess([], 0, b'not JSON', b'')):
            with patch('aos.dataset_bound_loss.run_bounded', return_value=output), self.assertRaises(ValueError):
                self.run_probe()

    def test_metrics_counts_pin_budget_and_training_overclaims_rejected(self):
        for field, value in (('weights_sha256', 'f' * 64), ('manifest_sha256', 'f' * 64), ('input_sha256', 'f' * 64),
                             ('token_budget', 128), ('training_ready', True), ('optimizer_run', True), ('gradient_run', True),
                             ('parameters_unchanged', False), ('elapsed_seconds', float('inf'))):
            changed = {**self.probe, field: value}
            with patch('aos.dataset_bound_loss.run_bounded', return_value=subprocess.CompletedProcess([], 0, json.dumps(changed), '')):
                with self.assertRaises(ValueError):
                    self.run_probe()
        for field, value in (('mean_nll', float('nan')), ('correct_choices', 2), ('decisions', 2), ('split', 'test')):
            changed = copy.deepcopy(self.probe)
            changed['split_metrics'][0][field] = value
            with patch('aos.dataset_bound_loss.run_bounded', return_value=subprocess.CompletedProcess([], 0, json.dumps(changed), '')):
                with self.assertRaises(ValueError):
                    self.run_probe()

    def test_schema_and_minimized_synthetic_fixture(self):
        fixture = json.loads((REPO_ROOT / 'examples/dataset_bound_loss.json').read_text())
        self.assertTrue(fixture['synthetic'])
        report = fixture['report']
        DatasetLossReport.model_validate(report)
        validator('dataset_bound_loss').validate(report)
        self.assertEqual(json.loads((REPO_ROOT / 'schemas/dataset_bound_loss.schema.json').read_text()), DatasetLossReport.model_json_schema())
        for field in ('training_ready', 'execution_authorized', 'promotion_authorized', 'supervisor_tokenizer_verified'):
            with self.assertRaises(ValueError):
                DatasetLossReport.model_validate({**report, field: True})
            self.assertFalse(validator('dataset_bound_loss').is_valid({**report, field: True}))
        with self.assertRaises(ValueError):
            DatasetLossReport.model_validate({**report, 'raw_input': 'secret'})

    def test_report_binds_dataset_deployment_and_runner(self):
        report = self.report().model_dump()
        for field in ('dataset_manifest_sha256', 'input_sha256', 'deployment_manifest_sha256', 'runner_sha256'):
            with self.assertRaises(ValueError):
                verify_dataset_loss_report({**report, field: 'f' * 64}, self.dataset, self.manifest)
        with patch('aos.dataset_bound_loss.run_bounded') as child:
            verify_dataset_loss_report(report, self.dataset, self.manifest)
        child.assert_not_called()

    def test_readiness_closes_only_dataset_loss_gap(self):
        path = self.root / 'report.json'
        path.write_text(canonical(self.report().model_dump()))
        with self.assertRaisesRegex(ValueError, 'deployment_pin_required'):
            readiness(self.dataset, dataset_loss_report=path)
        result = readiness(self.dataset, deployment=self.manifest, dataset_loss_report=path)
        self.assertEqual(result['reported_dataset_loss_binding'], 'matches')
        self.assertNotIn('forward_loss_report_missing', result['blockers'])
        for blocker in ('supervisor_tokenizer_missing', 'licensed_replay_missing', 'training_authorization_missing', 'gradient_optimizer_unverified'):
            self.assertIn(blocker, result['blockers'])
        self.assertFalse(result['training_ready'])

    def test_readiness_rejects_changed_dataset_report(self):
        report = self.report()
        path = self.root / 'report.json'
        path.write_text(canonical(report.model_dump()))
        changed = report.model_copy(update={'dataset_id': 'fixture-' + 'f' * 64})
        with patch('aos.dataset_bound_loss.verify_dataset_loss_report', return_value=changed):
            with self.assertRaisesRegex(ValueError, 'readiness_dataset_loss_changed'):
                readiness(self.dataset, deployment=self.manifest, dataset_loss_report=path)

    def test_symlink_and_tampered_dataset_rejected_before_child(self):
        alias = self.root / 'alias'
        alias.symlink_to(self.dataset, target_is_directory=True)
        with patch('aos.dataset_bound_loss.run_bounded') as child:
            with self.assertRaises(OSError):
                dataset_loss_preflight(alias, self.manifest, Path(sys.executable), self.root, include_synthetic=True, forward_only=True)
            (self.dataset / 'system1_choice/train.converted.jsonl').write_text('{}\n')
            with self.assertRaises(ValueError):
                self.run_probe()
        child.assert_not_called()

    def test_cli_missing_flags_has_no_effect(self):
        missing = self.root / 'missing'
        result = subprocess.run([sys.executable, '-m', 'aos.dataset_bound_loss', '--dataset', str(missing), '--manifest', str(missing),
                                 '--model-python', sys.executable, '--upstream-source', str(missing)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')
        self.assertFalse(missing.exists())


@unittest.skipUnless(os.environ.get('AOS_LOSS_TESTS') == '1', 'Real dataset forward/loss requires AOS_LOSS_TESTS=1')
class DatasetBoundLossRealTests(unittest.TestCase):
    def test_real_dataset_forward_split_binding_without_training(self):
        with tempfile.TemporaryDirectory(prefix='dataset-loss-', dir=REPO_ROOT / 'data') as temporary:
            dataset = Path(temporary) / 'dataset'
            build_fixture_dataset(dataset, include_synthetic=True)
            manifest = Path(os.environ.get('AOS_DECIDER_MANIFEST', REPO_ROOT / 'models/decider-manifest.json'))
            report = dataset_loss_preflight(dataset, manifest, Path(os.environ['AOS_MODEL_PYTHON']),
                                            Path(os.environ['AOS_DECIDER_DATA_SOURCE']), include_synthetic=True, forward_only=True)
            self.assertEqual(report.split_counts, {'train': 1, 'validation': 0, 'test': 0})
            self.assertEqual(sum(metric.decisions for metric in report.forward_report.split_metrics), 2)
            self.assertTrue(report.forward_report.parameters_unchanged)
            self.assertFalse(report.training_ready)
            self.assertEqual(verify_dataset_loss_report(report.model_dump(), dataset, manifest), report)
            print(canonical({'real_dataset_loss': report.model_dump()}))
