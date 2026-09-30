import asyncio
from dataclasses import FrozenInstanceError, replace
import json
import os
import unittest
from unittest.mock import patch

import httpx

from aos.contracts import AOSFault, HELLO_CONTENT, REPO_ROOT
from aos.browser_operator import BrowserOperator
from aos.decision import FixtureDecisionEngine
from aos.desktop_console import create_console
from aos.operator import Operator
from aos.task_sequence import SequencePlan, SequenceStart
from aos.vision import FixtureVisionSupervisor
from aos.vision_operator import VisionOperator
import test_desktop_tasks as fixtures


class GatedFixtureEngine(FixtureDecisionEngine):
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def decide(self, state, options):
        self.started.set()
        await self.release.wait()
        return await super().decide(state, options)


class TaskAutoApprovalTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.DesktopTaskTests.asyncSetUp
    asyncTearDown = fixtures.DesktopTaskTests.asyncTearDown
    approval = fixtures.DesktopTaskTests.approval
    settled = fixtures.DesktopTaskTests.settled

    def start(self, kind='hello', approve_all=True):
        state = self.controller.state()
        return self.scheduler.start(state['lease_id'], state['generation'], kind, approve_all=approve_all)

    def grants(self):
        return [json.loads(row[0]) for row in self.store.connection.execute(
            "SELECT payload_json FROM desktop_events WHERE kind='task_auto_approval' ORDER BY rowid")]

    async def gated(self):
        engine = GatedFixtureEngine()
        self.scheduler.engine = engine
        self.start()
        await engine.started.wait()
        return engine

    async def test_hello_delegation_preserves_exact_approval_and_independent_verification(self):
        admission = self.start()
        grant = self.scheduler._approval_grant
        self.assertEqual(self.scheduler.status()['auto_approval'], {'job_id': admission['job_id'], 'kind': 'hello'})
        with self.assertRaises(FrozenInstanceError):
            grant.kind = 'browser_form'
        self.assertEqual((await self.settled())['status'], 'succeeded')
        self.assertEqual((self.runtime.root / 'hello.txt').read_text(), HELLO_CONTENT)
        approvals = self.store.connection.execute('SELECT * FROM desktop_approvals').fetchall()
        self.assertEqual([row['status'] for row in approvals], ['consumed'])
        intervention = self.store.connection.execute('SELECT * FROM human_interventions').fetchone()
        self.assertEqual(intervention['actor'], 'task_scoped_auto_approval')
        self.assertEqual(intervention['kind'], 'approve')
        self.assertEqual(json.loads(intervention['payload_json']), {
            'approval_id': approvals[0]['approval_id'], 'action_sha256': approvals[0]['action_sha256'],
            'grant_id': grant.grant_id, 'job_id': admission['job_id']})
        self.assertEqual([event['status'] for event in self.grants()], ['granted', 'bound', 'revoked'])
        self.assertEqual(self.grants()[1]['run_id'], json.loads(approvals[0]['envelope_json'])['action']['run_id'])
        self.assertIsNone(self.scheduler.status()['auto_approval'])
        self.assertEqual(self.store.connection.execute('SELECT result FROM verifications').fetchone()[0], 'passed')
        self.assertEqual(self.store.connection.execute('PRAGMA foreign_key_check').fetchall(), [])

    async def test_existing_exact_hello_is_read_only_and_next_task_is_manual(self):
        (self.runtime.root / 'hello.txt').write_text(HELLO_CONTENT)
        self.start()
        self.assertEqual((await self.settled())['status'], 'succeeded')
        self.assertEqual({row[0] for row in self.store.connection.execute('SELECT tool FROM actions')}, {'filesystem.read'})
        self.start(approve_all=False)
        approval = await self.approval()
        self.assertIsNone(self.scheduler.status()['auto_approval'])
        self.scheduler.respond(approval['approval_id'], approval['action_sha256'], False)
        self.assertEqual((await self.settled())['status'], 'cancelled')
        self.assertEqual(len(self.grants()), 3)

    async def test_pause_clears_grant_and_resume_requires_new_manual_action(self):
        await self.gated()
        self.controller.control('pause')
        await self.scheduler.pause()
        self.assertTrue(self.scheduler.paused)
        self.assertIsNone(self.scheduler._approval_grant)
        self.scheduler.engine = FixtureDecisionEngine()
        state = self.controller.control('resume')
        self.scheduler.resume(state['lease_id'], state['generation'])
        approval = await self.approval()
        self.assertIsNone(self.scheduler.status()['auto_approval'])
        self.assertFalse((self.runtime.root / 'hello.txt').exists())
        self.scheduler.respond(approval['approval_id'], approval['action_sha256'], False)
        await self.settled()

    async def test_prestart_cancel_and_shutdown_revoke_without_execution(self):
        self.start()
        await self.scheduler.cancel('take_control')
        self.assertIsNone(self.scheduler._approval_grant)
        self.assertFalse((self.runtime.root / 'hello.txt').exists())
        self.assertEqual(self.grants()[-1]['reason'], 'take_control')
        self.start()
        await self.scheduler.close()
        self.assertIsNone(self.scheduler._approval_grant)
        self.assertEqual(self.grants()[-1]['reason'], 'stop')

    async def test_expiry_is_read_only_in_status_and_fails_closed(self):
        for field in ('expires_at', 'monotonic_deadline'):
            with self.subTest(field=field):
                engine = await self.gated()
                self.scheduler._approval_grant = replace(self.scheduler._approval_grant, **{field: 0.0})
                before = self.store.connection.total_changes
                self.assertIsNone(self.scheduler.status()['auto_approval'])
                self.assertEqual(self.store.connection.total_changes, before)
                engine.release.set()
                self.assertEqual((await self.settled())['status'], 'failed')
                self.assertIsNone(self.scheduler._approval_grant)
        self.assertFalse((self.runtime.root / 'hello.txt').exists())
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)

    async def test_changed_grant_bindings_cannot_approve(self):
        for field, value in (('job_id', 'other'), ('session_id', 'other'), ('run_id', 'other'),
                             ('task_id', 'other'), ('runtime_id', 'other'), ('parent_runtime_id', 'other'),
                             ('lease_id', 'other'), ('generation', 999), ('kind', 'browser_form')):
            with self.subTest(field=field):
                engine = await self.gated()
                self.scheduler._approval_grant = replace(self.scheduler._approval_grant, **{field: value})
                engine.release.set()
                self.assertEqual((await self.settled())['status'], 'failed')
        self.assertFalse((self.runtime.root / 'hello.txt').exists())
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)

    async def test_unexpected_hello_arguments_tool_option_and_exhaustion_rejected(self):
        original = Operator.action
        for changes in ({'tool': 'process.sha256'}, {'selected_option': 'ask_human'},
                        {'arguments': {'path': '/workspace/other.txt', 'content': HELLO_CONTENT}},
                        {'arguments': {'path': '/workspace/hello.txt', 'content': 'different'}},
                        {'verification': 'unverified'}):
            with self.subTest(changes=changes):
                def changed(operator, state, tool, option):
                    return original(operator, state, tool, option).model_copy(update=changes)
                with patch.object(Operator, 'action', changed):
                    self.start()
                    self.assertEqual((await self.settled())['status'], 'failed')
        engine = await self.gated()
        self.scheduler._grant_uses = 1
        engine.release.set()
        self.assertEqual((await self.settled())['status'], 'failed')
        self.assertFalse((self.runtime.root / 'hello.txt').exists())

    async def test_revocation_between_response_and_consume_blocks_action(self):
        original = self.scheduler._respond
        def revoke(*arguments):
            result = original(*arguments)
            self.scheduler.clear_grant('synthetic_race')
            return result
        with patch.object(self.scheduler, '_respond', revoke):
            self.start()
            self.assertEqual((await self.settled())['status'], 'failed')
        self.assertFalse((self.runtime.root / 'hello.txt').exists())
        self.assertEqual(self.store.connection.execute('SELECT status FROM desktop_approvals').fetchone()[0], 'revoked')

    async def test_model_failure_and_human_escalation_clear_grant(self):
        with patch.object(FixtureDecisionEngine, 'decide', side_effect=ValueError('synthetic failure')):
            self.start()
            self.assertEqual((await self.settled())['status'], 'failed')
        self.assertIsNone(self.scheduler._approval_grant)
        async def abstain(state, options):
            prediction = await FixtureDecisionEngine().decide(state, options)
            return prediction.model_copy(update={'selected_option': 'ask_human',
                                                  'probabilities': {option.id: float(option.id == 'ask_human') for option in options}})
        self.scheduler.engine.decide = abstain
        self.start()
        self.assertEqual((await self.settled())['status'], 'waiting_human')
        self.assertIsNone(self.scheduler._approval_grant)

    async def test_sequence_children_never_inherit_previous_delegation(self):
        self.start()
        await self.settled()
        browser_manifest = self.root / 'synthetic-browser-manifest.json'
        browser_manifest.write_text(json.dumps({
            'synthetic': True, 'purpose': 'sequence_identity_only_no_browser_runtime'}))
        self.scheduler.browser_manifest = browser_manifest
        state = self.controller.state()
        self.scheduler.sequences.start(SequenceStart(plan=SequencePlan(kinds=['hello', 'browser_form']),
                                                    lease_id=state['lease_id'], generation=state['generation']))
        approval = await self.approval()
        self.assertIsNone(self.scheduler.status()['auto_approval'])
        with self.assertRaises(AOSFault):
            self.start()
        self.scheduler.respond(approval['approval_id'], approval['action_sha256'], False)
        await self.scheduler.sequences.task
        self.assertEqual(len(self.grants()), 3)

    async def test_api_strict_boolean_and_no_retroactive_grant(self):
        origin = 'http://testserver'
        app = create_console(self.controller, 'synthetic-token', origin, self.root, scheduler=self.scheduler)
        state = self.controller.state()
        payload = {'kind': 'hello', 'lease_id': state['lease_id'], 'generation': state['generation']}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin,
                                    headers={'Origin': origin}) as client:
            self.assertEqual((await client.post('/api/tasks', json={**payload, 'approve_all': True})).status_code, 401)
            await client.post('/api/login', json={'token': 'synthetic-token'})
            self.assertTrue((await client.get('/api/tasks')).json()['supports_approve_all'])
            for invalid in ('true', 1, 0, None, {}, []):
                self.assertEqual((await client.post('/api/tasks', json={**payload, 'approve_all': invalid})).status_code, 400)
            self.assertEqual((await client.post('/api/tasks', json=payload)).status_code, 200)
            approval = await self.approval()
            self.assertEqual((await client.post('/api/tasks', json={**payload, 'approve_all': True})).status_code, 409)
            self.assertIsNone(self.scheduler._approval_grant)
            self.scheduler.respond(approval['approval_id'], approval['action_sha256'], False)
            await self.settled()
            self.assertEqual((await client.post('/api/tasks', json={**payload, 'approve_all': True})).status_code, 200)
            self.assertEqual((await self.settled())['status'], 'succeeded')

    async def test_direct_non_boolean_delegation_rejected(self):
        for invalid in ('true', 1, None):
            with self.assertRaises(AOSFault):
                self.start(approve_all=invalid)
        self.assertEqual(self.grants(), [])

    async def test_approval_audit_failure_never_executes(self):
        original = self.store.insert
        def fail_audit(table, **values):
            if table == 'human_interventions':
                raise RuntimeError('Synthetic approval audit failure')
            return original(table, **values)
        with patch.object(self.store, 'insert', fail_audit):
            self.start()
            self.assertEqual((await self.settled())['status'], 'failed')
        self.assertIsNone(self.scheduler._approval_grant)
        self.assertFalse((self.runtime.root / 'hello.txt').exists())
        self.assertEqual(self.store.connection.execute('SELECT status FROM desktop_approvals').fetchone()[0], 'revoked')

    async def test_revocation_audit_failure_still_cancels_worker(self):
        await self.gated()
        original = self.scheduler.grant_event
        def fail_revoke(grant, status, reason):
            if status == 'revoked':
                raise RuntimeError('Synthetic revoke audit failure')
            return original(grant, status, reason)
        with patch.object(self.scheduler, 'grant_event', fail_revoke):
            with self.assertRaises(RuntimeError):
                await self.scheduler.cancel('stop')
        self.assertFalse(self.scheduler.busy)
        self.assertIsNone(self.scheduler._approval_grant)
        self.assertIsNone(self.scheduler.active_runtime)
        self.assertFalse((self.runtime.root / 'hello.txt').exists())

    async def test_admission_audit_failure_does_not_start_job(self):
        with patch.object(self.scheduler, 'grant_event', side_effect=RuntimeError('Synthetic admission audit failure')):
            with self.assertRaises(RuntimeError):
                self.start()
        self.assertFalse(self.scheduler.busy)
        self.assertIsNone(self.scheduler._approval_grant)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM desktop_tasks').fetchone()[0], 0)


