import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from aos.contracts import REPO_ROOT, canonical
from aos.dataset import validator
from aos.decider_calibration_smoke import synthetic_input
from aos.decider_row_candidate import ARTIFACT_BYTES, ARTIFACT_MAGIC, RowCandidateReport, row_candidate_smoke, runner_digest


FIXTURE = json.loads((REPO_ROOT / 'examples/decider_row_candidate.json').read_text())


def mock_artifact(binding, manifest):
    return (ARTIFACT_MAGIC + bytes.fromhex(binding['source_sha256'])
            + bytes.fromhex(binding['input_sha256']) + hashlib.sha256(manifest).digest()
            + struct.pack('<II', 101, 102) + struct.pack('<4096f', *([0.1] * 4096)))


class DeciderRowCandidateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='row-candidate-unit-', dir=REPO_ROOT / 'data')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manifest = self.root / 'synthetic-manifest.json'
        self.pins = {'checkpoint_revision': 'a' * 40, 'code_revision': 'b' * 40,
                     'model_files': {'model.safetensors': 'c' * 64}}
        self.manifest.write_text(canonical(self.pins))

    def fake_worker(self, arguments, **options):
        self.assertEqual(arguments[0], sys.executable)
        self.assertEqual(arguments[1], str(REPO_ROOT / 'services/decider/row_candidate.py'))
        self.assertEqual(options['timeout'], 180)
        self.assertEqual(options['env']['HF_HUB_OFFLINE'], '1')
        request = json.loads(options['input'])
        binding, examples = synthetic_input()
        self.assertEqual(request['examples'], examples)
        self.assertEqual(request['input_sha256'], binding['input_sha256'])
        self.assertNotEqual(request['train_sample_ref'], request['heldout_sample_ref'])
        output = Path(arguments[3])
        self.assertEqual(output.parent, self.root)
        self.assertEqual(output.stat().st_size, 0)
        payload = mock_artifact(binding, self.manifest.read_bytes())
        output.write_bytes(payload)
        report = {**FIXTURE['report'], **binding,
                  'deployment_manifest_sha256': hashlib.sha256(self.manifest.read_bytes()).hexdigest(),
                  'runner_sha256': runner_digest(),
                  'checkpoint_revision': self.pins['checkpoint_revision'],
                  'code_revision': self.pins['code_revision'],
                  'weights_sha256': self.pins['model_files']['model.safetensors'],
                  'candidate_artifact_sha256': hashlib.sha256(payload).hexdigest(),
                  'candidate_artifact_bytes': len(payload)}
        return subprocess.CompletedProcess(arguments, 0, canonical(report).encode(), b'')

    def run_smoke(self):
        return row_candidate_smoke(self.manifest, Path(sys.executable),
                                   include_synthetic=True, train_candidate=True,
                                   output_root=self.root)

    def test_canonical_synthetic_report_rejects_authority_overclaims(self):
        self.assertTrue(FIXTURE['synthetic'])
        report = RowCandidateReport.model_validate(FIXTURE['report'])
        validator('decider_row_candidate').validate(report.model_dump())
        for field, value in (('training_ready', True), ('promotion_authorized', True),
                             ('candidate_only', False), ('model_parameters_unchanged', False),
                             ('model_gradient_count', 1), ('token_ids', [101, 101]),
                             ('train_sample_ref', report.heldout_sample_ref)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                RowCandidateReport.model_validate({**report.model_dump(), field: value})

    def test_mocked_private_artifact_is_content_addressed_and_idempotent(self):
        binding, _ = synthetic_input()
        payload = mock_artifact(binding, self.manifest.read_bytes())
        self.assertEqual(len(payload), ARTIFACT_BYTES)
        with patch('aos.decider_row_candidate.run_bounded', side_effect=self.fake_worker):
            first = self.run_smoke()
            second = self.run_smoke()
        self.assertEqual(first, second)
        candidate = self.root / (first.candidate_artifact_sha256 + '.aosrows')
        self.assertEqual(candidate.read_bytes(), payload)
        self.assertEqual(stat.S_IMODE(candidate.stat().st_mode), 0o600)
        self.assertEqual(candidate.stat().st_nlink, 1)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()),
                         [candidate.name, self.manifest.name])
        self.assertFalse(first.promotion_authorized)
        self.assertFalse(first.training_ready)

    def test_explicit_flags_and_ignored_private_output_root(self):
        with patch('aos.decider_row_candidate.run_bounded') as worker:
            for synthetic, train in ((False, False), (True, False), (False, True)):
                with self.assertRaises(ValueError):
                    row_candidate_smoke(self.manifest, Path(sys.executable),
                                        include_synthetic=synthetic, train_candidate=train,
                                        output_root=self.root)
            with tempfile.TemporaryDirectory() as outside:
                with self.assertRaises(ValueError):
                    row_candidate_smoke(self.manifest, Path(sys.executable),
                                        include_synthetic=True, train_candidate=True,
                                        output_root=Path(outside))
        worker.assert_not_called()
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), [self.manifest.name])

    def test_symlink_or_broad_output_permissions_rejected_before_worker(self):
        alias = REPO_ROOT / 'data' / (self.root.name + '-alias')
        alias.symlink_to(self.root, target_is_directory=True)
        self.addCleanup(alias.unlink)
        with patch('aos.decider_row_candidate.run_bounded') as worker:
            with self.assertRaises((OSError, ValueError)):
                row_candidate_smoke(self.manifest, Path(sys.executable),
                                    include_synthetic=True, train_candidate=True,
                                    output_root=alias)
            self.root.chmod(0o755)
            with self.assertRaises(ValueError):
                self.run_smoke()
            self.root.chmod(0o700)
        worker.assert_not_called()
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), [self.manifest.name])

    def test_partial_child_failure_removes_temporary_artifact(self):
        def failed_worker(arguments, **options):
            Path(arguments[3]).write_bytes(b'partial')
            return subprocess.CompletedProcess(arguments, 1, b'', b'private stderr')

        with patch('aos.decider_row_candidate.run_bounded', side_effect=failed_worker):
            with self.assertRaises(ValueError):
                self.run_smoke()
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), [self.manifest.name])

    def test_invalid_artifact_or_report_fails_without_publication(self):
        for mutation in ('hash', 'token_ids', 'source_header', 'nan', 'authority'):
            def changed_worker(arguments, **options):
                result = self.fake_worker(arguments, **options)
                report = json.loads(result.stdout)
                output = Path(arguments[3])
                if mutation == 'hash':
                    report['candidate_artifact_sha256'] = 'f' * 64
                elif mutation == 'token_ids':
                    report['token_ids'] = [103, 104]
                elif mutation == 'source_header':
                    changed = bytearray(output.read_bytes())
                    changed[8] ^= 1
                    output.write_bytes(changed)
                    report['candidate_artifact_sha256'] = hashlib.sha256(changed).hexdigest()
                elif mutation == 'nan':
                    changed = bytearray(output.read_bytes())
                    changed[112:116] = struct.pack('<f', float('nan'))
                    output.write_bytes(changed)
                    report['candidate_artifact_sha256'] = hashlib.sha256(changed).hexdigest()
                else:
                    report['promotion_authorized'] = True
                return subprocess.CompletedProcess(arguments, 0, json.dumps(report).encode(), b'')

            with self.subTest(mutation=mutation), patch('aos.decider_row_candidate.run_bounded',
                                                        side_effect=changed_worker):
                with self.assertRaises(ValueError):
                    self.run_smoke()
                self.assertEqual(sorted(path.name for path in self.root.iterdir()), [self.manifest.name])


