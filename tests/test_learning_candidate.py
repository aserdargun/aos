import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from aos.contracts import REPO_ROOT
from aos.dataset import validator
from aos.learning_candidate import review_learning_candidates, summarize_learning_candidates
from aos.storage import TrajectoryStore


class LearningCandidateTests(unittest.TestCase):
    def test_role_separated_blocked_rows_from_synthetic_events(self):
        event = json.loads((REPO_ROOT / 'examples/learning_event_v2.json').read_text())
        fixture = json.loads((REPO_ROOT / 'examples/learning_candidate_report.json').read_text())
        self.assertTrue(fixture['synthetic'])
        validator('learning_candidate_report').validate(fixture['report'])
        source_review = {'schema_version': '1.0', 'mode': 'read_only_metadata_review',
                         'run_ref': 'c' * 64, 'snapshot_sha256': 'd' * 64,
                         'event_count': 1, 'events': [event],
                         'collection_authorized': False, 'training_ready': False}
        summary = summarize_learning_candidates(source_review)
        self.assertEqual(summary, fixture['report'])
        system2 = {**event, 'event_id': 'learning-' + 'b' * 64, 'role': 'system2',
                   'model_kind': 'bonsai_native_supervisor', 'verified_outcome': False,
                   'source': {**event['source'], 'deployment_id': 'bonsai-test',
                              'decision_id': None, 'verification_ids': [],
                              'scene_observation_id': 'scene-test',
                              'downstream_verification_ids': ['verification-test']},
                   'blockers': sorted(set(event['blockers']) | {'outcome_not_attributed',
                                                                 'supervisor_plan_not_linked'})}
        result = summarize_learning_candidates({**source_review, 'event_count': 2,
                                                'events': [system2, event]})
        self.assertEqual((result['system1_count'], result['system2_count']), (1, 1))
        self.assertEqual([row['role'] for row in result['rows']], ['system1', 'system2'])
        self.assertFalse(result['rows'][1]['has_independent_outcome'])
        self.assertTrue(result['rows'][1]['has_scene_evidence'])
        self.assertTrue(result['rows'][1]['has_downstream_verification'])
        self.assertTrue(all(row['status'] == 'blocked' and row['training_ready'] is False
                            for row in result['rows']))
        self.assertNotIn('private', json.dumps(result))
        with self.assertRaises(ValueError):
            summarize_learning_candidates({**source_review, 'events': [{**event, 'training_ready': True}]})
        for changed in ({'event_count': 2}, {'mode': 'collector'}, {'collection_authorized': True},
                        {'training_ready': True}, {'event_count': 2, 'events': [event, event]}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                summarize_learning_candidates({**source_review, **changed})

    def test_read_only_empty_run_and_safe_cli_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'store.sqlite'
            store = TrajectoryStore(path)
            with store.connection:
                store.insert('tasks', task_id='task-test', original_goal='synthetic',
                             normalized_goal='synthetic', success_criteria_json='[]',
                             workspace_scope_json='[]', created_at='2026-01-01T00:00:00Z')
                store.insert('runs', run_id='run-test', task_id='task-test', status='running',
                             outcome='unknown', policy_version='hello-policy-v1', environment_json='{}',
                             deployment_snapshot_json='{}', started_at='2026-01-01T00:00:00Z')
            store.close()
            before = path.read_bytes()
            report = review_learning_candidates(path, 'run-test')
            self.assertEqual(report['event_count'], 0)
            self.assertEqual(report['rows'], [])
            self.assertEqual(path.read_bytes(), before)
            command = [sys.executable, '-m', 'aos.learning_candidate', '--database', str(path),
                       '--run-id', 'run-test']
            process = subprocess.run(command, capture_output=True, text=True, timeout=10)
            self.assertEqual(process.returncode, 0)
            self.assertEqual(json.loads(process.stdout), report)
            self.assertEqual(path.read_bytes(), before)
            invalid = subprocess.run(command[:-1] + ['missing'], capture_output=True, text=True, timeout=10)
            self.assertEqual(invalid.returncode, 1)
            self.assertEqual(invalid.stdout, '')
            self.assertNotIn(str(path), invalid.stderr)
            linked = Path(directory) / 'linked.sqlite'
            linked.symlink_to(path)
            unsafe = subprocess.run([sys.executable, '-m', 'aos.learning_candidate',
                                     '--database', str(linked), '--run-id', 'run-test'],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(unsafe.returncode, 1)
            self.assertEqual(unsafe.stdout, '')
            self.assertNotIn(str(linked), unsafe.stderr)
            self.assertEqual(path.read_bytes(), before)
