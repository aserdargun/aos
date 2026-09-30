import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from aos.contracts import REPO_ROOT
from aos.dataset import validator
from aos.vision_skill_candidate import (VisionDualRoleCandidate, VisionSkillCandidate,
                                         derive_vision_dual_role_candidate, derive_vision_skill_candidate)


class VisionSkillCandidateTests(unittest.TestCase):
    def test_canonical_fixture_is_content_free_and_grants_no_authority(self):
        fixture = json.loads((REPO_ROOT / 'examples/vision_skill_candidate.json').read_text())
        self.assertTrue(fixture['synthetic'])
        candidate = fixture['candidate']
        self.assertEqual(json.loads((REPO_ROOT / 'schemas/vision_skill_candidate.schema.json').read_text()),
                         VisionSkillCandidate.model_json_schema())
        self.assertEqual(VisionSkillCandidate.model_validate(candidate).model_dump(), candidate)
        validator('vision_skill_candidate').validate(candidate)
        self.assertFalse(candidate['supervisor_outcome_verified'])
        self.assertTrue(candidate['downstream_operator_outcome_verified'])
        for field in ('supervisor_outcome_verified', 'reviewed', 'profile_bound',
                      'activation_authorized', 'execution_authorized',
                      'collection_authorized', 'training_ready'):
            with self.subTest(field=field):
                self.assertFalse(validator('vision_skill_candidate').is_valid({**candidate, field: True}))
        self.assertFalse(validator('vision_skill_candidate').is_valid({
            **candidate, 'raw_screenshot': 'private'}))

    def test_missing_and_unsafe_source_fail_without_path_disclosure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'missing.sqlite'
            with self.assertRaises(ValueError):
                derive_vision_skill_candidate(source, 'run-synthetic')
            for run_id in ('run-synthetic', '../private'):
                result = subprocess.run([sys.executable, '-m', 'aos.vision_skill_candidate',
                                         '--database', str(source), '--run-id', run_id],
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, '')
                self.assertNotIn(str(source), result.stderr)

    def test_dual_role_fixture_preserves_separate_outcome_and_authority(self):
        fixture = json.loads((REPO_ROOT / 'examples/vision_dual_role_candidate.json').read_text())
        candidate = fixture['candidate']
        self.assertTrue(fixture['synthetic'])
        self.assertEqual(json.loads((REPO_ROOT / 'schemas/vision_dual_role_candidate.schema.json').read_text()),
                         VisionDualRoleCandidate.model_json_schema())
        self.assertEqual(VisionDualRoleCandidate.model_validate(candidate).model_dump(), candidate)
        validator('vision_dual_role_candidate').validate(candidate)
        self.assertTrue(candidate['operator']['outcome_verified'])
        self.assertFalse(candidate['supervisor']['supervisor_outcome_verified'])
        self.assertEqual(candidate['operator']['verification_ref'],
                         candidate['supervisor']['downstream_verification_ref'])
        for field in ('profile_bound', 'reviewed', 'execution_authorized',
                      'collection_authorized', 'activation_authorized', 'training_ready'):
            with self.subTest(field=field):
                self.assertFalse(validator('vision_dual_role_candidate').is_valid({**candidate, field: True}))
        self.assertFalse(validator('vision_dual_role_candidate').is_valid({
            **candidate, 'operator': {**candidate['operator'], 'training_ready': True}}))
        self.assertFalse(validator('vision_dual_role_candidate').is_valid({
            **candidate, 'supervisor': {**candidate['supervisor'], 'supervisor_outcome_verified': True}}))
        self.assertFalse(validator('vision_dual_role_candidate').is_valid({
            **candidate, 'raw_screenshot': 'private'}))

    @unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1',
                         'Requires an isolated owned Docker desktop')
    def test_fixture_vision_success_cannot_be_reported_as_real_s2_candidate(self):
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='vision-candidate-fixture-', dir=REPO_ROOT / 'data') as directory:
            root = Path(directory)
            with task_server(root, 'fixture', ('--browser-tasks', '--vision-engine', 'fixture')) as (
                    origin, token, client, server):
                control = client.get('/api/state').json()['control']
                started = client.post('/api/tasks', json={
                    'kind': 'vision_canvas', 'lease_id': control['lease_id'],
                    'generation': control['generation']})
                started.raise_for_status()
                deadline = time.monotonic() + 45
                approved = False
                while time.monotonic() < deadline:
                    status = client.get('/api/tasks').json()
                    if status['approval'] is not None and not approved:
                        approval = status['approval']
                        client.post('/api/approvals/' + approval['approval_id'], json={
                            'action_sha256': approval['action_sha256'], 'accept': True}).raise_for_status()
                        approved = True
                    if not status['busy']:
                        break
                    time.sleep(.05)
                else:
                    self.fail('Fixture vision did not settle')
                self.assertTrue(approved)
                job = status['jobs'][0]
                self.assertEqual(job['status'], 'succeeded')
                self.assertEqual(job['real_model'], 0)
                with self.assertRaisesRegex(ValueError, 'vision_source_not_completed_real_task'):
                    derive_vision_skill_candidate(root / 'store.sqlite', job['run_id'])
                with self.assertRaisesRegex(ValueError, 'vision_source_not_completed_real_task'):
                    derive_vision_dual_role_candidate(root / 'store.sqlite', job['run_id'])


if __name__ == '__main__':
    unittest.main()
