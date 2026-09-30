import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from aos.contracts import REPO_ROOT, canonical
from aos.dataset import validator
from aos.decider_calibration_smoke import synthetic_input
from aos.decider_row_candidate import row_candidate_smoke
from aos.decider_row_replay import RowReplayReport, replay_row_candidate, runner_digest
from tests.test_decider_row_candidate import mock_artifact


FIXTURE = json.loads((REPO_ROOT / 'examples/decider_row_replay.json').read_text())


class DeciderRowReplayTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='row-replay-unit-', dir=REPO_ROOT / 'data')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manifest = self.root / 'synthetic-manifest.json'
        self.pins = {'checkpoint_revision': 'a' * 40, 'code_revision': 'b' * 40,
                     'model_files': {'model.safetensors': 'c' * 64}}
        self.manifest.write_text(canonical(self.pins))
        binding, _ = synthetic_input()
        self.artifact = self.root / 'candidate.aosrows'
        self.artifact.write_bytes(mock_artifact(binding, self.manifest.read_bytes()))
        self.artifact.chmod(0o600)

    def fake_worker(self, arguments, **options):
        self.assertEqual(arguments[0], sys.executable)
        self.assertEqual(arguments[1], str(REPO_ROOT / 'services/decider/row_replay.py'))
        self.assertEqual(options['timeout'], 180)
        self.assertEqual(options['env']['HF_HUB_OFFLINE'], '1')
        request = json.loads(options['input'])
        binding, examples = synthetic_input()
        self.assertEqual(request['heldout'], examples['heldout'])
        self.assertEqual(request['input_sha256'], binding['input_sha256'])
        self.assertEqual(request['token_ids'], [101, 102])
        report = {**FIXTURE['report'],
                  **{key: value for key, value in binding.items() if key != 'train_sample_ref'},
                  'deployment_manifest_sha256': hashlib.sha256(self.manifest.read_bytes()).hexdigest(),
                  'artifact_sha256': hashlib.sha256(self.artifact.read_bytes()).hexdigest(),
                  'runner_sha256': runner_digest(),
                  'checkpoint_revision': self.pins['checkpoint_revision'],
                  'code_revision': self.pins['code_revision'],
                  'weights_sha256': self.pins['model_files']['model.safetensors']}
        return subprocess.CompletedProcess(arguments, 0, canonical(report).encode(), b'')

    def test_schema_fixture_and_false_authority(self):
        self.assertTrue(FIXTURE['synthetic'])
        report = RowReplayReport.model_validate(FIXTURE['report'])
        validator('decider_row_replay').validate(report.model_dump())
        for change in ({'training_ready': True}, {'promotion_authorized': True},
                       {'candidate_loaded': False}, {'token_ids': [101, 101]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                RowReplayReport.model_validate({**report.model_dump(), **change})

    def test_mocked_private_candidate_replays_without_deployment(self):
        with patch('aos.decider_row_replay.run_bounded', side_effect=self.fake_worker):
            first = replay_row_candidate(self.manifest, Path(sys.executable), self.artifact,
                                         include_synthetic=True)
            second = replay_row_candidate(self.manifest, Path(sys.executable), self.artifact,
                                          include_synthetic=True)
        self.assertEqual(first, second)
        self.assertTrue(first.candidate_loaded)
        self.assertFalse(first.training_ready)
        self.assertFalse(first.promotion_authorized)
        self.assertEqual(stat.S_IMODE(self.artifact.stat().st_mode), 0o600)

    def test_flags_artifact_integrity_and_worker_failure(self):
        with patch('aos.decider_row_replay.run_bounded') as worker:
            with self.assertRaises(ValueError):
                replay_row_candidate(self.manifest, Path(sys.executable), self.artifact)
            self.artifact.chmod(0o644)
            with self.assertRaises(ValueError):
                replay_row_candidate(self.manifest, Path(sys.executable), self.artifact,
                                     include_synthetic=True)
            self.artifact.chmod(0o600)
            damaged = bytearray(self.artifact.read_bytes())
            damaged[8] ^= 1
            self.artifact.write_bytes(damaged)
            with self.assertRaises(ValueError):
                replay_row_candidate(self.manifest, Path(sys.executable), self.artifact,
                                     include_synthetic=True)
        worker.assert_not_called()

    def test_worker_report_identity_or_authority_mismatch_fails(self):
        for change in ({'artifact_sha256': '0' * 64}, {'runner_sha256': '0' * 64},
                       {'promotion_authorized': True}, {'token_ids': [103, 104]}):
            def changed_worker(arguments, **options):
                result = self.fake_worker(arguments, **options)
                return subprocess.CompletedProcess(arguments, 0,
                                                   canonical({**json.loads(result.stdout), **change}).encode(), b'')

            with self.subTest(change=change), patch('aos.decider_row_replay.run_bounded',
                                                    side_effect=changed_worker), self.assertRaises(ValueError):
                replay_row_candidate(self.manifest, Path(sys.executable), self.artifact,
                                     include_synthetic=True)

    def test_cli_missing_artifact_has_generic_error(self):
        command = [sys.executable, '-m', 'aos.decider_row_replay', '--manifest', str(self.manifest),
                   '--model-python', sys.executable, '--artifact', str(self.root / 'missing.aosrows'),
                   '--include-synthetic']
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')
        self.assertEqual(result.stderr,
                         'Row candidate replay unavailable: pinned synthetic source or private artifact differs. No deployment changed.\n')


@unittest.skipUnless(os.environ.get('AOS_ROW_REPLAY_TESTS') == '1',
                     'Real pinned Decider candidate replay requires AOS_ROW_REPLAY_TESTS=1')
class DeciderRowReplayRealTests(unittest.TestCase):
    def test_pinned_candidate_is_reloaded_and_scored_without_promotion(self):
        with tempfile.TemporaryDirectory(prefix='row-replay-real-', dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            manifest = REPO_ROOT / 'models/decider-manifest.json'
            model_python = Path(os.environ['AOS_MODEL_PYTHON'])
            candidate = row_candidate_smoke(manifest, model_python, include_synthetic=True,
                                            train_candidate=True, output_root=root)
            artifact = root / (candidate.candidate_artifact_sha256 + '.aosrows')
            replay = replay_row_candidate(manifest, model_python, artifact, include_synthetic=True)
            self.assertEqual(replay.artifact_sha256, candidate.candidate_artifact_sha256)
            self.assertEqual(replay.token_ids, candidate.token_ids)
            self.assertAlmostEqual(replay.heldout_base_nll, candidate.heldout_nll_before, delta=0.001)
            self.assertAlmostEqual(replay.heldout_candidate_nll, candidate.heldout_nll_after, delta=0.001)
            self.assertTrue(replay.candidate_loaded)
            self.assertFalse(replay.training_ready)
            self.assertFalse(replay.promotion_authorized)
            command = [sys.executable, '-m', 'aos.decider_row_replay', '--manifest', str(manifest),
                       '--model-python', str(model_python), '--artifact', str(artifact),
                       '--include-synthetic']
            completed = subprocess.run(command, capture_output=True, text=True, timeout=210,
                                       check=True)
            cli_report = RowReplayReport.model_validate_json(completed.stdout)
            self.assertEqual(cli_report, replay)
            print(canonical({'row_replay': replay.model_dump()}))
        self.assertFalse(root.exists())


if __name__ == '__main__':
    unittest.main()