@unittest.skipUnless(os.environ.get('AOS_ROW_CANDIDATE_TESTS') == '1',
                     'Real pinned Decider candidate requires AOS_ROW_CANDIDATE_TESTS=1')
class DeciderRowCandidateRealTests(unittest.TestCase):
    def test_real_isolated_option_row_candidate_is_not_promoted_and_cleans_up(self):
        with tempfile.TemporaryDirectory(prefix='row-candidate-real-', dir=REPO_ROOT / 'data') as temporary:
            output_root = Path(temporary)
            manifest = Path(os.environ.get('AOS_DECIDER_MANIFEST', REPO_ROOT / 'models/decider-manifest.json'))
            report = row_candidate_smoke(manifest, Path(os.environ['AOS_MODEL_PYTHON']),
                                         include_synthetic=True, train_candidate=True,
                                         output_root=output_root)
            candidate = output_root / (report.candidate_artifact_sha256 + '.aosrows')
            self.assertTrue(candidate.is_file())
            self.assertEqual(stat.S_IMODE(candidate.stat().st_mode), 0o600)
            self.assertEqual(candidate.stat().st_size, ARTIFACT_BYTES)
            self.assertFalse(report.promotion_authorized)
            self.assertFalse(report.training_ready)
            print(canonical({'real_row_candidate': report.model_dump()}))
        self.assertFalse(output_root.exists())


if __name__ == '__main__':
    unittest.main()
