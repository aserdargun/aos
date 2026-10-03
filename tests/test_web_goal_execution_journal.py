from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from aos.contracts import REPO_ROOT, canonical, digest
from aos.dataset import validator
from aos.web_goal_execution_binding import (
    build_web_goal_execution_binding, confirm_web_goal_execution_binding,
    read_web_goal_whole_record,
)
from aos.web_goal_execution_journal import (
    WebGoalExecutionIntent, WebGoalExecutionJournal, WebGoalExecutionJournalStatus,
    WebGoalExecutionRunBinding, WebGoalExecutionTerminalReceipt,
)
from test_web_goal_execution_binding import example, source_fixture


class WebGoalExecutionJournalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.arguments = source_fixture(self.root / 'source', 'synthetic-journal-crm',
                                        'contact_name', 'Ada', 'Call tomorrow')
        self.binding = build_web_goal_execution_binding(**self.arguments)
        self.source = self.binding.source
        self.authority = self.binding.authority
        self.current = {'current_source': lambda: self.source, 'current_authority': lambda: self.authority}
        self.confirmation = confirm_web_goal_execution_binding(self.binding,
            confirm_sha256=self.binding.confirm_sha256, human_confirmation=True, **self.current)
        self.directory = self.root / 'execution-journal'
        self.journal = WebGoalExecutionJournal(self.directory)
        self.run = {'task_id': 'task-' + '1' * 32, 'run_id': 'run-' + '2' * 32,
            'runtime_id': self.authority.runtime_id, 'deployment_id': 'synthetic-fixture-deployment',
            'owner_lease_id': self.authority.lease_id,
            'skill_invocation_sha256': digest(self.binding.invocation.model_dump(mode='json'))}
        self.bind_current = self.current | {'expected_execution_runtime_id': self.run['runtime_id']}

    def begin(self, journal=None):
        return (journal or self.journal).begin(self.binding, self.confirmation, **self.current)

    def proof(self):
        invocation = self.binding.invocation.model_dump(mode='json')
        audit = deepcopy(example('site_skill_form_recipe_audit.json')['report'])
        for field in ('profile_sha256', 'task_sha256', 'skill_sha256', 'skill_plan_sha256',
                      'case_inputs_sha256', 'form_plan_sha256', 'state_plan_sha256',
                      'field_binding_sha256', 'recipe_sha256', 'case_key'):
            audit[field] = invocation[field]
        audit['symbolic_step_keys'] = [step['step_key'] for step in invocation['steps']]
        audit['invocation_sha256'] = digest(invocation)
        audit['run_ref'] = digest({'run_id': self.run['run_id']})
        readback = read_web_goal_whole_record(self.binding, self.confirmation, **self.current,
            host_readback=lambda request: canonical({'schema_version': '1.0',
                'scope': self.binding.oracle.scope.model_dump(mode='json'),
                'record': self.binding.oracle.expected_fields,
                'reported_post_count': 1, 'effect_status': 'committed'}).encode())
        return audit, readback.model_dump(mode='json')

    def finish(self, checksum, journal=None, **changes):
        audit, readback = self.proof()
        return (journal or self.journal).finish(checksum, **({'run_identity': self.run,
            'terminal_status': 'succeeded', 'recipe_audit': audit, 'readback': readback,
            **self.current} | changes))

    def snapshot(self):
        return {path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.directory.iterdir()}

    def test_intent_is_durable_before_dispatch_and_fresh_instance_never_replays(self):
        self.assertFalse(self.journal.reserved)
        self.assertFalse(self.directory.exists())
        checksum = self.begin()
        self.assertEqual(len(list(self.directory.iterdir())), 1)
        fresh = WebGoalExecutionJournal(self.directory)
        self.assertTrue(fresh.reserved)
        report = fresh.inspect(checksum)
        self.assertEqual(report['status'], 'uncertain_before_run_binding')
        self.assertFalse(report['task_terminal_verified'] or report['record_outcome_verified'])
        self.assertTrue(report['confirmation_consumed'])
        self.assertFalse(report['replay_authorized'])
        with self.assertRaisesRegex(ValueError, 'consumed_or_unresolved'):
            self.begin(fresh)

    def test_exact_run_bind_is_idempotent_without_new_writes_and_foreign_run_rejected(self):
        checksum = self.begin()
        for changes in ({'runtime_id': 'foreign-runtime'}, {'owner_lease_id': 'foreign-lease'},
                        {'skill_invocation_sha256': '0' * 64}):
            with self.assertRaises(ValueError):
                self.journal.bind_run(checksum, self.run | changes, **self.bind_current)
        bound = self.journal.bind_run(checksum, self.run, **self.bind_current)
        before = self.snapshot()
        fresh = WebGoalExecutionJournal(self.directory)
        self.assertEqual(fresh.bind_run(checksum, self.run, **self.bind_current), bound)
        self.assertEqual(self.snapshot(), before)
        with self.assertRaisesRegex(ValueError, 'run_already_bound'):
            fresh.bind_run(checksum, self.run | {'run_id': 'run-' + '3' * 32}, **self.bind_current)
        report = fresh.inspect(checksum)
        self.assertEqual(report['status'], 'awaiting_independent_verification')
        self.assertTrue(report['reserved'])
        self.assertFalse(report['task_terminal_verified'])

    def test_verified_fixture_receipt_frees_reservation_but_consumes_confirmation_forever(self):
        checksum = self.begin()
        self.journal.bind_run(checksum, self.run, **self.bind_current)
        receipt = self.finish(checksum)
        fresh = WebGoalExecutionJournal(self.directory)
        self.assertFalse(fresh.reserved)
        report = fresh.inspect(checksum)
        self.assertEqual(report['status'], 'accepted_verified')
        self.assertTrue(report['task_terminal_verified'] and report['record_outcome_verified'])
        self.assertEqual(report['receipt_sha256'], digest(receipt))
        self.assertFalse(report['site_outcome_verified'] or report['training_ready'] or report['gpu_release_verified'])
        self.assertNotIn('binding', report)
        self.assertNotIn('recipe_audit', report)
        before = self.snapshot()
        self.assertEqual(self.finish(checksum, fresh), receipt)
        self.assertEqual(self.snapshot(), before)
        with self.assertRaisesRegex(ValueError, 'consumed_or_unresolved'):
            self.begin(fresh)

    def test_terminal_status_or_record_match_alone_cannot_release(self):
        checksum = self.begin()
        with self.assertRaisesRegex(ValueError, 'terminal_run_not_bound'):
            self.finish(checksum)
        self.journal.bind_run(checksum, self.run, **self.bind_current)
        for changes in ({'terminal_status': 'failed'}, {'terminal_status': 'cancelled'},
                        {'recipe_audit': None}, {'readback': None}):
            with self.assertRaises(ValueError):
                self.finish(checksum, **changes)
        self.assertTrue(WebGoalExecutionJournal(self.directory).reserved)
        self.assertFalse((self.directory / (checksum + '.accepted.json')).exists())

    def test_child_runtime_is_exactly_host_bound_separately_from_desktop_authority(self):
        checksum = self.begin()
        self.run['runtime_id'] = 'synthetic-child-browser-runtime'
        with self.assertRaises(ValueError):
            self.journal.bind_run(checksum, self.run,
                expected_execution_runtime_id='foreign-child-runtime', **self.current)
        bound = self.journal.bind_run(checksum, self.run,
            expected_execution_runtime_id=self.run['runtime_id'], **self.current)
        self.assertNotEqual(bound['execution_runtime_id'], self.authority.runtime_id)
        self.assertEqual(bound['execution_runtime_id'], self.run['runtime_id'])
        receipt = self.finish(checksum)
        self.assertEqual(receipt['run_identity']['runtime_id'], 'synthetic-child-browser-runtime')
        self.assertFalse(WebGoalExecutionJournal(self.directory).reserved)

    def test_wrong_permitted_stage_order_does_not_match_bound_recipe(self):
        checksum = self.begin()
        self.journal.bind_run(checksum, self.run, **self.bind_current)
        audit, _readback = self.proof()
        audit['stages'][1], audit['stages'][2] = audit['stages'][2], audit['stages'][1]
        with self.assertRaisesRegex(ValueError, 'trajectory_changed'):
            self.finish(checksum, recipe_audit=audit)
        self.assertTrue(WebGoalExecutionJournal(self.directory).reserved)

    def test_changed_source_session_owner_generation_or_status_never_binds_or_releases(self):
        checksum = self.begin()
        self.journal.bind_run(checksum, self.run, **self.bind_current)
        audit, readback = self.proof()
        original_authority = self.authority
        for changes in ({'manager_session': 'foreign-manager'}, {'desktop_session_id': 'foreign-session'},
                        {'generation': 3}, {'owner': 'HUMAN'}, {'status': 'paused'}):
            self.authority = original_authority.model_copy(update=changes)
            with self.assertRaises(ValueError):
                self.journal.bind_run(checksum, self.run, **self.bind_current)
            with self.assertRaises(ValueError):
                self.journal.finish(checksum, run_identity=self.run, terminal_status='succeeded',
                    recipe_audit=audit, readback=readback, **self.current)
        self.authority = original_authority
        self.source = self.source.model_copy(update={'review_sha256': '0' * 64})
        with self.assertRaises(ValueError):
            self.journal.finish(checksum, run_identity=self.run, terminal_status='succeeded',
                recipe_audit=audit, readback=readback, **self.current)
        self.assertTrue(WebGoalExecutionJournal(self.directory).reserved)

    def test_mismatching_trajectory_or_record_proof_is_uncertain_across_restart(self):
        checksum = self.begin()
        self.journal.bind_run(checksum, self.run, **self.bind_current)
        audit, readback = self.proof()
        for field in ('run_ref', 'profile_sha256', 'task_sha256', 'recipe_sha256',
                      'field_binding_sha256', 'invocation_sha256'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.finish(checksum, recipe_audit=audit | {field: '0' * 64})
        for field in ('binding_sha256', 'confirmation_sha256', 'oracle_sha256', 'source_sha256',
                      'request_sha256', 'observed_record_sha256'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.finish(checksum, readback=readback | {field: '0' * 64})
        self.assertTrue(WebGoalExecutionJournal(self.directory).reserved)
        self.assertEqual(len(list(self.directory.iterdir())), 2)

    def test_accepted_receipt_cannot_be_overwritten_by_new_run_or_new_proof(self):
        checksum = self.begin()
        self.journal.bind_run(checksum, self.run, **self.bind_current)
        self.finish(checksum)
        audit, _readback = self.proof()
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.finish(checksum, run_identity=self.run | {'deployment_id': 'foreign-deployment'})
        with self.assertRaisesRegex(ValueError, 'terminal_already_bound'):
            self.finish(checksum, recipe_audit=audit | {'source_snapshot_sha256': '0' * 64})
        self.assertEqual(self.snapshot(), before)

    def test_confirmation_source_and_authority_must_match_original_binding(self):
        for changes in ({'binding_sha256': '0' * 64}, {'source_sha256': '0' * 64},
                        {'authority': self.authority.model_dump(mode='json') | {'generation': 3}}):
            confirmation = self.confirmation.model_dump(mode='json') | changes
            confirmation['confirm_sha256'] = digest({key: value for key, value in confirmation.items()
                                                     if key != 'confirm_sha256'})
            with self.assertRaises(ValueError):
                self.journal.begin(self.binding, confirmation, **self.current)
        self.assertFalse(self.journal.reserved)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_parallel_fresh_instances_consume_exact_confirmation_once(self):
        journals = [WebGoalExecutionJournal(self.directory) for _ in range(2)]

        def start(journal):
            try:
                return self.begin(journal)
            except ValueError:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(start, journals))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertTrue(WebGoalExecutionJournal(self.directory).reserved)
        self.assertEqual(len(list(self.directory.iterdir())), 1)

    def test_copied_journal_into_replacement_directory_cannot_adopt_intents(self):
        checksum = self.begin()
        moved = self.root / 'old-journal'
        self.directory.rename(moved)
        self.directory.mkdir(mode=0o700)
        for path in moved.iterdir():
            shutil.copy2(path, self.directory / path.name)
        fresh = WebGoalExecutionJournal(self.directory)
        self.assertTrue(fresh.reserved)
        with self.assertRaisesRegex(ValueError, 'intent_changed'):
            fresh.inspect(checksum)
        with self.assertRaises(ValueError):
            self.begin(fresh)

    def test_noncanonical_symlink_hardlink_and_unsafe_modes_fail_closed(self):
        checksum = self.begin()
        path = next(self.directory.iterdir())
        original = path.read_bytes()
        path.write_bytes(original + b'\n')
        fresh = WebGoalExecutionJournal(self.directory)
        self.assertTrue(fresh.reserved)
        with self.assertRaises(ValueError):
            fresh.inspect(checksum)
        path.write_bytes(original)
        os.chmod(path, 0o644)
        self.assertTrue(WebGoalExecutionJournal(self.directory).reserved)
        os.chmod(path, 0o600)
        target = self.root / 'alias.json'
        os.link(path, target)
        self.assertTrue(WebGoalExecutionJournal(self.directory).reserved)
        target.unlink()
        path.unlink()
        target.write_bytes(original)
        os.chmod(target, 0o600)
        path.symlink_to(target)
        self.assertTrue(WebGoalExecutionJournal(self.directory).reserved)

    def test_source_change_between_checks_cannot_publish_run_binding(self):
        checksum = self.begin()
        calls = []

        def current_source():
            calls.append(True)
            return (self.source if len(calls) == 1 else
                    self.source.model_copy(update={'reuse_admission_sha256': '0' * 64}))

        with self.assertRaises(ValueError):
            self.journal.bind_run(checksum, self.run, expected_execution_runtime_id=self.run['runtime_id'],
                                  current_source=current_source,
                                  current_authority=lambda: self.authority)
        self.assertEqual(len(list(self.directory.iterdir())), 1)
        self.assertTrue(WebGoalExecutionJournal(self.directory).reserved)

    def test_parent_swap_during_confirmation_cannot_return_admitted_intent(self):
        checks = []
        moved = self.root.with_name(self.root.name + '-moved')
        self.addCleanup(lambda: shutil.rmtree(moved, ignore_errors=True))

        def authority():
            checks.append(True)
            if len(checks) == 2:
                self.root.rename(moved)
                self.root.mkdir(mode=0o700)
            return self.authority

        with self.assertRaises((OSError, ValueError)):
            self.journal.begin(self.binding, self.confirmation,
                current_source=lambda: self.source, current_authority=authority)
        self.assertFalse(self.directory.exists())
        old_directory = moved / 'execution-journal'
        self.assertEqual(len(list(old_directory.iterdir())), 1)
        self.assertTrue(self.journal.reserved)

    def test_orphan_receipt_and_interrupted_private_publication_block_new_work(self):
        checksum = self.begin()
        path = self.directory / (checksum + '.accepted.json')
        path.write_bytes(b'{}')
        os.chmod(path, 0o600)
        self.assertTrue(WebGoalExecutionJournal(self.directory).reserved)
        with self.assertRaises(ValueError):
            self.begin(WebGoalExecutionJournal(self.directory))
        path.unlink()
        temporary = self.directory / '.candidate-interrupted'
        temporary.write_bytes(b'synthetic incomplete publication')
        os.chmod(temporary, 0o600)
        self.assertTrue(WebGoalExecutionJournal(self.directory).reserved)
        with self.assertRaises(ValueError):
            WebGoalExecutionJournal(self.directory).inspect(checksum)

    def test_canonical_schemas_match_journal_records_and_hashonly_status(self):
        for name, model in (
                ('web_goal_execution_intent', WebGoalExecutionIntent),
                ('web_goal_execution_run_binding', WebGoalExecutionRunBinding),
                ('web_goal_execution_terminal_receipt', WebGoalExecutionTerminalReceipt),
                ('web_goal_execution_journal_status', WebGoalExecutionJournalStatus)):
            schema = model.model_json_schema()
            schema['$schema'] = 'https://json-schema.org/draft/2020-12/schema'
            schema['$id'] = name + '.schema.json'
            stored = json.loads((REPO_ROOT / 'schemas' / (name + '.schema.json')).read_text())
            self.assertEqual(stored, schema)

    def test_typed_boolean_flags_and_status_cannot_promote_missing_receipt(self):
        checksum = self.begin()
        status = self.journal.inspect(checksum)
        for changes in ({'task_terminal_verified': True}, {'record_outcome_verified': True},
                        {'reserved': False}, {'status': 'accepted_verified'},
                        {'replay_authorized': 0}, {'gpu_release_verified': 0}):
            with self.subTest(changes=changes):
                self.assertFalse(validator('web_goal_execution_journal_status').is_valid(status | changes))
                with self.assertRaises(ValueError):
                    WebGoalExecutionJournalStatus.model_validate(status | changes)
