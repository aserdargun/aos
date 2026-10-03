import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from aos.contracts import canonical
from aos.owned_candidate_review import (
    inspect_owned_candidate_review, make_owned_candidate_review,
    persist_owned_candidate_review, revoke_owned_candidate_review,
)


class OwnedCandidateReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source_directory = self.root / 'owned-form'
        self.source_directory.mkdir(mode=0o700)
        self.candidate = {
            'source_run_ref': '1' * 64,
            'source_context': {'invocation_sha256': '2' * 64},
            'source_group_sha256': '3' * 64,
            'source_fingerprint_sha256': '4' * 64,
            'skill': {'skill_key': 'save-record',
                      'expected_outcome_key': 'record-saved'},
            'recipe': {'steps': [
                {'step_key': 'open-entry', 'operation': 'open_entry'},
                {'step_key': 'read-before', 'operation': 'read_state_before'},
                {'step_key': 'fill-form', 'operation': 'fill_form'},
                {'step_key': 'submit-form', 'operation': 'submit_form'},
                {'step_key': 'read-receipt', 'operation': 'read_receipt'},
                {'step_key': 'read-after', 'operation': 'read_state_after'},
            ]},
            'field_bindings': [{'parameter_key': 'record-query',
                                'form_field_name': 'message'}],
        }
        self.execution = {
            'candidate_execution_sha256': '5' * 64,
            'run_ref': '6' * 64,
            'invocation_sha256': '7' * 64,
            'recipe_sha256': '8' * 64,
            'skill_sha256': '9' * 64,
            'profile_sha256': 'a' * 64,
            'case_key': 'dev-beta',
            'parameter_variant_sha256': 'b' * 64,
        }
        self.review_sha256, self.receipt = make_owned_candidate_review(
            self.candidate, 'c' * 64, self.execution)

    def tearDown(self):
        self.temporary.cleanup()

    def test_receipt_accept_revoke_is_immutable_and_not_reopened(self):
        accepted = persist_owned_candidate_review(
            self.source_directory, self.receipt, self.review_sha256)
        self.assertEqual(accepted['status'], 'accepted')
        self.assertEqual(accepted['summary'], self.receipt['summary'])

        repeated = persist_owned_candidate_review(
            self.source_directory, self.receipt, self.review_sha256)
        self.assertEqual(repeated, accepted)
        with self.assertRaisesRegex(ValueError, 'confirmation_invalid'):
            revoke_owned_candidate_review(
                self.source_directory, self.review_sha256, '0' * 64)

        revoked = revoke_owned_candidate_review(
            self.source_directory, self.review_sha256, self.review_sha256)
        self.assertEqual(revoked['status'], 'revoked')
        self.assertIsNotNone(revoked['revocation_sha256'])
        self.assertEqual(revoke_owned_candidate_review(
            self.source_directory, self.review_sha256,
            self.review_sha256), revoked)
        with self.assertRaisesRegex(ValueError, 'cannot_reopen'):
            persist_owned_candidate_review(
                self.source_directory, self.receipt, self.review_sha256)

    def test_one_review_slot_cannot_accept_a_different_receipt(self):
        persist_owned_candidate_review(
            self.source_directory, self.receipt, self.review_sha256)
        changed = dict(self.receipt)
        changed['reviewer'] = 'local_authenticated_user'
        changed['summary'] = {**changed['summary'], 'skill_key': 'other-skill'}
        from aos.contracts import digest
        other_sha256 = digest(changed)

        with self.assertRaisesRegex(ValueError, 'slot_already_used'):
            persist_owned_candidate_review(
                self.source_directory, changed, other_sha256)

    def test_receipt_tampering_and_symlinked_path_are_rejected(self):
        persist_owned_candidate_review(
            self.source_directory, self.receipt, self.review_sha256)
        path = (self.source_directory / 'candidate-reviews'
                / self.review_sha256 / 'receipt.json')
        path.write_bytes(path.read_bytes().replace(b'"decision":"accept"',
                                                    b'"decision":"revoke"'))
        path.chmod(0o600)
        with self.assertRaises(ValueError):
            inspect_owned_candidate_review(self.source_directory,
                                           self.review_sha256)

    def test_review_and_admission_examples_match_canonical_schemas(self):
        from aos.contracts import REPO_ROOT
        from aos.dataset import validator

        review_example = json.loads(
            (REPO_ROOT / 'examples/owned_candidate_review.json').read_text())
        admission_example = json.loads(
            (REPO_ROOT / 'examples/site_skill_recipe_candidate_review_admission.json').read_text())
        validator('owned_candidate_review_receipt').validate(review_example['receipt'])
        validator('owned_candidate_review_revocation').validate(review_example['revocation'])
        validator('site_skill_recipe_candidate_review_admission').validate(
            admission_example['admission'])

    def test_revoke_closes_matching_active_fixture_with_missing_or_done_future(self):
        import asyncio

        from aos.desktop_tasks import DesktopScheduler

        class Connection:
            def __init__(self):
                self.row = {'approval_id': 'approval-exact',
                            'action_sha256': 'd' * 64, 'status': 'pending'}

            def execute(self, sql, _parameters=()):
                if sql.startswith('SELECT approval_id,action_sha256'):
                    return SimpleNamespace(fetchone=lambda: self.row)
                if sql.startswith("UPDATE desktop_approvals SET status='revoked'"):
                    self.row['status'] = 'revoked'
                    return SimpleNamespace(rowcount=1)
                raise AssertionError('unexpected query')

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        async def exercise(answer_state):
            source = self.root / answer_state
            source.mkdir(mode=0o700)
            execution = {**self.execution,
                         'candidate_execution_sha256': answer_state.encode().hex().ljust(64, '0')[:64]}
            review_sha256, receipt = make_owned_candidate_review(
                self.candidate, 'c' * 64, execution)
            persist_owned_candidate_review(source, receipt, review_sha256)
            target = Mock()
            scheduler = DesktopScheduler.__new__(DesktopScheduler)
            scheduler.remote_form_owned_candidate_session = SimpleNamespace(directory=source)
            scheduler._active_owned_candidate_execution = {
                'review_sha256': review_sha256, 'lifecycle': 'running',
                'job_id': 'job-exact'}
            scheduler.remote_form_owned_target = target
            scheduler.store = SimpleNamespace(connection=Connection())
            if answer_state == 'missing':
                scheduler.answer = None
            else:
                scheduler.answer = asyncio.get_running_loop().create_future()
                if answer_state == 'done':
                    scheduler.answer.set_result(None)
            scheduler.revoke_owned_candidate_review(review_sha256, review_sha256)
            self.assertGreaterEqual(target.revoke.call_count, 1)
            self.assertEqual(scheduler._active_owned_candidate_execution['review_status'],
                             'revoked')
            if answer_state == 'pending':
                with self.assertRaises(Exception):
                    await scheduler.answer

        async def run_all():
            for state in ('missing', 'done', 'pending'):
                await exercise(state)

        asyncio.run(run_all())

    def test_review_admission_is_unique_pinned_and_between_initial_states(self):
        from aos.desktop_tasks import DesktopScheduler

        run_id = 'run-review-admission'
        execution = {
            'review_sha256': 'a' * 64,
            'candidate_execution_sha256': 'b' * 64,
            'candidate_sha256': 'c' * 64,
            'source_run_ref': 'd' * 64,
            'invocation_sha256': 'e' * 64,
            'run_id': run_id,
        }
        expected = {
            'schema_version': '1.0', 'synthetic': True,
            'review_sha256': execution['review_sha256'],
            'candidate_execution_sha256': execution['candidate_execution_sha256'],
            'candidate_sha256': execution['candidate_sha256'],
            'source_run_ref': execution['source_run_ref'],
            'review_status': 'accepted', 'review_admission_verified': True,
            'activation_authorized': False, 'training_ready': False,
            'independent_held_out': False,
        }
        created = ['2026-01-01T00:00:00.000000+00:00',
                   '2026-01-01T00:00:00.100000+00:00',
                   '2026-01-01T00:00:00.200000+00:00',
                   '2026-01-01T00:00:00.300000+00:00']

        def check(*, observation_times=(created[1],), observation_payloads=None,
                  include_next_state=True):
            connection = sqlite3.connect(':memory:')
            connection.row_factory = sqlite3.Row
            connection.executescript(
                'CREATE TABLE observations (run_id TEXT, kind TEXT, action_id TEXT, '
                'step_id TEXT, payload_json TEXT, created_at TEXT);'
                'CREATE TABLE state_snapshots (run_id TEXT, state_version INTEGER, '
                'step_id TEXT, state_json TEXT, created_at TEXT);'
                'CREATE TABLE actions (run_id TEXT, created_at TEXT);')
            state_identity = {
                'task_kind': 'browser_remote_form', 'task_id': 'task-review',
                'run_id': run_id, 'step_id': 'step-review',
                'runtime_id': 'runtime-review', 'deployment_id': 'deploy-review',
                'owner_lease_id': 'lease-review',
                'skill_invocation_sha256': execution['invocation_sha256'],
            }
            initial = {**state_identity, 'phase': 'CREATED', 'owner': 'AGENT',
                       'state_version': 0}
            next_state = {**state_identity, 'phase': 'OBSERVE', 'owner': 'AGENT',
                          'state_version': 1}
            connection.execute(
                'INSERT INTO state_snapshots VALUES (?,?,?,?,?)',
                (run_id, 0, 'step-review', json.dumps(initial, separators=(',', ':')),
                 created[0]))
            if include_next_state:
                connection.execute(
                    'INSERT INTO state_snapshots VALUES (?,?,?,?,?)',
                    (run_id, 1, 'step-review', json.dumps(next_state, separators=(',', ':')),
                     created[2]))
            payloads = observation_payloads or [expected for _ in observation_times]
            for timestamp, payload in zip(observation_times, payloads):
                connection.execute(
                    'INSERT INTO observations VALUES (?,?,?,?,?,?)',
                    (run_id, 'skill.recipe_candidate_review_admission', None,
                     'step-review', canonical(payload), timestamp))
            connection.execute('INSERT INTO actions VALUES (?,?)', (run_id, created[3]))
            scheduler = DesktopScheduler.__new__(DesktopScheduler)
            try:
                return scheduler._candidate_review_admission_verified(
                    execution, snapshot=connection)
            finally:
                connection.close()

        self.assertTrue(check())
        self.assertFalse(check(observation_times=()))
        self.assertFalse(check(observation_times=(created[1], created[1])))
        wrong_pin = {**expected, 'review_sha256': 'f' * 64}
        self.assertFalse(check(observation_payloads=[wrong_pin]))
        self.assertFalse(check(observation_times=('2025-12-31T23:59:59.999999+00:00',)))
        self.assertFalse(check(observation_times=('2026-01-01T00:00:00.250000+00:00',)))
        self.assertFalse(check(include_next_state=False))

    def test_historical_audit_rejects_receipt_from_different_candidate_scope(self):
        from contextlib import contextmanager

        from aos.contracts import REPO_ROOT, digest
        from aos.desktop_tasks import DesktopScheduler

        report = json.loads((REPO_ROOT / 'examples/site_skill_form_recipe_candidate_execution.json')
                            .read_text())['report']
        admission = report['admission']
        run_id = 'run-reviewed-historical'
        report['execution_run_ref'] = digest({'run_id': run_id})
        execution = {
            'schema_version': '1.1', 'mode': 'owned_candidate_development',
            'lifecycle': 'completed', 'candidate_execution_sha256': '7' * 64,
            'candidate_sha256': admission['candidate_sha256'],
            'source_run_ref': admission['source_run_ref'],
            'source_invocation_sha256': '8' * 64,
            'source_group_sha256': admission['source_group_sha256'],
            'source_fingerprint_sha256': '9' * 64,
            'profile_sha256': admission['profile_sha256'],
            'skill_sha256': admission['skill_sha256'],
            'recipe_sha256': admission['recipe_sha256'],
            'case_key': 'dev-gamma',
            'parameter_variant_sha256': admission['parameter_variant_sha256'],
            'invocation_sha256': admission['invocation_sha256'],
            'steps': [{'step_key': 'open-entry', 'operation': 'open_entry'}],
            'job_id': 'job-reviewed-historical', 'run_id': run_id,
            'run_ref': report['execution_run_ref'], 'report_sha256': None,
            'review_sha256': 'a' * 64, 'review_status': 'revoked',
            'bundle_directory': Path('/private/bundle'),
        }
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute('CREATE TABLE desktop_tasks (job_id TEXT, kind TEXT, status TEXT, run_id TEXT)')
        connection.execute('INSERT INTO desktop_tasks VALUES (?,?,?,?)',
                           (execution['job_id'], 'browser_remote_form', 'succeeded', run_id))

        @contextmanager
        def shared_snapshot(_database):
            yield connection, {'sha256': 'c' * 64}

        def audit_report(*_args, **_kwargs):
            return report

        def audit_with_receipt(receipt):
            scheduler = DesktopScheduler.__new__(DesktopScheduler)
            scheduler.web_goal_planning = None
            scheduler.owned_skill_planning = None
            scheduler.task = None
            scheduler.sequences = SimpleNamespace(reserved=False)
            scheduler.job_id = execution['job_id']
            scheduler.store = SimpleNamespace(connection=connection)
            scheduler.settings = SimpleNamespace(database=Path('/private/store.sqlite'))
            scheduler.remote_form_owned_candidate_session = object()
            scheduler._owned_candidate_execution_history = {
                execution['candidate_execution_sha256']: dict(execution)}
            from unittest.mock import patch
            with patch('aos.dataset_audit.audit_snapshot', shared_snapshot), \
                 patch('aos.owned_form_candidate_execution.audit_persisted_candidate_execution',
                       side_effect=audit_report), \
                 patch.object(scheduler, 'inspect_owned_candidate_review', return_value={
                     'status': 'revoked', 'receipt': receipt}), \
                 patch.object(scheduler, '_candidate_review_admission_verified',
                              return_value=True):
                return scheduler.audit_owned_form_candidate_execution(
                    execution['candidate_execution_sha256'])

        base_receipt = {
            'candidate_sha256': execution['candidate_sha256'],
            'source_run_ref': execution['source_run_ref'],
            'source_invocation_sha256': execution['source_invocation_sha256'],
            'source_group_sha256': execution['source_group_sha256'],
            'source_fingerprint_sha256': execution['source_fingerprint_sha256'],
            'profile_sha256': execution['profile_sha256'],
            'skill_sha256': execution['skill_sha256'],
            'recipe_sha256': execution['recipe_sha256'],
        }
        good = audit_with_receipt(base_receipt)
        self.assertTrue(good['available'], good)
        for key in base_receipt:
            wrong = {**base_receipt, key: 'f' * 64}
            result = audit_with_receipt(wrong)
            self.assertFalse(result['available'], key)
            self.assertIsNone(result['report'])


if __name__ == '__main__':
    unittest.main()
