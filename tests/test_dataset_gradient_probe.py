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
from aos.dataset_gradient_probe import DatasetGradientReport, gradient_probe
from aos.dataset_preflight import dataset_input


class DatasetGradientProbeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.dataset = self.root / 'dataset'
        build_fixture_dataset(self.dataset, include_synthetic=True)
        self.manifest = self.root / 'manifest.json'
        self.pins = {'checkpoint_revision': 'a' * 40, 'code_revision': 'b' * 40,
                     'model_files': {'model.safetensors': 'c' * 64}}
        self.manifest.write_text(canonical(self.pins))
        self.worker = {
            'mode': 'dataset_s1_gradient_smoke_v1', 'checkpoint_revision': 'a' * 40,
            'code_revision': 'b' * 40, 'manifest_sha256': hashlib.sha256(self.manifest.read_bytes()).hexdigest(),
            'weights_sha256': 'c' * 64, 'input_sha256': dataset_input(self.dataset)[0]['input_sha256'],
            'train_decisions': 1, 'token_budget': 1536, 'adapter_target': 'last_mlp_down_proj',
            'adapter_rank': 4, 'adapter_parameter_count': 32768, 'seed': 42, 'optimizer_step_count': 1,
            'train_nll_before': 0.5, 'train_nll_after': 0.4, 'gradient_norm': 0.1,
            'peak_vram_allocated_bytes': 1024, 'peak_vram_reserved_bytes': 2048,
            'elapsed_seconds': 1.0, 'base_parameters_unchanged': True, 'base_gradient_count': 0,
            'adapter_parameters_changed': True, 'candidate_checkpoint_written': False,
            'training_ready': False}

    def fake_worker(self, arguments, **options):
        self.assertEqual(arguments[0], sys.executable)
        request = json.loads(options['input'])
        self.assertEqual(request['split_counts'], {'train': 1, 'validation': 0, 'test': 0})
        self.assertEqual(request['max_tokens'], 1536)
        self.assertEqual(options['env']['HF_HUB_OFFLINE'], '1')
        self.assertEqual(options['timeout'], 240)
        return subprocess.CompletedProcess(arguments, 0, canonical(self.worker).encode(), b'')

    def run_probe(self):
        return gradient_probe(self.dataset, self.manifest, Path(sys.executable),
                              include_synthetic=True, gradient_smoke=True)

    def test_dataset_bound_smoke_is_not_training_ready(self):
        before = {path: path.read_bytes() for path in self.dataset.rglob('*') if path.is_file()}
        with patch('aos.dataset_gradient_probe.run_bounded', side_effect=self.fake_worker):
            report = self.run_probe()
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        self.assertEqual(report.split_counts, {'train': 1, 'validation': 0, 'test': 0})
        self.assertFalse(report.training_ready)
        self.assertFalse(report.gradient_report.candidate_checkpoint_written)
        self.assertNotIn(str(self.dataset), report.model_dump_json())

    def test_explicit_flags_and_budget_reject_before_worker(self):
        with patch('aos.dataset_gradient_probe.run_bounded') as child:
            for synthetic, gradient in ((False, False), (True, False), (False, True)):
                with self.assertRaises(ValueError):
                    gradient_probe(self.dataset, self.manifest, Path(sys.executable),
                                   include_synthetic=synthetic, gradient_smoke=gradient)
            for budget in (True, 63, 1537):
                with self.assertRaises(ValueError):
                    gradient_probe(self.dataset, self.manifest, Path(sys.executable),
                                   include_synthetic=True, gradient_smoke=True, max_tokens=budget)
        child.assert_not_called()

    def test_changed_source_and_worker_failure_reject(self):
        for target in (self.manifest, self.dataset / 'manifest.json'):
            original = target.read_bytes()

            def mutate(arguments, **options):
                result = self.fake_worker(arguments, **options)
                target.write_bytes(original + b'\n')
                return result

            with patch('aos.dataset_gradient_probe.run_bounded', side_effect=mutate), self.assertRaises(ValueError):
                self.run_probe()
            target.write_bytes(original)
        for result in (subprocess.CompletedProcess([], 1, b'', b'private error'),
                       subprocess.CompletedProcess([], 0, b'not json', b'')):
            with patch('aos.dataset_gradient_probe.run_bounded', return_value=result), self.assertRaises(ValueError):
                self.run_probe()

    def test_worker_overclaims_and_binding_reject(self):
        for field, value in (('weights_sha256', 'f' * 64), ('input_sha256', 'f' * 64),
                             ('manifest_sha256', 'f' * 64), ('train_decisions', 2),
                             ('candidate_checkpoint_written', True), ('training_ready', True),
                             ('base_parameters_unchanged', False), ('base_gradient_count', 1),
                             ('adapter_parameters_changed', False), ('adapter_parameter_count', 1),
                             ('seed', 7), ('gradient_norm', float('nan'))):
            changed = {**self.worker, field: value}
            result = subprocess.CompletedProcess([], 0, json.dumps(changed).encode(), b'')
            with patch('aos.dataset_gradient_probe.run_bounded', return_value=result), self.assertRaises(ValueError):
                self.run_probe()

    def test_schema_and_illustrative_fixture(self):
        fixture = json.loads((REPO_ROOT / 'examples/dataset_gradient_probe.json').read_text())
        self.assertTrue(fixture['synthetic'])
        self.assertEqual(fixture['note'], 'Illustrative schema fixture; not a model measurement.')
        report = DatasetGradientReport.model_validate(fixture['report'])
        validator('dataset_gradient_probe').validate(report.model_dump())
        self.assertEqual(json.loads((REPO_ROOT / 'schemas/dataset_gradient_probe.schema.json').read_text()),
                         DatasetGradientReport.model_json_schema())
        for field in ('training_ready', 'execution_authorized', 'promotion_authorized'):
            with self.assertRaises(ValueError):
                DatasetGradientReport.model_validate({**report.model_dump(), field: True})
        changed = copy.deepcopy(report.model_dump())
        changed['gradient_report']['candidate_checkpoint_written'] = True
        with self.assertRaises(ValueError):
            DatasetGradientReport.model_validate(changed)

    def test_cli_missing_flags_or_dataset_has_no_effect(self):
        missing = self.root / 'missing'
        result = subprocess.run([sys.executable, '-m', 'aos.dataset_gradient_probe', '--dataset', str(missing),
                                 '--manifest', str(self.manifest), '--model-python', sys.executable],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')
        self.assertFalse(missing.exists())


@unittest.skipUnless(os.environ.get('AOS_GRADIENT_TESTS') == '1', 'Real CUDA gradient smoke requires AOS_GRADIENT_TESTS=1')
class DatasetGradientProbeRealTests(unittest.TestCase):
    def test_pinned_model_gradient_without_checkpoint(self):
        report = gradient_probe(REPO_ROOT / 'datasets/fixture-v002', REPO_ROOT / 'models/decider-manifest.json',
                                Path(os.environ['AOS_MODEL_PYTHON']), include_synthetic=True, gradient_smoke=True)
        self.assertEqual(report.gradient_report.adapter_parameter_count, 32768)
        self.assertGreater(report.gradient_report.gradient_norm, 0)
        self.assertTrue(report.gradient_report.base_parameters_unchanged)
        self.assertFalse(report.gradient_report.candidate_checkpoint_written)
        self.assertFalse(report.training_ready)


if __name__ == '__main__':
    unittest.main()
