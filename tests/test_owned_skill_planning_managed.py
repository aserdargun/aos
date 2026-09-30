import os
import shutil
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from aos import local_app
from aos.contracts import REPO_ROOT, canonical
from aos.owned_skill_planner import BonsaiOwnedSkillPlanner
from aos.owned_skill_planning import OwnedSkillPlanning
import test_owned_candidate_execution_managed as execution_helpers
import test_owned_skill_release_managed as release_helpers
from test_owned_skill_reuse_managed import owned_subprocess_session, read_isolated_state


@unittest.skipUnless(os.environ.get('AOS_OWNED_SKILL_PLANNING_TESTS') == '1'
                     and os.environ.get('AOS_DESKTOP_TESTS') == '1',
                     'Explicit real Bonsai planning in an isolated owned-reuse backend')
class OwnedSkillPlanningManagedTests(unittest.TestCase):
    def test_two_real_goal_proposals_no_actions_and_pending_cancellation(self):
        base = REPO_ROOT / 'data' / ('local-app-test-' + uuid4().hex)
        base.mkdir(mode=0o700)
        helper = execution_helpers.OwnedCandidateExecutionManagedTests()
        release_helper = release_helpers.OwnedSkillReleaseManagedTests()
        completed = False

        def post(client, suffix, value):
            response = client.post('/api/tasks/owned-form-candidate/' + suffix, json=value)
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()

        try:
            with owned_subprocess_session(base) as first:
                context, candidate, demonstration = helper.create_source_candidate(first.client)
                review = release_helper.evidence_and_review(first.client, helper, context, candidate, 'beta')
                request = {'schema_version': '1.0', 'review_sha256': review, 'parent_release_sha256': None}
                release = post(first.client, 'release-preview', request)['release_sha256']
                post(first.client, 'release-publish', request | {'confirm_sha256': release})
                request = {'schema_version': '1.0', 'release_sha256': release,
                           'expected_selection_sha256': None, 'operation': 'select'}
                selection = post(first.client, 'selection-preview', request)['selection_sha256']
                post(first.client, 'selection-commit', request | {'confirm_sha256': selection})
                database = first.source / 'store.sqlite'
            previous = read_isolated_state(base)
            self.assertEqual(previous.phase, 'stopped')
            with patch('socket.socket', side_effect=AssertionError('offline_preview')):
                material = local_app.prepare_owned_skill_reuse(previous, release, selection, base=base)
            with owned_subprocess_session(base, previous=previous, material=material) as second:
                client = second.client
                control = client.get('/api/state').json()['control']
                counts = helper.database_counts(database)

                def plan(goal):
                    response = client.post('/api/tasks/owned-skill-plan', json={
                        'schema_version': '1.0', 'goal': goal,
                        'lease_id': control['lease_id'], 'generation': control['generation']})
                    self.assertEqual(response.status_code, 202, response.text)
                    return response.json()

                for goal in ('Do not save message "gamma"', 'Save message "a" and "b"'):
                    rejected = plan(goal)
                    self.assertEqual(rejected['status'], 'needs_human')
                    self.assertFalse(rejected['model_called'])
                self.assertFalse((second.directory / 'owned-skill-plans').exists())
                verifier = OwnedSkillPlanning(BonsaiOwnedSkillPlanner(REPO_ROOT / 'models/bonsai-manifest.json', knowledge_context=True),
                                              second.directory / 'owned-skill-plans', None, None)
                for goal, literal in (('Save message "gamma"', 'gamma'),
                                      ('Mesaj alanına "delta" kaydet', 'delta')):
                    started = plan(goal)
                    self.assertEqual(started['status'], 'pending')
                    for attempt in range(900):
                        status = client.get('/api/tasks/owned-skill-plan').json()
                        if status['status'] != 'pending':
                            break
                        time.sleep(.2)
                    self.assertEqual(status['status'], 'ready', status)
                    self.assertTrue(status['real_model'])
                    self.assertTrue(status['model_called'])
                    self.assertFalse(status['execution_authorized'])
                    bundle = verifier.load(status['bundle_sha256'])
                    self.assertEqual(bundle['model_response']['parameter_value'], literal)
                    self.assertEqual(bundle['authority']['release_sha256'], release)
                    self.assertEqual(bundle['authority']['selection_sha256'], selection)
                    self.assertTrue(bundle['deployment']['real_model'])
                    self.assertGreater(bundle['metrics']['output_tokens'], 0)
                    self.assertEqual(helper.database_counts(database), counts)
                    print(canonical({'real_planner': bundle['deployment']['kind'],
                                     'language': 'en' if literal == 'gamma' else 'tr',
                                     'metrics': bundle['metrics'], 'actions_created': 0}), flush=True)
                plan('Save message "epsilon"')
                rejected = client.post('/api/tasks/owned-skill-plan', json={
                    'schema_version': '1.0', 'goal': 'Save message "zeta"',
                    'lease_id': control['lease_id'], 'generation': control['generation']})
                self.assertEqual(rejected.status_code, 409)
                response = client.post('/api/control', json={'command': 'pause'})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(client.get('/api/tasks/owned-skill-plan').json()['status'], 'cancelled')
                self.assertEqual(helper.database_counts(database), counts)
                self.assertEqual(len(list((second.directory / 'owned-skill-plans').glob('*.json'))), 2)
            self.assertEqual(read_isolated_state(base).phase, 'stopped')
            completed = True
        finally:
            if completed:
                shutil.rmtree(base)
            else:
                print('Preserved private planning acceptance evidence:', base, flush=True)
