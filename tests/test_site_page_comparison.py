import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from aos.contracts import REPO_ROOT, digest
from aos.dataset import validator
from aos.site_knowledge import SiteKnowledgeStore, SitePageDraft
from aos.site_page_comparison import compare_site_page_draft
from aos.site_page_evidence_cli import select_site_page_evidence
from aos.storage import TrajectoryStore
from aos.web_application import WebApplicationProfile, WebApplicationProfiles, profile_report


PROFILE = json.loads((REPO_ROOT / 'examples/web_application_profile.json').read_text())['profile']
PAGE = json.loads((REPO_ROOT / 'examples/site_page_draft.json').read_text())['page']
OUTCOME = {'page': 'start', 'heading': 'Synthetic start',
           'links': [{'role': 'link', 'label': 'Details'}]}
OBSERVED = {'page': 'start', 'heading': 'Synthetic start', 'snapshot_id': 'a' * 32,
            'elements': [{'element_id': 'b' * 32, 'role': 'link', 'label': 'Details'}]}


class SitePageComparisonTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profiles = WebApplicationProfiles(self.root / 'profiles')
        profile = WebApplicationProfile.model_validate(PROFILE)
        self.profile_sha256 = profile_report(profile).profile_sha256
        self.profiles.register(profile, confirm_sha256=self.profile_sha256)
        self.knowledge = SiteKnowledgeStore(self.root / 'knowledge', self.profiles)
        self.database = self.root / 'trajectory.sqlite'
        trajectory = TrajectoryStore(self.database)
        self.addCleanup(trajectory.close)
        self.trajectory = trajectory
        with trajectory.connection:
            trajectory.insert('tasks', task_id='task-test', original_goal='private-do-not-export',
                              normalized_goal='synthetic', success_criteria_json='[]',
                              workspace_scope_json='[]', created_at='2026-01-01T00:00:00Z')
            trajectory.insert('runs', run_id='run-test', task_id='task-test', status='running',
                              outcome='unknown', policy_version='browser-local-navigation-policy-v1',
                              environment_json='{"secret":"private-do-not-export"}',
                              deployment_snapshot_json=json.dumps({
                                  'deployment_id': 'decider-test', 'kind': 'decider_native_worker',
                                  'real_model': True, 'pins': {'revision': 'synthetic'}}),
                              started_at='2026-01-01T00:00:00Z')
            trajectory.insert('steps', step_id='step-test', run_id='run-test', ordinal=0,
                              state='DECIDE', started_at='2026-01-01T00:00:00Z')
            trajectory.insert('model_calls', call_id='call-test', run_id='run-test', step_id='step-test',
                              deployment_id='decider-test', role='system1', request_json='{}',
                              response_json='{}', status='ok', created_at='2026-01-01T00:00:00Z')
            trajectory.insert('state_snapshots', snapshot_id='snapshot-test', run_id='run-test',
                              step_id='step-test', state_version=1, state_json='{}', content_sha256='0' * 64,
                              created_at='2026-01-01T00:00:00Z')
            trajectory.insert('decisions', decision_id='decision-test', run_id='run-test',
                              step_id='step-test', snapshot_id='snapshot-test', call_id='call-test',
                              question='synthetic', options_json='[{"id":"a"},{"id":"b"}]',
                              probabilities_json='{"a":1,"b":0}', selected_option='a',
                              confidence=1, policy_result='allow', created_at='2026-01-01T00:00:00Z')
            trajectory.insert('actions', action_id='effect-test', run_id='run-test', step_id='step-test',
                              decision_id='decision-test', idempotency_key='effect-key',
                              tool='browser.fixture.open', arguments_json='{}', status='ok',
                              actual_option='open_start', created_at='2026-01-01T00:00:00Z')
            trajectory.insert('actions', action_id='readback-test', run_id='run-test', step_id='step-test',
                              decision_id='decision-test', idempotency_key='readback-key',
                              tool='browser.fixture.snapshot', arguments_json='{}', status='ok',
                              result_json=json.dumps(OBSERVED), actual_option='open_start',
                              created_at='2026-01-01T00:00:00Z')
            trajectory.insert('observations', observation_id='observation-test', run_id='run-test',
                              step_id='step-test', action_id='readback-test', kind='browser.local_navigation',
                              payload_json=json.dumps(OBSERVED), created_at='2026-01-01T00:00:00Z')
            trajectory.insert('verifications', verification_id='verification-test', run_id='run-test',
                              step_id='step-test', action_id='effect-test', criterion='Exact synthetic Start page',
                              method='independent_local_page_equals', result='passed',
                              expected_json=json.dumps(OUTCOME), actual_json=json.dumps(OUTCOME),
                              evidence_refs_json='["observation-test"]', verifier='aos-local-navigation-v1',
                              created_at='2026-01-01T00:00:00Z')

    def register(self, **changes):
        page = SitePageDraft.model_validate({**PAGE, **changes})
        checksum = digest(page.model_dump())
        self.knowledge.register(page, confirm_sha256=checksum)
        return checksum

    def compare(self, checksum, **changes):
        arguments = {'profiles': self.profiles.root, 'store': self.knowledge.root,
                     'knowledge_sha256': checksum, 'selected_profile_sha256': self.profile_sha256,
                     'verification_id': 'verification-test'}
        return compare_site_page_draft(self.database, 'run-test', **{**arguments, **changes})

    def test_fixture_and_explicit_unbound_statuses(self):
        fixture = json.loads((REPO_ROOT / 'examples/site_page_comparison.json').read_text())
        self.assertTrue(fixture['synthetic'])
        validator('site_page_comparison').validate(fixture['comparison'])
        mismatch = self.compare(self.register())
        self.assertEqual(mismatch['status'], 'page_key_mismatch_unbound')
        self.assertEqual(mismatch['knowledge_sha256'], fixture['comparison']['knowledge_sha256'])
        self.assertEqual(mismatch['draft_page_key'], fixture['comparison']['draft_page_key'])
        self.assertEqual(mismatch['evidence_fingerprint_sha256'],
                         fixture['comparison']['evidence_fingerprint_sha256'])
        self.assertFalse(validator('site_page_comparison').is_valid(
            {**fixture['comparison'], 'profile_bound': True}))
        self.assertFalse(validator('site_page_comparison').is_valid(
            {**fixture['comparison'], 'origin_verified': True}))
        matching = self.compare(self.register(page_key='start', outgoing_page_keys=[],
                                              page_fingerprint_sha256=fixture['comparison']['evidence_fingerprint_sha256']))
        self.assertEqual(matching['status'], 'hash_equal_unbound')
        different = self.compare(self.register(page_key='start', outgoing_page_keys=[],
                                               page_fingerprint_sha256='b' * 64))
        self.assertEqual(different['status'], 'hash_different_unbound')
        for report in (mismatch, matching, different):
            validator('site_page_comparison').validate(report)
            self.assertTrue(all(report[field] is False for field in (
                'profile_bound', 'origin_verified', 'route_verified', 'fingerprint_semantics_verified',
                'reviewed', 'execution_authorized', 'collection_authorized', 'training_ready')))
            self.assertNotIn('https://', json.dumps(report))
            self.assertNotIn('private-do-not-export', json.dumps(report))
            self.assertEqual(report, self.compare(report['knowledge_sha256']))

    def test_missing_sources_and_scope_mismatch_fail_closed(self):
        checksum = self.register()
        for changes in ({'knowledge_sha256': '0' * 64},
                        {'selected_profile_sha256': '0' * 64},
                        {'verification_id': 'missing'},
                        {'verification_id': '../unsafe'}):
            with self.subTest(changes=changes), self.assertRaises((ValueError, FileNotFoundError)):
                self.compare(checksum, **changes)
        with self.trajectory.connection:
            self.trajectory.connection.execute(
                "UPDATE verifications SET actual_json='{}' WHERE verification_id='verification-test'")
        with self.assertRaises(ValueError):
            self.compare(checksum)

    def test_cli_is_read_only_and_suppresses_private_source_on_failure(self):
        checksum = self.register(page_key='start', outgoing_page_keys=[],
                                 page_fingerprint_sha256='f8d2ae93e37fb34a35d0a3eefc18297bafa15f15e81cb76e088560721b8d639b')
        command = [sys.executable, '-m', 'aos.site_page_comparison', '--database', str(self.database),
                   '--run-id', 'run-test', '--profiles', str(self.profiles.root), '--store', str(self.knowledge.root),
                   '--knowledge-sha256', checksum, '--selected-profile-sha256', self.profile_sha256,
                   '--verification-id', 'verification-test']
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), self.compare(checksum))
        self.assertEqual(result.stderr, '')
        self.assertEqual(len(list(self.knowledge.root.iterdir())), 1)
        failure = subprocess.run([*command, '--unexpected'], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(failure.returncode, 0)
        self.assertEqual(failure.stdout, '')
        self.assertNotIn('private-do-not-export', failure.stderr)

    def test_exact_selection_cli_uses_audited_private_sources(self):
        selected = select_site_page_evidence(self.database, 'run-test', 'verification-test')
        self.assertEqual(selected['page_count'], 1)
        self.assertEqual(selected['pages'][0]['page_key'], 'start')
        inspect = subprocess.run(
            [sys.executable, '-m', 'aos.site_page_evidence_cli', 'inspect',
             '--database', str(self.database), '--run-id', 'run-test',
             '--verification-id', 'verification-test'],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(inspect.returncode, 0, inspect.stderr)
        self.assertEqual(json.loads(inspect.stdout), selected)
        self.assertNotIn('private-do-not-export', inspect.stdout)
        checksum = self.register()
        comparison = subprocess.run(
            [sys.executable, '-m', 'aos.site_page_evidence_cli', 'compare',
             '--database', str(self.database), '--run-id', 'run-test',
             '--verification-id', 'verification-test', '--profiles', str(self.profiles.root),
             '--store', str(self.knowledge.root), '--knowledge-sha256', checksum,
             '--selected-profile-sha256', self.profile_sha256],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(comparison.returncode, 0, comparison.stderr)
        self.assertEqual(json.loads(comparison.stdout), self.compare(checksum))
        self.assertNotIn('private-do-not-export', comparison.stdout)


if __name__ == '__main__':
    unittest.main()
