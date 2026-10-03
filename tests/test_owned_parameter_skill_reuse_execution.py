"""Durable manual reuse coordinator acceptance with actual CPU operator and TLS."""

import asyncio
from copy import deepcopy
import json
import shutil
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from aos.contracts import REPO_ROOT, canonical, digest, identifier
from aos.desktop_tasks import DesktopScheduler
from aos.owned_learning_workspace import OwnedLearningWorkspace
from aos.owned_parameter_project_startup import load_owned_parameter_project_startup
from aos.owned_parameter_skill_release import RELEASE_CONFIRMATION, SELECT_CONFIRMATION
from aos.owned_parameter_skill_reuse import OwnedParameterSkillReuseSession
from aos.owned_parameter_skill_reuse_execution import OwnedParameterSkillReuseExecution
from aos.web_application_binding import WebRuntimePin
from aos.web_goal_execution_journal import WebGoalExecutionJournal

import test_owned_parameter_project_desktop as desktop_fixture
from test_owned_parameter_skill_candidate import create_accepted_bootstrap
from test_web_goal_desktop_execution import TLSFixtureBrowserBackend


@unittest.skipUnless(shutil.which('openssl'), 'Requires local synthetic TLS preparation')
class OwnedParameterSkillReuseExecutionTests(unittest.TestCase):
    def setUp(self):
        captured = {}

        def startup(*arguments, **keywords):
            captured['startup'] = load_owned_parameter_project_startup(*arguments, **keywords)
            return captured['startup']

        def manager(*arguments, **keywords):
            captured['manager'] = DesktopScheduler(*arguments, **keywords)
            return captured['manager']

        with patch.object(desktop_fixture, 'load_owned_parameter_project_startup', side_effect=startup), \
                patch.object(desktop_fixture, 'DesktopScheduler', side_effect=manager):
            self.source = create_accepted_bootstrap(self)
        self.manager = captured['manager']
        self.startup = captured['startup']
        self.source_lock = OwnedLearningWorkspace.acquire(self.source.directory)
        self.addCleanup(self.source_lock.close)
        self.startup.retain_workspace(self.source_lock)
        self.original_fixture = self.manager.remote_form_owned_fixture
        self.assertTrue(self.original_fixture._closed)
        candidates = self.manager.parameter_skill_candidate_session()
        _candidate, candidate_sha = candidates.preview(self.source.intent_sha256)
        candidates.publish(self.source.intent_sha256, candidate_sha, 'PUBLISH_MANUAL_CANDIDATE')
        reviews = self.manager.parameter_skill_review_session()
        _review, review_sha = reviews.preview(candidate_sha)
        reviews.accept(candidate_sha, review_sha, 'ACCEPT_MANUAL_REVIEW')
        self.reviewed = SimpleNamespace(reviews=reviews, review_sha256=review_sha)
        self.releases = self.manager.parameter_skill_release_session()
        _record, self.release_sha = self.releases.preview(self.reviewed.review_sha256)
        self.releases.release(self.reviewed.review_sha256, None, self.release_sha, RELEASE_CONFIRMATION)
        _record, self.selection_sha = self.releases.selection_preview(self.release_sha)
        self.releases.select(self.release_sha, None, 'select', self.selection_sha, SELECT_CONFIRMATION)
        self.reuse = OwnedParameterSkillReuseSession(self.releases)
        self.journal = WebGoalExecutionJournal(self.source.root / 'reuse-execution')
        self.control = self.manager.controller.state()
        self.parameters = {'record-id': 'Synthetic second record', 'note-text': 'New separately approved note'}
        self.events = []
        self.children = []
        self.bundles = []

    def service(self, prepare=None, journal=None):
        if prepare is None and journal is None:
            with patch.object(self.manager, '_prepare_parameter_skill_reuse_manager',
                              side_effect=self.prepare_child):
                service = self.manager.parameter_skill_reuse_execution_session()
            self.journal = service.journal
        else:
            service = OwnedParameterSkillReuseExecution(
                self.manager, self.reuse, journal or self.journal,
                manager_session=self.manager.controller.session_id,
                prepare_manager=prepare or self.prepare_child)
        self.addCleanup(service.close)
        return service

    def preview(self, service):
        return service.preview(self.release_sha, self.selection_sha, self.parameters,
                               lease_id=self.control['lease_id'], generation=self.control['generation'])

    def start(self, service, preview, **changes):
        arguments = {'confirm_sha256': preview['confirm_sha256'], 'human_confirmation': True,
                     'lease_id': self.control['lease_id'], 'generation': self.control['generation']}
        return service.start(preview['admission'], **(arguments | changes))

    def prepare_child(self, bundle):
        intent_path, = self.journal.directory.glob('*.intent.json')
        intent = json.loads(intent_path.read_text())
        self.assertEqual(intent['binding']['source']['reuse_admission_sha256'], bundle['admission_sha256'])
        self.assertTrue(WebGoalExecutionJournal(self.journal.directory).reserved)
        self.events.append(('durable_intent_before_child', digest(intent)))
        child = self.manager._prepare_parameter_skill_reuse_manager(bundle)
        self.addCleanup(child.remote_form_owned_fixture.close)
        self.children.append(child)
        self.bundles.append(bundle)
        self.addCleanup(lambda: asyncio.run(child.close()))
        return child

    def original_rows(self):
        run_id = self.source.receipt['run_identity']['run_id']
        connection = self.manager.store.connection
        return {table: [tuple(row) for row in connection.execute(
            'SELECT * FROM ' + table + ' WHERE run_id=? ORDER BY rowid', (run_id,))]
            for table in ('runs', 'actions', 'observations', 'verifications', 'state_snapshots')}

    def parent_configuration(self):
        return {key: deepcopy(getattr(self.manager, key)) for key in (
            'remote_form_fields', 'remote_form_skill_invocation', 'remote_form_skill_invocation_sha256',
            'remote_form_owned_manifest', '_owned_form_lifecycle', '_owned_form_run_id',
            '_owned_form_source_job_id', 'job_id')}

    async def execute(self, service, preview, *, revoke_pending_fill=False):
        approvals = []
        revoked = False

        def backend(*_arguments, **_keywords):
            bundle = self.bundles[-1]
            runtime = json.loads((REPO_ROOT / 'examples/web_application_binding.json').read_text())['runtime']
            pin = WebRuntimePin.model_validate(runtime | {
                'runtime_id': identifier('browser'),
                'parent_runtime_id': self.manager.controller.runtime.runtime_id})
            return TLSFixtureBrowserBackend(pin, bundle, self.events)

        with patch('aos.desktop_form_mcp.DesktopHTTPSFormMCPRuntime', side_effect=backend):
            started = self.start(service, preview)
            child = service.child_manager
            deadline = asyncio.get_running_loop().time() + 15
            while not child.task.done():
                if asyncio.get_running_loop().time() >= deadline:
                    child.task.cancel()
                    await asyncio.gather(child.task, return_exceptions=True)
                    self.fail('Synthetic reuse task exceeded its fifteen-second bound')
                pending = child.store.connection.execute(
                    "SELECT * FROM desktop_approvals WHERE job_id=? AND status='pending'",
                    (started['job_id'],)).fetchone()
                if pending is not None:
                    if revoke_pending_fill and not revoked and any(
                            event == ('tool', 'browser.form.state_before') for event in self.events):
                        _record, checksum = self.reviewed.reviews.revocation_preview(self.reviewed.review_sha256)
                        self.reviewed.reviews.revoke(self.reviewed.review_sha256, checksum, 'REVOKE_MANUAL_REVIEW')
                        revoked = True
                    approvals.append(pending['approval_id'])
                    try:
                        self.manager.respond(pending['approval_id'], pending['action_sha256'], True)
                    except Exception:
                        if not revoked:
                            raise
                await asyncio.sleep(.001)
            await child.task
        return started, child, approvals, revoked

    def test_new_actual_cpu_tls_run_has_durable_intent_fresh_approvals_and_independent_readback(self):
        service = self.service()
        before_rows = self.original_rows()
        before_parent = self.parent_configuration()
        preview = self.preview(service)
        self.assertFalse(self.journal.directory.exists())
        self.assertEqual(self.events, [])
        started, child, approvals, _revoked = asyncio.run(self.execute(service, preview))
        row = child.store.connection.execute('SELECT * FROM desktop_tasks WHERE job_id=?',
                                              (started['job_id'],)).fetchone()
        self.assertEqual(row['status'], 'succeeded', dict(row))
        self.assertNotEqual(row['run_id'], self.source.receipt['run_identity']['run_id'])
        self.assertEqual(len(approvals), 6)
        self.assertEqual(len(set(approvals)), 6)
        self.assertEqual(sum(event == ('tool', 'browser.form.submit') for event in self.events), 1)
        self.assertEqual(self.events[0][0], 'durable_intent_before_child')
        receipt = service.execution.receipt
        self.assertTrue(receipt['record_outcome_verified'])
        self.assertTrue(receipt['recipe_execution_verified'])
        self.assertEqual(receipt['recipe_audit']['consumed_approval_count'], 6)
        self.assertEqual(receipt['recipe_audit']['invocation_sha256'], preview['admission']['invocation_sha256'])
        self.assertEqual(receipt['readback']['observed_record_sha256'], preview['admission']['expected_record_sha256'])
        self.assertEqual(child.remote_form_owned_fixture._state.committed_record,
                         {'contact_name': self.parameters['record-id'], 'note': self.parameters['note-text']})
        self.assertTrue(child.remote_form_owned_fixture._readback_used)
        self.assertTrue(child.remote_form_owned_fixture._closed)
        self.assertEqual(service.status()['status'], 'accepted_verified')
        self.assertFalse(service.reserved)
        self.assertEqual(self.original_rows(), before_rows)
        self.assertEqual(self.parent_configuration(), before_parent)
        self.assertIs(self.manager.remote_form_owned_fixture, self.original_fixture)
        self.assertEqual(self.manager.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 14)
        self.assertEqual(self.manager.store.connection.execute('SELECT count(*) FROM model_calls').fetchone()[0], 0)
        self.assertEqual(self.manager.store.connection.execute('SELECT count(*) FROM trajectory_labels').fetchone()[0], 0)
        for key in ('site_outcome_verified', 'activation_authorized', 'training_ready', 'gpu_release_verified'):
            self.assertIs(receipt[key], False)
        with self.assertRaises(ValueError):
            self.start(service, preview)
        fresh = self.service(journal=WebGoalExecutionJournal(self.journal.directory))
        with self.assertRaises(ValueError):
            self.start(fresh, preview)

    def test_confirmation_control_and_revocation_reject_before_child_or_intent(self):
        prepare = Mock(side_effect=AssertionError('Child creation must not be reached'))
        service = self.service(prepare)
        preview = self.preview(service)
        for changes in ({'confirm_sha256': 'a' * 64}, {'human_confirmation': False},
                        {'human_confirmation': 1}, {'lease_id': 'different-lease'}, {'generation': True},
                        {'generation': self.control['generation'] + 1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.start(service, preview, **changes)
        _record, checksum = self.reviewed.reviews.revocation_preview(self.reviewed.review_sha256)
        self.reviewed.reviews.revoke(self.reviewed.review_sha256, checksum, 'REVOKE_MANUAL_REVIEW')
        with self.assertRaises(ValueError):
            self.start(service, preview)
        prepare.assert_not_called()
        self.assertFalse(self.journal.directory.exists())

    def test_callback_failure_leaves_fresh_instance_unresolved_fence_and_no_replay(self):
        def failure(_bundle):
            self.assertTrue(WebGoalExecutionJournal(self.journal.directory).reserved)
            self.assertEqual(len(list(self.journal.directory.glob('*.intent.json'))), 1)
            raise ValueError('synthetic child preparation failed')

        prepare = Mock(side_effect=failure)
        service = self.service(prepare)
        preview = self.preview(service)
        with self.assertRaisesRegex(ValueError, 'synthetic child preparation failed'):
            self.start(service, preview)
        self.assertEqual(service.status()['status'], 'uncertain_before_run_binding')
        self.assertTrue(service.reserved)
        fresh = self.service(prepare, WebGoalExecutionJournal(self.journal.directory))
        for existing in (service, fresh):
            with self.assertRaises(ValueError):
                self.start(existing, preview)
            with self.assertRaises(ValueError):
                self.preview(existing)
        self.assertEqual(prepare.call_count, 1)
        self.assertEqual(self.manager.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 7)

    def test_wrong_child_identity_leaves_exact_intent_unresolved(self):
        child = SimpleNamespace(controller=object(), store=self.manager.store, engine=self.manager.engine,
                                settings=self.manager.settings, parameter_web_goal_execution=None,
                                owned_parameter_project_execution=None, remote_form_owned_fixture=None)
        service = self.service(Mock(return_value=child))
        preview = self.preview(service)
        with self.assertRaisesRegex(ValueError, 'child_identity_changed'):
            self.start(service, preview)
        self.assertEqual(service.status()['status'], 'uncertain_before_run_binding')
        self.assertTrue(WebGoalExecutionJournal(self.journal.directory).reserved)
        self.assertEqual(len(list(self.journal.directory.iterdir())), 1)

    def test_returning_original_parent_cannot_create_recursive_child_or_reopen_intent(self):
        service = self.service()
        service.prepare_manager = Mock(return_value=self.manager)
        preview = self.preview(service)
        before_parent = self.parent_configuration()
        with self.assertRaisesRegex(ValueError, 'child_identity_changed'):
            self.start(service, preview)
        self.assertIsNone(service.child_manager)
        self.assertTrue(service.reserved)
        self.assertFalse(self.manager.busy)
        self.assertEqual(service.status()['status'], 'uncertain_before_run_binding')
        self.assertEqual(self.parent_configuration(), before_parent)
        self.assertIs(self.manager.remote_form_owned_fixture, self.original_fixture)

    def test_missing_selected_head_denies_start_before_durable_intent_or_child(self):
        prepare = Mock(side_effect=AssertionError('Changed selection must not reach child creation'))
        service = self.service(prepare)
        preview = self.preview(service)
        selection_file = self.releases.store.directory / (self.selection_sha + '.selection.json')
        selection_file.unlink()
        with self.assertRaisesRegex(ValueError, 'anchored_inventory_changed'):
            self.start(service, preview)
        prepare.assert_not_called()
        self.assertFalse(self.journal.directory.exists())

    def test_pending_fill_review_revocation_prevents_post_and_retains_uncertain_run(self):
        service = self.service()
        preview = self.preview(service)
        before = self.original_rows()
        started, child, _approvals, revoked = asyncio.run(self.execute(service, preview, revoke_pending_fill=True))
        self.assertTrue(revoked)
        row = child.store.connection.execute('SELECT status FROM desktop_tasks WHERE job_id=?',
                                             (started['job_id'],)).fetchone()
        self.assertIn(row['status'], ('failed', 'cancelled'))
        self.assertEqual(sum(event == ('tool', 'browser.form.fill') for event in self.events), 0)
        self.assertEqual(sum(event == ('tool', 'browser.form.submit') for event in self.events), 0)
        self.assertEqual(child.store.connection.execute(
            "SELECT count(*) FROM desktop_approvals WHERE job_id=? AND status IN ('pending','approved')",
            (started['job_id'],)).fetchone()[0], 0)
        self.assertEqual(child.store.connection.execute(
            "SELECT count(*) FROM desktop_approvals WHERE job_id=? AND status='revoked'",
            (started['job_id'],)).fetchone()[0], 1)
        self.assertIsNone(child.remote_form_owned_fixture._state.committed_record)
        self.assertTrue(service.reserved)
        self.assertEqual(self.original_rows(), before)
        self.assertEqual(service.status()['status'], 'awaiting_independent_verification')


if __name__ == '__main__':
    unittest.main()