@unittest.skipUnless(os.environ.get('AOS_BROWSER_TESTS') == '1', 'Requires pinned isolated Chromium; fixture models only')
class BrowserTaskAutoApprovalTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.DesktopTaskTests.asyncSetUp
    asyncTearDown = fixtures.DesktopTaskTests.asyncTearDown
    start = TaskAutoApprovalTests.start
    settled = TaskAutoApprovalTests.settled

    async def test_real_form_two_fresh_delegated_actions_and_separate_verifications(self):
        self.scheduler.browser_manifest = REPO_ROOT / 'models/browser-manifest.json'
        self.start('browser_form')
        self.assertEqual((await self.settled())['status'], 'succeeded')
        rows = self.store.connection.execute('SELECT * FROM desktop_approvals ORDER BY rowid').fetchall()
        self.assertEqual([row['status'] for row in rows], ['consumed', 'consumed'])
        self.assertNotEqual(rows[0]['action_sha256'], rows[1]['action_sha256'])
        self.assertEqual([json.loads(row['envelope_json'])['action']['tool'] for row in rows], ['browser.fill', 'browser.submit'])
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM verifications WHERE result='passed'").fetchone()[0], 2)
        self.assertIsNone(self.scheduler.status()['auto_approval'])

    async def test_real_canvas_one_capture_bound_save_and_separate_verification(self):
        self.scheduler.browser_manifest = REPO_ROOT / 'models/browser-manifest.json'
        self.scheduler.vision_supervisor = FixtureVisionSupervisor()
        self.start('vision_canvas')
        self.assertEqual((await self.settled())['status'], 'succeeded')
        self.assertEqual(self.store.connection.execute('SELECT status FROM desktop_approvals').fetchone()[0], 'consumed')
        self.assertEqual(self.store.connection.execute('SELECT result FROM verifications').fetchone()[0], 'passed')
        self.assertIsNone(self.scheduler.status()['auto_approval'])

    async def test_form_repeated_fill_cannot_consume_second_delegated_approval(self):
        self.scheduler.browser_manifest = REPO_ROOT / 'models/browser-manifest.json'
        original = BrowserOperator.browser_action
        def duplicate(operator, state, tool, option, arguments):
            if tool == 'browser.submit':
                return original(operator, state, 'browser.fill', 'fill_message', {**arguments, 'value': 'Hello from the local agent.'})
            return original(operator, state, tool, option, arguments)
        with patch.object(BrowserOperator, 'browser_action', duplicate):
            self.start('browser_form')
            self.assertEqual((await self.settled())['status'], 'failed')
        self.assertEqual([row[0] for row in self.store.connection.execute('SELECT status FROM desktop_approvals ORDER BY rowid')],
                         ['consumed', 'revoked'])
        self.assertEqual([row[0] for row in self.store.connection.execute("SELECT tool FROM actions WHERE tool!='browser.verify'")],
                         ['browser.fill'])

    async def test_form_wrong_current_symbol_is_not_automatically_approved(self):
        self.scheduler.browser_manifest = REPO_ROOT / 'models/browser-manifest.json'
        original = BrowserOperator.browser_action
        def wrong_target(operator, state, tool, option, arguments):
            return original(operator, state, tool, option, {**arguments, 'element_id': '0' * 32})
        with patch.object(BrowserOperator, 'browser_action', wrong_target):
            self.start('browser_form')
            self.assertEqual((await self.settled())['status'], 'failed')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)

    async def test_canvas_cancel_symbol_is_not_automatically_approved(self):
        self.scheduler.browser_manifest = REPO_ROOT / 'models/browser-manifest.json'
        self.scheduler.vision_supervisor = FixtureVisionSupervisor()
        original = VisionOperator.visual_action
        def cancel_target(operator, state, tool, option):
            target = next(key for key, element in operator.runtime.scene.targets().items() if element.label == 'CANCEL')
            return original(operator, state, tool, target)
        with patch.object(VisionOperator, 'visual_action', cancel_target):
            self.start('vision_canvas')
            self.assertEqual((await self.settled())['status'], 'failed')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)
        self.assertIsNone(self.scheduler._approval_grant)
