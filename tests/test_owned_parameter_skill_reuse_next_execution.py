"""Separately confirmed sequential manual reuse through the actual CPU TLS host."""

import asyncio
import json
import shutil
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aos.contracts import REPO_ROOT, digest, identifier
from aos.web_application_binding import WebRuntimePin
from aos.web_goal_execution_journal import WebGoalExecutionJournal

import test_owned_parameter_skill_reuse_execution as execution_fixture
from test_web_goal_desktop_execution import TLSFixtureBrowserBackend


@unittest.skipUnless(shutil.which('openssl'), 'Requires local synthetic TLS preparation')
class OwnedParameterSkillReuseNextExecutionTests(unittest.TestCase):
    setUp = execution_fixture.OwnedParameterSkillReuseExecutionTests.setUp
    service = execution_fixture.OwnedParameterSkillReuseExecutionTests.service
    preview = execution_fixture.OwnedParameterSkillReuseExecutionTests.preview
    start = execution_fixture.OwnedParameterSkillReuseExecutionTests.start
    original_rows = execution_fixture.OwnedParameterSkillReuseExecutionTests.original_rows
    parent_configuration = execution_fixture.OwnedParameterSkillReuseExecutionTests.parent_configuration

    def prepare_child(self, bundle):
        intents = [json.loads(path.read_text()) for path in self.journal.directory.glob('*.intent.json')]
        matching = [intent for intent in intents if intent['binding']['source']['reuse_admission_sha256']
                    == bundle['admission_sha256']]
        self.assertEqual(len(matching), 1)
        self.assertTrue(WebGoalExecutionJournal(self.journal.directory).reserved)
        self.events.append(('durable_intent_before_child', digest(matching[0])))
        child = self.manager._prepare_parameter_skill_reuse_manager(bundle)
        self.addCleanup(child.remote_form_owned_fixture.close)
        self.addCleanup(lambda: asyncio.run(child.close()))
        self.children.append(child)
        self.bundles.append(bundle)
        return child

    def predecessor(self, service):
        status = service.status()
        self.assertEqual(status['status'], 'accepted_verified')
        return {'previous_intent_sha256': status['intent_sha256'],
                'previous_receipt_sha256': status['receipt_sha256']}

    def next_preview(self, service, predecessor, **changes):
        arguments = {'lease_id': self.control['lease_id'], 'generation': self.control['generation'],
                     **predecessor, **changes}
        return service.next_preview(self.release_sha, self.selection_sha,
                                    {'record-id': 'Synthetic third record', 'note-text': 'Third approved note'},
                                    **arguments)

    async def next_start(self, service, preview, predecessor, **changes):
        arguments = {'confirm_sha256': preview['confirm_sha256'], 'human_confirmation': True,
                     'lease_id': self.control['lease_id'], 'generation': self.control['generation'],
                     **predecessor, **changes}
        return await service.next_start(preview['admission'], **arguments)

    async def execute(self, service, preview, predecessor=None, *, pending_effect=None, success=True):
        def backend(*_arguments, **_keywords):
            runtime = json.loads((REPO_ROOT / 'examples/web_application_binding.json').read_text())['runtime']
            pin = WebRuntimePin.model_validate(runtime | {
                'runtime_id': identifier('browser'),
                'parent_runtime_id': self.manager.controller.runtime.runtime_id})
            return TLSFixtureBrowserBackend(pin, self.bundles[-1], self.events)

        approvals = []
        with patch('aos.desktop_form_mcp.DesktopHTTPSFormMCPRuntime', side_effect=backend):
            started = (self.start(service, preview) if predecessor is None
                       else await self.next_start(service, preview, predecessor))
            child = service.child_manager
            deadline = asyncio.get_running_loop().time() + 15
            while not child.task.done():
                if asyncio.get_running_loop().time() >= deadline:
                    child.task.cancel()
                    await asyncio.gather(child.task, return_exceptions=True)
                    self.fail('Synthetic sequential task exceeded its fifteen-second bound')
                pending = child.store.connection.execute(
                    "SELECT * FROM desktop_approvals WHERE job_id=? AND status='pending'",
                    (started['job_id'],)).fetchone()
                if pending is not None:
                    if pending_effect is not None:
                        pending_effect()
                    approvals.append(pending['approval_id'])
                    try:
                        self.manager.respond(pending['approval_id'], pending['action_sha256'], True)
                    except Exception:
                        if success:
                            raise
                await asyncio.sleep(.001)
            await child.task
        if success:
            self.assertEqual(service.status()['status'], 'accepted_verified')
            self.assertEqual(len(approvals), 6)
            self.assertEqual(len(set(approvals)), 6)
            self.assertEqual(service.execution.receipt['readback']['observed_record_sha256'],
                             preview['admission']['expected_record_sha256'])
        return started, child, approvals

    def files(self, directory):
        return {str(path.relative_to(directory)): path.read_bytes()
                for path in directory.rglob('*') if path.is_file()}

    def test_two_new_tasks_preserve_original_and_predecessor_with_fresh_effects(self):
        service = self.service()
        original_rows = self.original_rows()
        original_files = self.files(self.source.directory)
        parent = self.parent_configuration()

        async def scenario():
            first, first_child, first_approvals = await self.execute(service, self.preview(service))
            predecessor = self.predecessor(service)
            previous_files = self.files(self.journal.directory)
            previous_receipt = service.execution.receipt
            preview = self.next_preview(service, predecessor)
            self.assertEqual(preview['transition_sha256'], digest(preview['transition']))
            self.assertEqual(preview['binding']['proposal_sha256'], preview['transition_sha256'])
            self.assertEqual(preview['binding']['source']['reuse_admission_sha256'],
                             digest(preview['admission']))
            self.assertEqual(preview['transition']['previous_receipt_sha256'], digest(previous_receipt))
            second, second_child, second_approvals = await self.execute(service, preview, predecessor)
            self.assertIsNot(first_child, second_child)
            self.assertTrue(first_child.closed)
            self.assertNotEqual(first['job_id'], second['job_id'])
            self.assertNotEqual(first['intent_sha256'], second['intent_sha256'])
            current_receipt = service.execution.receipt
            self.assertEqual(sum(receipt['recipe_audit']['submit_count'] for receipt in
                                 (self.source.receipt, previous_receipt, current_receipt)), 3)
            for receipt in (previous_receipt, current_receipt):
                for flag in ('site_outcome_verified', 'activation_authorized', 'training_ready',
                             'gpu_release_verified'):
                    self.assertIs(receipt[flag], False)
            for field in ('run_id', 'runtime_id'):
                self.assertNotEqual(previous_receipt['run_identity'][field],
                                    current_receipt['run_identity'][field])
            self.assertTrue(set(first_approvals).isdisjoint(second_approvals))
            for name, content in previous_files.items():
                self.assertEqual((self.journal.directory / name).read_bytes(), content)
            self.assertEqual(service.read(first['intent_sha256'])['receipt_sha256'],
                             predecessor['previous_receipt_sha256'])
            self.assertEqual(first_child.remote_form_owned_fixture._state.committed_record,
                             {'contact_name': self.parameters['record-id'], 'note': self.parameters['note-text']})
            self.assertEqual(second_child.remote_form_owned_fixture._state.committed_record,
                             {'contact_name': 'Synthetic third record', 'note': 'Third approved note'})
            for child in (first_child, second_child):
                self.assertTrue(child.remote_form_owned_fixture._closed)
                self.assertTrue(child.remote_form_owned_fixture._readback_used)
            with self.assertRaises(ValueError):
                await self.next_start(service, preview, predecessor)
            self.assertEqual(len(self.children), 2)

        asyncio.run(scenario())
        self.assertEqual(self.original_rows(), original_rows)
        self.assertEqual(self.files(self.source.directory), original_files)
        self.assertEqual(self.parent_configuration(), parent)
        self.assertEqual(len(list(self.journal.directory.glob('*.intent.json'))), 2)
        self.assertEqual(len(list(self.journal.directory.glob('*.accepted.json'))), 2)
        self.assertEqual(sum(event == ('tool', 'browser.form.submit') for event in self.events), 2)
        connection = self.manager.store.connection
        self.assertEqual(connection.execute('SELECT count(*) FROM actions').fetchone()[0], 21)
        self.assertEqual(connection.execute("SELECT count(*) FROM desktop_approvals WHERE status='consumed'").fetchone()[0], 18)
        self.assertEqual(connection.execute('SELECT count(*) FROM model_calls').fetchone()[0], 0)
        self.assertFalse(service.reserved)

    def test_stale_predecessor_confirmation_control_and_same_parameters_do_not_cleanup(self):
        service = self.service()

        async def scenario():
            await self.execute(service, self.preview(service))
            predecessor = self.predecessor(service)
            preview = self.next_preview(service, predecessor)
            child = service.child_manager
            with patch.object(child, 'close', new_callable=AsyncMock) as close:
                for changes in ({'previous_intent_sha256': 'a' * 64},
                                {'previous_receipt_sha256': 'a' * 64},
                                {'confirm_sha256': 'a' * 64}, {'human_confirmation': False},
                                {'human_confirmation': 1}, {'lease_id': 'different-lease'},
                                {'generation': self.control['generation'] + 1}, {'generation': True}):
                    with self.subTest(changes=changes), self.assertRaises(ValueError):
                        await self.next_start(service, preview, predecessor, **changes)
                with self.assertRaises(ValueError):
                    service.next_preview(self.release_sha, self.selection_sha, self.parameters,
                                         **predecessor, lease_id=self.control['lease_id'],
                                         generation=self.control['generation'])
                close.assert_not_awaited()
            self.assertFalse(child.closed)
            self.assertEqual(len(self.children), 1)
            self.assertEqual(len(list(self.journal.directory.glob('*.intent.json'))), 1)

        asyncio.run(scenario())

    def test_uncertain_previous_intent_never_admits_next_or_calls_cleanup(self):
        prepare = Mock(side_effect=ValueError('synthetic preparation failure'))
        service = self.service(prepare)
        preview = self.preview(service)
        with self.assertRaisesRegex(ValueError, 'synthetic preparation failure'):
            self.start(service, preview)
        predecessor = {'previous_intent_sha256': service.intent_sha256, 'previous_receipt_sha256': 'a' * 64}
        with self.assertRaises(ValueError):
            self.next_preview(service, predecessor)
        with self.assertRaises(ValueError):
            asyncio.run(self.next_start(service, preview, predecessor))
        self.assertTrue(service.reserved)
        self.assertEqual(service.status()['status'], 'uncertain_before_run_binding')
        self.assertEqual(prepare.call_count, 1)

    def test_revocation_after_preview_keeps_old_receipt_without_next_intent(self):
        service = self.service()

        async def scenario():
            await self.execute(service, self.preview(service))
            predecessor = self.predecessor(service)
            preview = self.next_preview(service, predecessor)
            before = self.files(self.journal.directory)
            _record, checksum = self.reviewed.reviews.revocation_preview(self.reviewed.review_sha256)
            self.reviewed.reviews.revoke(self.reviewed.review_sha256, checksum, 'REVOKE_MANUAL_REVIEW')
            with patch.object(service.child_manager, 'close', new_callable=AsyncMock) as close:
                with self.assertRaises(ValueError):
                    await self.next_start(service, preview, predecessor)
                close.assert_not_awaited()
            self.assertEqual(self.files(self.journal.directory), before)
            self.assertEqual(service.read(predecessor['previous_intent_sha256'])['status'], 'accepted_verified')

        asyncio.run(scenario())

    def test_failed_cleanup_latches_transition_without_consuming_next_confirmation(self):
        service = self.service()

        async def scenario():
            await self.execute(service, self.preview(service))
            predecessor = self.predecessor(service)
            preview = self.next_preview(service, predecessor)
            before = self.files(self.journal.directory)
            with patch.object(service.child_manager, 'close', new_callable=AsyncMock,
                              side_effect=ValueError('synthetic cleanup failure')) as close:
                with self.assertRaisesRegex(ValueError, 'synthetic cleanup failure'):
                    await self.next_start(service, preview, predecessor)
                self.assertEqual(close.await_count, 1)
            self.assertTrue(service.transition_blocked)
            self.assertFalse(service.transitioning)
            self.assertEqual(self.files(self.journal.directory), before)
            self.assertEqual(len(self.children), 1)
            with self.assertRaises(ValueError):
                self.next_preview(service, predecessor)
            with self.assertRaises(ValueError):
                await self.next_start(service, preview, predecessor)
            self.assertEqual(service.read(predecessor['previous_intent_sha256'])['status'], 'accepted_verified')

        asyncio.run(scenario())

    def test_cleanup_timeout_preserves_one_handle_and_latches_after_late_completion(self):
        service = self.service()
        service.cleanup_timeout_seconds = .01

        async def scenario():
            await self.execute(service, self.preview(service))
            predecessor = self.predecessor(service)
            preview = self.next_preview(service, predecessor)
            before = self.files(self.journal.directory)
            child = service.child_manager
            close_child = child.close
            release = asyncio.Event()

            async def delayed_close():
                await release.wait()
                await close_child()

            with patch.object(child, 'close', side_effect=delayed_close) as close:
                with self.assertRaises(TimeoutError):
                    await self.next_start(service, preview, predecessor)
                handle = service.cleanup_task
                self.assertFalse(handle.done())
                self.assertFalse(handle.cancelled())
                self.assertTrue(service.transition_blocked)
                self.assertTrue(service.reserved)
                self.assertFalse(service.transitioning)
                self.assertEqual(self.files(self.journal.directory), before)
                self.assertEqual(len(self.children), 1)
                with self.assertRaises(ValueError):
                    self.next_preview(service, predecessor)
                with self.assertRaises(ValueError):
                    await self.next_start(service, preview, predecessor)
                self.assertEqual(service.read(predecessor['previous_intent_sha256'])['status'],
                                 'accepted_verified')
                with self.assertRaises(TimeoutError):
                    await self.manager.close()
                self.assertEqual(close.await_count, 1)
                self.assertFalse(handle.cancelled())
                release.set()
                await handle
                await self.manager.close()
                self.assertEqual(close.await_count, 1)
                self.assertIs(service.cleanup_task, handle)
                self.assertTrue(service.transition_blocked)
                self.assertEqual(self.files(self.journal.directory), before)

        asyncio.run(scenario())

    def test_control_change_during_cleanup_latches_without_new_intent_or_child(self):
        service = self.service()

        async def scenario():
            await self.execute(service, self.preview(service))
            predecessor = self.predecessor(service)
            preview = self.next_preview(service, predecessor)
            before = self.files(self.journal.directory)
            child = service.child_manager
            close = child.close

            async def changed_control():
                self.assertTrue(service.transitioning)
                self.assertTrue(service.reserved)
                await close()
                self.manager.controller.control('take-control')

            with patch.object(child, 'close', side_effect=changed_control):
                with self.assertRaises(ValueError):
                    await self.next_start(service, preview, predecessor)
            self.assertTrue(child.closed)
            self.assertTrue(service.transition_blocked)
            self.assertFalse(service.transitioning)
            self.assertEqual(self.files(self.journal.directory), before)
            self.assertEqual(len(self.children), 1)
            self.assertEqual(service.read(predecessor['previous_intent_sha256'])['status'], 'accepted_verified')
            self.assertEqual(self.manager.controller.state()['owner'], 'HUMAN')

        asyncio.run(scenario())

    def test_missing_previous_receipt_denies_before_cleanup_or_new_intent(self):
        service = self.service()

        async def scenario():
            await self.execute(service, self.preview(service))
            predecessor = self.predecessor(service)
            preview = self.next_preview(service, predecessor)
            accepted = self.journal.directory / (predecessor['previous_intent_sha256'] + '.accepted.json')
            accepted.unlink()
            before = self.files(self.journal.directory)
            with patch.object(service.child_manager, 'close', new_callable=AsyncMock) as close:
                with self.assertRaises(ValueError):
                    self.next_preview(service, predecessor)
                with self.assertRaises(ValueError):
                    await self.next_start(service, preview, predecessor)
                close.assert_not_awaited()
            self.assertEqual(self.files(self.journal.directory), before)
            self.assertEqual(len(self.children), 1)
            self.assertTrue(service.reserved)
            self.assertNotEqual(service.read(predecessor['previous_intent_sha256'])['status'], 'accepted_verified')

        asyncio.run(scenario())

    def test_missing_predecessor_during_next_pending_fill_stops_before_fill_or_post(self):
        service = self.service()

        async def scenario():
            await self.execute(service, self.preview(service))
            predecessor = self.predecessor(service)
            preview = self.next_preview(service, predecessor)
            accepted = self.journal.directory / (predecessor['previous_intent_sha256'] + '.accepted.json')
            event_offset = len(self.events)
            removed = False

            def remove_pending_fill_proof():
                nonlocal removed
                if not removed and ('tool', 'browser.form.state_before') in self.events[event_offset:]:
                    accepted.unlink()
                    removed = True

            started, child, _approvals = await self.execute(
                service, preview, predecessor, pending_effect=remove_pending_fill_proof, success=False)
            self.assertTrue(removed)
            self.assertNotIn(('tool', 'browser.form.fill'), self.events[event_offset:])
            self.assertNotIn(('tool', 'browser.form.submit'), self.events[event_offset:])
            self.assertEqual(child.store.connection.execute(
                "SELECT count(*) FROM desktop_approvals WHERE job_id=? AND status IN ('pending','approved')",
                (started['job_id'],)).fetchone()[0], 0)
            self.assertIsNone(service.execution.receipt)
            self.assertTrue(service.reserved)
            self.assertEqual(len(list(self.journal.directory.glob('*.intent.json'))), 2)
            with self.assertRaises(ValueError):
                self.next_preview(service, predecessor)

        asyncio.run(scenario())
