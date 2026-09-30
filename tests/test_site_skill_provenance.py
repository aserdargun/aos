import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from aos.contracts import REPO_ROOT, digest
from aos.dataset import validator
from aos.dataset_audit import audit_snapshot
from aos.learning_events import DERIVATION_VERSION
from aos.site_knowledge import SiteKnowledgeStore, SitePageDraft
from aos.site_skill import SiteSkillDraft, SiteSkillStore
from aos.site_skill_provenance import inspect_site_skill_sources
from aos.storage import TrajectoryStore
from aos.web_application import WebApplicationProfile, WebApplicationProfiles, profile_report


PROFILE = json.loads((REPO_ROOT / 'examples/web_application_profile.json').read_text())['profile']
PAGE = json.loads((REPO_ROOT / 'examples/site_page_draft.json').read_text())['page']
SKILL = json.loads((REPO_ROOT / 'examples/site_skill_draft.json').read_text())['skill']
DECIDER = {'deployment_id': 'decider-test', 'kind': 'decider_native_worker',
           'real_model': True, 'pins': {'revision': 'synthetic'}}
BONSAI = {'deployment_id': 'bonsai-test', 'kind': 'bonsai_native_supervisor',
          'real_model': True, 'pins': {'revision': 'synthetic'}}


class SiteSkillProvenanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profiles = WebApplicationProfiles(self.root / 'profiles')
        profile = WebApplicationProfile.model_validate(PROFILE)
        self.profile_sha256 = profile_report(profile).profile_sha256
        self.profiles.register(profile, confirm_sha256=self.profile_sha256)
        self.pages = SiteKnowledgeStore(self.root / 'pages', self.profiles)
        page = SitePageDraft.model_validate(PAGE)
        self.pages.register(page, confirm_sha256=digest(page.model_dump()))
        self.skills = SiteSkillStore(self.root / 'skills', self.profiles, self.pages)
        self.database = self.root / 'trajectory.sqlite'
        self.trajectory = TrajectoryStore(self.database)
        self.addCleanup(self.trajectory.close)
        with self.trajectory.connection:
            self.trajectory.insert('tasks', task_id='task-test', original_goal='private-do-not-export',
                                   normalized_goal='synthetic', success_criteria_json='[]',
                                   workspace_scope_json='[]', created_at='2026-01-01T00:00:00Z')
            self.trajectory.insert('runs', run_id='run-test', task_id='task-test', status='running',
                                   outcome='unknown', policy_version='hello-policy-v1',
                                   environment_json='{"secret":"private-do-not-export"}',
                                   deployment_snapshot_json=json.dumps({**DECIDER, 'supervisor': BONSAI}),
                                   started_at='2026-01-01T00:00:00Z')
            self.trajectory.insert('steps', step_id='step-test', run_id='run-test', ordinal=0,
                                   state='DECIDE', started_at='2026-01-01T00:00:00Z')
            self.trajectory.insert('model_calls', call_id='call-test', run_id='run-test',
                                   step_id='step-test', deployment_id='decider-test', role='system1',
                                   request_json='{}', response_json='{}', status='ok',
                                   created_at='2026-01-01T00:00:00Z')
            self.trajectory.insert('state_snapshots', snapshot_id='snapshot-test', run_id='run-test',
                                   step_id='step-test', state_version=1, state_json='{}',
                                   content_sha256='0' * 64, created_at='2026-01-01T00:00:00Z')
            self.trajectory.insert('decisions', decision_id='decision-test', run_id='run-test',
                                   step_id='step-test', snapshot_id='snapshot-test', call_id='call-test',
                                   question='synthetic', options_json='[{"id":"a"},{"id":"b"}]',
                                   probabilities_json='{"a":1,"b":0}', selected_option='a',
                                   confidence=1, policy_result='allow', created_at='2026-01-01T00:00:00Z')
            self.trajectory.insert('actions', action_id='action-test', run_id='run-test',
                                   step_id='step-test', decision_id='decision-test',
                                   idempotency_key='effect-key', tool='filesystem.write',
                                   arguments_json='{}', status='ok', actual_option='a',
                                   created_at='2026-01-01T00:00:00Z')
            self.trajectory.insert('actions', action_id='readback-test', run_id='run-test',
                                   step_id='step-test', decision_id='decision-test',
                                   idempotency_key='readback-key', tool='filesystem.read',
                                   arguments_json='{}', status='ok', result_json='{"content":"same"}',
                                   actual_option='a', created_at='2026-01-01T00:00:00Z')
            self.trajectory.insert('observations', observation_id='observation-test', run_id='run-test',
                                   step_id='step-test', action_id='readback-test', kind='filesystem.read',
                                   payload_json='{"content":"same"}',
                                   created_at='2026-01-01T00:00:00Z')
            self.trajectory.insert('verifications', verification_id='verification-test',
                                   run_id='run-test', step_id='step-test', action_id='action-test',
                                   criterion='matches', method='independent_read_equals', result='passed',
                                   expected_json='"same"', actual_json='"same"',
                                   evidence_refs_json='["observation-test"]', verifier='aos-exact-bytes-v1',
                                   created_at='2026-01-01T00:00:00Z')

    def register(self, **changes):
        draft = SiteSkillDraft.model_validate({**SKILL, **changes})
        checksum = digest(draft.model_dump())
        self.skills.register(draft, confirm_sha256=checksum)
        return checksum

    def inspect(self, checksum, run_id='run-test'):
        return inspect_site_skill_sources(self.database, run_id, profiles=self.profiles.root,
                                          pages=self.pages.root, skills=self.skills.root,
                                          skill_sha256=checksum)

    def test_fixture_and_s1_exact_source_verification(self):
        fixture = json.loads((REPO_ROOT / 'examples/site_skill_provenance.json').read_text())
        self.assertTrue(fixture['synthetic'])
        validator('site_skill_provenance').validate(fixture['report'])
        checksum = self.register()
        with patch('aos.site_skill_provenance.audit_snapshot', wraps=audit_snapshot) as audited:
            report = self.inspect(checksum)
        self.assertEqual(audited.call_count, 1)
        self.assertEqual(report['skill_sha256'], fixture['report']['skill_sha256'])
        self.assertEqual(report['source_event_ids'], fixture['report']['source_event_ids'])
        self.assertEqual(report['source_request_sha256'], fixture['report']['source_request_sha256'])
        self.assertFalse(validator('site_skill_provenance').is_valid({
            **report, 'source_request_sha256': ['wrong']}))
        self.assertEqual(report['deployment_sha256'], fixture['report']['deployment_sha256'])
        self.assertEqual(report['direct_verification_status'], 'claimed_and_matched')
        self.assertTrue(report['has_direct_verified_outcome'])
        self.assertFalse(report['profile_bound'])
        self.assertFalse(report['training_ready'])
        self.assertEqual(report, self.inspect(checksum))
        self.assertNotIn('private-do-not-export', json.dumps(report))
        self.assertFalse(validator('site_skill_provenance').is_valid({**report, 'reviewed': True}))
        unclaimed = self.inspect(self.register(source_verification_ids=[]))
        self.assertEqual(unclaimed['direct_verification_status'], 'not_claimed')
        self.assertTrue(unclaimed['has_direct_verified_outcome'])
        self.assertEqual(unclaimed['claimed_verification_ids'], [])

    def test_s2_reports_direct_verification_absent_and_rejects_claim(self):
        event_id = 'learning-' + digest({'version': DERIVATION_VERSION, 'role': 'system2',
                                         'run_id': 'run-test', 'call_id': 'bonsai-call'})
        with self.trajectory.connection:
            self.trajectory.insert('model_calls', call_id='bonsai-call', run_id='run-test',
                                   step_id='step-test', deployment_id='bonsai-test', role='system2',
                                   request_json='{}', response_json='{}', status='ok',
                                   created_at='2026-01-01T00:00:00Z')
        changes = {'skill_key': 'find-record-plan', 'model_role': 'system2',
                   'candidate_kind': 'workflow_plan', 'source_event_ids': [event_id],
                   'source_verification_ids': []}
        report = self.inspect(self.register(**changes))
        self.assertEqual(report['direct_verification_status'], 'absent_for_system2')
        self.assertFalse(report['has_direct_verified_outcome'])
        self.assertFalse(report['has_downstream_verification'])
        self.assertEqual(report['claimed_verification_ids'], [])
        self.assertFalse(report['activation_authorized'])
        with self.assertRaises(ValueError):
            self.inspect(self.register(**{**changes, 'source_verification_ids': ['verification-test']}))
        with self.assertRaises(ValueError):
            self.inspect(self.register(source_event_ids=[event_id]))

    def test_missing_event_wrong_deployment_and_verification_fail_closed(self):
        checksum = self.register()
        with self.assertRaises(ValueError):
            self.inspect(checksum, run_id='missing')
        with self.assertRaises(ValueError):
            self.inspect(self.register(source_event_ids=['learning-' + '0' * 64]))
        with self.assertRaises(ValueError):
            self.inspect(self.register(source_verification_ids=['missing-verification']))
        with self.trajectory.connection:
            self.trajectory.connection.execute(
                "UPDATE verifications SET actual_json='\"different\"' WHERE verification_id='verification-test'")
        with self.assertRaises(ValueError):
            self.inspect(checksum)
        with self.trajectory.connection:
            self.trajectory.connection.execute(
                "UPDATE verifications SET actual_json='\"same\"' WHERE verification_id='verification-test'")
            self.trajectory.connection.execute(
                "UPDATE model_calls SET deployment_id='wrong-deployment' WHERE call_id='call-test'")
        with self.assertRaises(ValueError):
            self.inspect(checksum)

    def test_cli_metadata_only_and_secret_safe_failure(self):
        checksum = self.register()
        command = [sys.executable, '-m', 'aos.site_skill_provenance', '--database', str(self.database),
                   '--run-id', 'run-test', '--profiles', str(self.profiles.root), '--pages', str(self.pages.root),
                   '--skills', str(self.skills.root), '--skill-sha256', checksum]
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), self.inspect(checksum))
        self.assertNotIn('private-do-not-export', result.stdout)
        failure = subprocess.run([*command, '--unknown'], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(failure.returncode, 0)
        self.assertEqual(failure.stdout, '')
        self.assertNotIn(str(self.database), failure.stderr)
        for malformed in ([*command, '--unknown', 'secret-do-not-export'],
                          [*command[:-2], '--skill-sha', 'secret-do-not-export'],
                          command[:-2]):
            with self.subTest(malformed=malformed[-2:]):
                rejected = subprocess.run(malformed, capture_output=True, text=True, timeout=10)
                self.assertEqual(rejected.returncode, 2)
                self.assertEqual(rejected.stdout, '')
                self.assertEqual(rejected.stderr,
                                 'Site skill source inspection unavailable: invalid arguments.\n')


if __name__ == '__main__':
    unittest.main()
