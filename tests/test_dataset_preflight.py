import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from aos.contracts import REPO_ROOT, canonical, digest
from aos.dataset import build_fixture_dataset, validator
from aos.dataset_preflight import DatasetTokenizerReport, bounded_file, dataset_input, dataset_tokenizer_preflight, verify_dataset_tokenizer_report
from aos.dataset_readiness import readiness


class DatasetPreflightTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.dataset = self.root / 'dataset'
        build_fixture_dataset(self.dataset, include_synthetic=True)
        self.probe = json.loads((REPO_ROOT / 'examples/dataset_tokenizer_probe.json').read_text())['report']
        self.probe.update(examples=1, variants=40)
        converter = json.loads((REPO_ROOT / 'examples/dataset_converter_pin.json').read_text())
        self.probe['core_sha256'] = converter['files']['core.py']
        self.pins = {key: self.probe[key] for key in ('checkpoint_revision', 'tokenizer_revision', 'code_revision', 'code_files', 'dependencies')}
        self.pins['model_files'] = self.probe['tokenizer_files']
        self.manifest = self.root / 'synthetic-deployment.json'
        self.manifest.write_text(canonical(self.pins))

    def fake_worker(self, arguments, **options):
        self.assertEqual(arguments[0], sys.executable)
        request = json.loads(options['input'])
        self.assertEqual(len(request['examples']), 1)
        self.assertEqual(options['env']['CUDA_VISIBLE_DEVICES'], '')
        self.assertEqual(options['env']['HF_HUB_OFFLINE'], '1')
        self.assertEqual(set(request['examples'][0]), {'task', 'context', 'qs'})
        return subprocess.CompletedProcess(arguments, 0, canonical(self.probe), '')

    def run_probe(self):
        return dataset_tokenizer_preflight(self.dataset, self.manifest, Path(sys.executable), self.root, include_synthetic=True)

    def test_mocked_worker_uses_dataset_not_four_canonical_examples(self):
        before = {path: path.read_bytes() for path in self.dataset.rglob('*') if path.is_file()}
        with patch('aos.dataset_preflight.run_bounded', side_effect=self.fake_worker):
            report = self.run_probe()
        self.assertEqual(report.split_counts, {'train': 1, 'validation': 0, 'test': 0})
        self.assertEqual(report.tokenizer_report['examples'], 1)
        self.assertFalse(report.training_ready)
        self.assertFalse(report.forward_loss_verified)
        self.assertEqual(verify_dataset_tokenizer_report(report.model_dump(), self.dataset, self.manifest), report)
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        self.assertNotIn(str(self.dataset), report.model_dump_json())

    def test_opt_in_and_budget_fail_before_worker(self):
        with patch('aos.dataset_preflight.run_bounded') as worker:
            with self.assertRaises(ValueError):
                dataset_tokenizer_preflight(self.dataset, self.manifest, Path(sys.executable), self.root)
            for budget in (True, 63, 1537):
                with self.assertRaises(ValueError):
                    dataset_tokenizer_preflight(self.dataset, self.manifest, Path(sys.executable), self.root, include_synthetic=True, max_tokens=budget)
        worker.assert_not_called()

    def test_tampered_dataset_and_rehashed_wrong_gold_are_rejected(self):
        path = self.dataset / 'system1_choice/train.converted.jsonl'
        payload = json.loads(path.read_text())
        payload['qs'][0]['gold'] = (payload['qs'][0]['gold'] + 1) % len(payload['qs'][0]['options'])
        path.write_text(canonical(payload) + '\n')
        with patch('aos.dataset_preflight.run_bounded') as worker:
            with self.assertRaises(ValueError):
                self.run_probe()
            manifest_path = self.dataset / 'manifest.json'
            manifest = json.loads(manifest_path.read_text())
            manifest['files']['system1_choice/train.converted.jsonl'].update(sha256=hashlib.sha256(path.read_bytes()).hexdigest(), bytes=path.stat().st_size)
            manifest['dataset_id'] = 'fixture-' + digest({key: value for key, value in manifest.items() if key != 'dataset_id'})
            manifest_path.write_text(canonical(manifest))
            with self.assertRaisesRegex(ValueError, 'converter_mismatch'):
                self.run_probe()
        worker.assert_not_called()

    def test_changed_dataset_or_manifest_during_worker_is_rejected(self):
        for target in (self.dataset / 'manifest.json', self.manifest):
            before = target.read_bytes()

            def changed(arguments, **options):
                result = self.fake_worker(arguments, **options)
                target.write_bytes(before + b'\n')
                return result

            with patch('aos.dataset_preflight.run_bounded', side_effect=changed), self.assertRaises(ValueError):
                self.run_probe()
            target.write_bytes(before)

    def test_worker_failure_pin_mismatch_and_overclaim_are_rejected(self):
        for field, value in (('training_ready', True), ('weights_loaded', True), ('examples', 4), ('code_revision', 'f' * 40)):
            changed = {**self.probe, field: value}
            with patch('aos.dataset_preflight.run_bounded', return_value=subprocess.CompletedProcess([], 0, canonical(changed), '')):
                with self.assertRaises(ValueError):
                    self.run_probe()
        with patch('aos.dataset_preflight.run_bounded', return_value=subprocess.CompletedProcess([], 1, '', 'private failure')):
            with self.assertRaisesRegex(ValueError, 'dataset_tokenizer_failed'):
                self.run_probe()

    def test_symlinks_hardlinks_fifo_and_oversize_are_rejected(self):
        alias = self.root / 'alias'
        alias.symlink_to(self.dataset, target_is_directory=True)
        with self.assertRaises(OSError):
            dataset_input(alias)
        hardlink = self.root / 'hardlink'
        os.link(self.manifest, hardlink)
        with self.assertRaises(ValueError):
            bounded_file(self.manifest)
        hardlink.unlink()
        fifo = self.root / 'fifo'
        os.mkfifo(fifo)
        with self.assertRaises(ValueError):
            bounded_file(fifo)
        with self.assertRaises(ValueError):
            bounded_file(self.manifest, 1)

    def test_report_verifier_rejects_other_dataset_input_deployment_or_runner(self):
        with patch('aos.dataset_preflight.run_bounded', side_effect=self.fake_worker):
            report = self.run_probe().model_dump()
        for field in ('dataset_manifest_sha256', 'input_sha256', 'deployment_manifest_sha256', 'runner_sha256'):
            with self.assertRaises(ValueError):
                verify_dataset_tokenizer_report({**report, field: 'f' * 64}, self.dataset, self.manifest)
        with patch('aos.dataset_preflight.run_bounded') as worker:
            verify_dataset_tokenizer_report(report, self.dataset, self.manifest)
        worker.assert_not_called()

    def test_canonical_fixture_schema_and_authority_constraints(self):
        fixture = json.loads((REPO_ROOT / 'examples/dataset_preflight.json').read_text())
        self.assertTrue(fixture['synthetic'])
        report = fixture['report']
        DatasetTokenizerReport.model_validate(report)
        validator('dataset_preflight').validate(report)
        self.assertEqual(json.loads((REPO_ROOT / 'schemas/dataset_preflight.schema.json').read_text()), DatasetTokenizerReport.model_json_schema())
        for field in ('training_ready', 'forward_loss_verified', 'supervisor_tokenizer_verified', 'execution_authorized', 'promotion_authorized'):
            with self.assertRaises(ValueError):
                DatasetTokenizerReport.model_validate({**report, field: True})
            self.assertFalse(validator('dataset_preflight').is_valid({**report, field: True}))
        nested = {**report, 'tokenizer_report': {**report['tokenizer_report'], 'private_input': 'secret'}}
        self.assertFalse(validator('dataset_preflight').is_valid(nested))

    def test_cli_missing_opt_in_never_creates_files(self):
        missing = self.root / 'missing'
        result = subprocess.run([sys.executable, '-m', 'aos.dataset_preflight', '--dataset', str(missing), '--manifest', str(missing),
                                 '--model-python', sys.executable, '--upstream-source', str(missing)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')
        self.assertFalse(missing.exists())

    def test_dataset_readiness_matches_only_s1_tokenizer_not_training(self):
        with patch('aos.dataset_preflight.run_bounded', side_effect=self.fake_worker):
            report = self.run_probe()
        report_path = self.root / 'synthetic-bound-report.json'
        report_path.write_text(canonical(report.model_dump()))
        with self.assertRaisesRegex(ValueError, 'deployment_pin_required'):
            readiness(self.dataset, dataset_tokenizer_report=report_path)
        inventory = readiness(self.dataset, deployment=self.manifest, dataset_tokenizer_report=report_path)
        self.assertEqual(inventory['reported_dataset_tokenizer_binding'], 'matches')
        self.assertNotIn('dataset_bound_preflight_missing', inventory['blockers'])
        for blocker in ('dataset_bound_forward_loss_missing', 'supervisor_tokenizer_missing', 'training_authorization_missing', 'licensed_replay_missing'):
            self.assertIn(blocker, inventory['blockers'])
        self.assertFalse(inventory['training_ready'])

    def test_readiness_rejects_changed_dataset_and_reuses_verified_deployment_hash(self):
        with patch('aos.dataset_preflight.run_bounded', side_effect=self.fake_worker):
            report = self.run_probe()
        report_path = self.root / 'synthetic-bound-report.json'
        report_path.write_text(canonical(report.model_dump()))
        changed = report.model_copy(update={'dataset_id': 'fixture-' + 'f' * 64})
        with patch('aos.dataset_preflight.verify_dataset_tokenizer_report', return_value=changed):
            with self.assertRaisesRegex(ValueError, 'readiness_dataset_preflight_changed'):
                readiness(self.dataset, deployment=self.manifest, dataset_tokenizer_report=report_path)

        def mutate_after_verification(*arguments):
            self.manifest.write_text('changed after verified snapshot')
            return report

        with patch('aos.dataset_preflight.verify_dataset_tokenizer_report', side_effect=mutate_after_verification):
            inventory = readiness(self.dataset, deployment=self.manifest, dataset_tokenizer_report=report_path)
        self.assertEqual(inventory['deployment_manifest_sha256'], report.deployment_manifest_sha256)


@unittest.skipUnless(os.environ.get('AOS_TOKENIZER_TESTS') == '1', 'Real pinned dataset tokenizer requires AOS_TOKENIZER_TESTS=1')
class DatasetPreflightRealTests(unittest.TestCase):
    def test_real_dataset_bound_tokenizer_has_no_weights_or_training(self):
        with tempfile.TemporaryDirectory(prefix='dataset-bound-', dir=REPO_ROOT / 'data') as temporary:
            dataset = Path(temporary) / 'dataset'
            build_fixture_dataset(dataset, include_synthetic=True)
            manifest = Path(os.environ.get('AOS_DECIDER_MANIFEST', REPO_ROOT / 'models/decider-manifest.json'))
            report = dataset_tokenizer_preflight(dataset, manifest, Path(os.environ['AOS_MODEL_PYTHON']),
                                                Path(os.environ['AOS_DECIDER_DATA_SOURCE']), include_synthetic=True)
            self.assertEqual(report.tokenizer_report['examples'], 1)
            self.assertEqual(report.tokenizer_report['variants'], 40)
            self.assertFalse(report.tokenizer_report['weights_loaded'])
            self.assertFalse(report.tokenizer_report['cuda_initialized'])
            self.assertEqual(verify_dataset_tokenizer_report(report.model_dump(), dataset, manifest), report)
            print(canonical({'real_dataset_tokenizer': report.model_dump()}))


if __name__ == '__main__':
    unittest.main()
