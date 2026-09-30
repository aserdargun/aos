import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aos.contracts import REPO_ROOT, digest
from aos.dataset import validator
from aos.site_page_change import compare_site_page_change, main
from aos.storage import TrajectoryStore


PAGE = json.loads((REPO_ROOT / 'examples/site_page_evidence.json').read_text())['page']
FIXTURE = json.loads((REPO_ROOT / 'examples/site_page_change.json').read_text())


def evidence(run_id, fingerprint=None, pages=None, snapshot='c' * 64):
    selected = {**PAGE, 'page_fingerprint_sha256': fingerprint or PAGE['page_fingerprint_sha256']}
    return {'schema_version': '1.0', 'mode': 'read_only_synthetic_page_evidence',
            'run_ref': digest({'run_id': run_id}), 'snapshot_sha256': snapshot,
            'page_count': len(pages if pages is not None else [selected]),
            'pages': pages if pages is not None else [selected], 'profile_bound': False,
            'execution_authorized': False, 'collection_authorized': False,
            'training_ready': False}


class SitePageChangeTests(unittest.TestCase):
    def test_two_real_audited_snapshot_selections_are_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / 'trajectory.sqlite'
            trajectory = TrajectoryStore(database)
            try:
                for run_id in ('run-before', 'run-after'):
                    self._add_navigation_run(trajectory, run_id)
                before_bytes = database.read_bytes()
                report = compare_site_page_change(
                    database, 'run-before', 'run-after',
                    before_verification_id='verification-run-before',
                    after_verification_id='verification-run-after')
                self.assertEqual(report['status'], 'unchanged_unbound')
                self.assertEqual(report['page_key'], 'start')
                self.assertEqual(report['before']['snapshot_sha256'],
                                 report['after']['snapshot_sha256'])
                self.assertEqual(database.read_bytes(), before_bytes)
                self.assertNotIn('run-before', json.dumps(report))
                self.assertNotIn('verification-run-before', json.dumps(report))
            finally:
                trajectory.close()

    @staticmethod
    def _add_navigation_run(trajectory, run_id):
        outcome = {'page': 'start', 'heading': 'Synthetic start',
                   'links': [{'role': 'link', 'label': 'Details'}]}
        observed = {'page': 'start', 'heading': 'Synthetic start',
                    'snapshot_id': 'a' * 32,
                    'elements': [{'element_id': 'b' * 32, 'role': 'link', 'label': 'Details'}]}
        with trajectory.connection:
            trajectory.insert('tasks', task_id=f'task-{run_id}',
                              original_goal='private-do-not-export', normalized_goal='synthetic',
                              success_criteria_json='[]', workspace_scope_json='[]',
                              created_at='2026-01-01T00:00:00Z')
            trajectory.insert('runs', run_id=run_id, task_id=f'task-{run_id}',
                              status='running', outcome='unknown',
                              policy_version='browser-local-navigation-policy-v1',
                              environment_json='{"secret":"private-do-not-export"}',
                              deployment_snapshot_json=json.dumps({
                                  'deployment_id': 'decider-test', 'kind': 'decider_native_worker',
                                  'real_model': True, 'pins': {'revision': 'synthetic'}}),
                              started_at='2026-01-01T00:00:00Z')
            trajectory.insert('steps', step_id=f'step-{run_id}', run_id=run_id,
                              ordinal=0, state='DECIDE', started_at='2026-01-01T00:00:00Z')
            trajectory.insert('model_calls', call_id=f'call-{run_id}', run_id=run_id,
                              step_id=f'step-{run_id}', deployment_id='decider-test',
                              role='system1', request_json='{}', response_json='{}',
                              status='ok', created_at='2026-01-01T00:00:00Z')
            trajectory.insert('state_snapshots', snapshot_id=f'snapshot-{run_id}',
                              run_id=run_id, step_id=f'step-{run_id}', state_version=1,
                              state_json='{}', content_sha256='0' * 64,
                              created_at='2026-01-01T00:00:00Z')
            trajectory.insert('decisions', decision_id=f'decision-{run_id}',
                              run_id=run_id, step_id=f'step-{run_id}',
                              snapshot_id=f'snapshot-{run_id}', call_id=f'call-{run_id}',
                              question='synthetic', options_json='[{"id":"a"},{"id":"b"}]',
                              probabilities_json='{"a":1,"b":0}', selected_option='a',
                              confidence=1, policy_result='allow',
                              created_at='2026-01-01T00:00:00Z')
            trajectory.insert('actions', action_id=f'effect-{run_id}',
                              run_id=run_id, step_id=f'step-{run_id}',
                              decision_id=f'decision-{run_id}', idempotency_key=f'effect-key-{run_id}',
                              tool='browser.fixture.open', arguments_json='{}', status='ok',
                              actual_option='open_start', created_at='2026-01-01T00:00:00Z')
            trajectory.insert('actions', action_id=f'readback-{run_id}',
                              run_id=run_id, step_id=f'step-{run_id}',
                              decision_id=f'decision-{run_id}', idempotency_key=f'readback-key-{run_id}',
                              tool='browser.fixture.snapshot', arguments_json='{}', status='ok',
                              result_json=json.dumps(observed), actual_option='open_start',
                              created_at='2026-01-01T00:00:00Z')
            trajectory.insert('observations', observation_id=f'observation-{run_id}',
                              run_id=run_id, step_id=f'step-{run_id}',
                              action_id=f'readback-{run_id}', kind='browser.local_navigation',
                              payload_json=json.dumps(observed),
                              created_at='2026-01-01T00:00:00Z')
            trajectory.insert('verifications', verification_id=f'verification-{run_id}',
                              run_id=run_id, step_id=f'step-{run_id}',
                              action_id=f'effect-{run_id}', criterion='Exact synthetic Start page',
                              method='independent_local_page_equals', result='passed',
                              expected_json=json.dumps(outcome), actual_json=json.dumps(outcome),
                              evidence_refs_json=json.dumps([f'observation-{run_id}']),
                              verifier='aos-local-navigation-v1',
                              created_at='2026-01-01T00:00:00Z')

    def compare(self, before=None, after=None):
        reports = [before if before is not None else evidence('run-before'),
                   after if after is not None else evidence('run-after')]
        with patch('aos.site_page_change.review_site_page_evidence', side_effect=reports) as review:
            result = compare_site_page_change(
                Path('/private/trajectory.sqlite'), 'run-before', 'run-after',
                before_verification_id='verification-synthetic',
                after_verification_id='verification-synthetic')
        self.assertEqual([call.args[1] for call in review.call_args_list],
                         ['run-before', 'run-after'])
        return result

    def test_fixture_and_unchanged_result_are_metadata_only(self):
        self.assertTrue(FIXTURE['synthetic'])
        validator('site_page_change').validate(FIXTURE['report'])
        result = self.compare()
        self.assertEqual(result['status'], 'unchanged_unbound')
        self.assertEqual(result['page_key'], 'start')
        self.assertEqual(result['before']['snapshot_sha256'], result['after']['snapshot_sha256'])
        self.assertFalse(result['profile_bound'])
        self.assertFalse(result['reviewed'])
        self.assertFalse(result['training_ready'])
        self.assertEqual(result, self.compare())
        self.assertNotIn('verification-synthetic', json.dumps(result))
        self.assertNotIn('observation-synthetic', json.dumps(result))
        self.assertNotIn('/private/', json.dumps(result))

    def test_changed_result_does_not_claim_site_change(self):
        changed = self.compare(after=evidence('run-after', fingerprint='b' * 64))
        self.assertEqual(changed['status'], 'changed_unbound')
        self.assertNotEqual(changed['before']['page_fingerprint_sha256'],
                            changed['after']['page_fingerprint_sha256'])
        validator('site_page_change').validate(changed)
        for flag in ('profile_bound', 'reviewed', 'execution_authorized',
                     'collection_authorized', 'training_ready'):
            self.assertFalse(validator('site_page_change').is_valid({**changed, flag: True}))

    def test_rejects_duplicate_missing_and_ambiguous_page_sources(self):
        duplicate = [PAGE, {**PAGE, 'source': {**PAGE['source'],
                                               'verification_id': 'verification-other'}}]
        wrong_page = [{**PAGE, 'page_key': 'details'}]
        for source in (evidence('run-before', pages=[]),
                       evidence('run-before', pages=duplicate),
                       evidence('run-before', pages=[{**PAGE, 'source': {
                           **PAGE['source'], 'verification_id': 'other'}}]),
                       evidence('run-before', pages=[{**PAGE, 'profile_bound': True}]),
                       evidence('run-before', pages=wrong_page)):
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.compare(before=source)

    def test_rejects_changed_snapshot_and_mismatched_run_claim(self):
        for source in (evidence('run-after', snapshot='d' * 64),
                       evidence('different-run')):
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.compare(after=source)

    def test_rejects_duplicate_or_invalid_run_and_verification_ids_before_io(self):
        for before_run, after_run, verification in (
                ('run-same', 'run-same', 'verification-synthetic'),
                ('../secret', 'run-after', 'verification-synthetic'),
                ('run-before', 'run-after', '../secret')):
            with self.subTest(before_run=before_run, verification=verification):
                with patch('aos.site_page_change.review_site_page_evidence') as review:
                    with self.assertRaises(ValueError):
                        compare_site_page_change(Path('/private/trajectory.sqlite'),
                                                 before_run, after_run,
                                                 before_verification_id=verification,
                                                 after_verification_id='verification-synthetic')
                    review.assert_not_called()

    def test_cli_is_exact_and_suppresses_private_errors(self):
        output = io.StringIO()
        arguments = ['--database', '/private/trajectory.sqlite',
                     '--before-run-id', 'run-before',
                     '--before-verification-id', 'verification-synthetic',
                     '--after-run-id', 'run-after',
                     '--after-verification-id', 'verification-synthetic']
        with patch('aos.site_page_change.review_site_page_evidence',
                   side_effect=[evidence('run-before'), evidence('run-after')]):
            with contextlib.redirect_stdout(output):
                main(arguments)
        self.assertEqual(json.loads(output.getvalue()), self.compare())
        self.assertNotIn('/private/', output.getvalue())
        errors = io.StringIO()
        output = io.StringIO()
        with patch('aos.site_page_change.review_site_page_evidence',
                   side_effect=ValueError('private-do-not-export')):
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                with self.assertRaises(SystemExit) as failure:
                    main(arguments)
        self.assertEqual(failure.exception.code, 1)
        self.assertEqual(output.getvalue(), '')
        self.assertNotIn('private-do-not-export', errors.getvalue())
        with self.assertRaises(SystemExit) as arguments_error:
            main([*arguments, '--extra-secret'])
        self.assertEqual(str(arguments_error.exception),
                         'Site page change unavailable: invalid arguments.')


if __name__ == '__main__':
    unittest.main()
