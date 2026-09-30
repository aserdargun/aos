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
from aos.dataset import validator
from aos.decider_calibration_smoke import CalibrationReport, calibration_smoke, runner_digest, synthetic_input


FIXTURE = json.loads((REPO_ROOT / 'examples/decider_calibration_smoke.json').read_text())


class DeciderCalibrationSmokeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manifest = self.root / 'synthetic-manifest.json'
        self.pins = {'checkpoint_revision': 'a' * 40, 'code_revision': 'b' * 40,
                     'model_files': {'model.safetensors': 'c' * 64}}
        self.manifest.write_text(canonical(self.pins))

    def fake_worker(self, arguments, **options):
        self.assertEqual(arguments[0], sys.executable)
        self.assertEqual(arguments[1], str(REPO_ROOT / 'services/decider/calibration_smoke.py'))
        self.assertEqual(options['timeout'], 180)
        self.assertEqual(options['env']['HF_HUB_OFFLINE'], '1')
        request = json.loads(options['input'])
        binding, examples = synthetic_input()
        self.assertEqual(request['examples'], examples)
        self.assertEqual(request['input_sha256'], binding['input_sha256'])
        self.assertEqual(request['train_sample_ref'], binding['train_sample_ref'])
        self.assertNotEqual(request['train_sample_ref'], request['heldout_sample_ref'])
        report = {**FIXTURE['report'], **binding,
                  'deployment_manifest_sha256': hashlib.sha256(self.manifest.read_bytes()).hexdigest(),
                  'runner_sha256': runner_digest(),
                  'checkpoint_revision': self.pins['checkpoint_revision'],
                  'code_revision': self.pins['code_revision'],
                  'weights_sha256': self.pins['model_files']['model.safetensors']}
        return subprocess.CompletedProcess(arguments, 0, canonical(report).encode(), b'')

    def test_distinct_reviewed_synthetic_input_and_non_authorizing_fixture(self):
        binding, examples = synthetic_input()
        self.assertNotEqual(binding['train_sample_ref'], binding['heldout_sample_ref'])
        self.assertNotEqual(examples['train']['context'], examples['heldout']['context'])
        self.assertNotEqual(examples['train']['task'], examples['heldout']['task'])
        self.assertTrue(FIXTURE['synthetic'])
        report = CalibrationReport.model_validate(FIXTURE['report'])
        validator('decider_calibration_smoke').validate(report.model_dump())
        self.assertFalse(report.training_ready)
        self.assertFalse(report.checkpoint_written)
        self.assertFalse(report.promotion_authorized)
        self.assertTrue(report.model_parameters_unchanged)
        self.assertTrue(report.transient_scalar_only)

    def test_mock_worker_binding_and_no_persistence(self):
        before = self.manifest.read_bytes()
        with patch('aos.decider_calibration_smoke.run_bounded', side_effect=self.fake_worker):
            report = calibration_smoke(self.manifest, Path(sys.executable),
                                       include_synthetic=True, optimizer_smoke=True)
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), [self.manifest.name])
        self.assertEqual(report.optimizer_step_count, 1)
        self.assertNotEqual(report.train_sample_ref, report.heldout_sample_ref)
        self.assertEqual(report.heldout_nll_after, 0.62)

    def test_explicit_opt_in_rejects_before_worker(self):
        with patch('aos.decider_calibration_smoke.run_bounded') as worker:
            for include_synthetic, optimizer_smoke in ((False, False), (True, False), (False, True)):
                with self.subTest(include_synthetic=include_synthetic, optimizer_smoke=optimizer_smoke):
                    with self.assertRaises(ValueError):
                        calibration_smoke(self.manifest, Path(sys.executable),
                                          include_synthetic=include_synthetic,
                                          optimizer_smoke=optimizer_smoke)
        worker.assert_not_called()

    def test_worker_overclaims_nonfinite_and_identity_mismatch_rejected(self):
        for field, value in (('training_ready', True), ('promotion_authorized', True),
                             ('checkpoint_written', True), ('model_parameters_unchanged', False),
                             ('transient_scalar_only', False), ('model_gradient_count', 1),
                             ('optimizer_step_count', 2), ('heldout_nll_after', float('nan')),
                             ('scalar_gradient_abs', float('inf')), ('temperature_after', 0),
                             ('weights_sha256', 'f' * 64), ('input_sha256', 'f' * 64),
                             ('train_sample_ref', FIXTURE['report']['heldout_sample_ref'])):
            def changed_worker(arguments, **options):
                result = self.fake_worker(arguments, **options)
                report = json.loads(result.stdout)
                report[field] = value
                return subprocess.CompletedProcess(arguments, 0, json.dumps(report).encode(), b'')

            with self.subTest(field=field), patch('aos.decider_calibration_smoke.run_bounded',
                                                  side_effect=changed_worker):
                with self.assertRaises(ValueError):
                    calibration_smoke(self.manifest, Path(sys.executable),
                                      include_synthetic=True, optimizer_smoke=True)

    def test_source_tamper_or_symlink_rejected_before_worker(self):
        from aos.dataset_preflight import bounded_file

        source = REPO_ROOT / 'examples/system1_choice.jsonl'
        original = bounded_file(source)

        def altered(path):
            content = bounded_file(path)
            return content.replace(b'File is absent.', b'File is present.') if path == source else content

        with patch('aos.decider_calibration_smoke.bounded_file', side_effect=altered), \
                patch('aos.decider_calibration_smoke.run_bounded') as worker:
            with self.assertRaises(ValueError):
                calibration_smoke(self.manifest, Path(sys.executable),
                                  include_synthetic=True, optimizer_smoke=True)
            worker.assert_not_called()
        self.assertEqual(bounded_file(source), original)
        alias = self.root / 'manifest-link.json'
        alias.symlink_to(self.manifest)
        with patch('aos.decider_calibration_smoke.run_bounded') as worker:
            with self.assertRaises(OSError):
                calibration_smoke(alias, Path(sys.executable),
                                  include_synthetic=True, optimizer_smoke=True)
            worker.assert_not_called()

    def test_changed_manifest_or_worker_failure_is_generic(self):
        def changed_worker(arguments, **options):
            result = self.fake_worker(arguments, **options)
            self.manifest.write_bytes(self.manifest.read_bytes() + b'\n')
            return result

        with patch('aos.decider_calibration_smoke.run_bounded', side_effect=changed_worker):
            with self.assertRaises(ValueError):
                calibration_smoke(self.manifest, Path(sys.executable),
                                  include_synthetic=True, optimizer_smoke=True)
        for result in (subprocess.CompletedProcess([], 1, b'', b'private stderr'),
                       subprocess.CompletedProcess([], 0, b'not JSON', b'')):
            with patch('aos.decider_calibration_smoke.run_bounded', return_value=result):
                with self.assertRaises(ValueError):
                    calibration_smoke(self.manifest, Path(sys.executable),
                                      include_synthetic=True, optimizer_smoke=True)


@unittest.skipUnless(os.environ.get('AOS_CALIBRATION_TESTS') == '1',
                     'Real pinned Decider diagnostic requires AOS_CALIBRATION_TESTS=1')
class DeciderCalibrationRealTests(unittest.TestCase):
    def test_real_frozen_decider_transient_scalar_without_checkpoint(self):
        manifest = Path(os.environ.get('AOS_DECIDER_MANIFEST', REPO_ROOT / 'models/decider-manifest.json'))
        report = calibration_smoke(manifest, Path(os.environ['AOS_MODEL_PYTHON']),
                                   include_synthetic=True, optimizer_smoke=True)
        self.assertTrue(report.model_parameters_unchanged)
        self.assertEqual(report.model_gradient_count, 0)
        self.assertFalse(report.checkpoint_written)
        self.assertFalse(report.training_ready)
        print(canonical({'real_transient_calibration': report.model_dump()}))


if __name__ == '__main__':
    unittest.main()
