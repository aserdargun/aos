import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import httpx
import jsonschema
from pydantic import ValidationError

from aos.contracts import AOSFault, REPO_ROOT
from aos.desktop_console import create_console
from aos.desktop_control import DesktopController
from aos.desktop_tasks import DesktopScheduler
from aos.decision import FixtureDecisionEngine
from aos.storage import TrajectoryStore
from aos.task_sequence import SequencePlan, SequenceStart, SequenceStatus
from aos.vision import FixtureVisionSupervisor
import test_desktop_tasks as fixtures


class TaskSequenceTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixtures.DesktopTaskTests.asyncTearDown

    async def asyncSetUp(self):
        await fixtures.DesktopTaskTests.asyncSetUp(self)
        self.browser_manifest = self.root / 'synthetic-browser-manifest.json'
        self.browser_manifest.write_text(json.dumps({
            'synthetic': True, 'purpose': 'sequence_identity_only_no_browser_runtime'}))

    def request(self, kinds=None):
        state = self.controller.state()
        return SequenceStart(plan=SequencePlan(kinds=kinds or ['hello', 'browser_form']),
                             lease_id=state['lease_id'], generation=state['generation'])

    def start(self, kinds=None, *, browser_manifest=None):
        self.scheduler.browser_manifest = browser_manifest or self.browser_manifest
        self.scheduler.vision_supervisor = FixtureVisionSupervisor()
        return self.scheduler.sequences.start(self.request(kinds))

    async def approval(self, previous=None):
        async with asyncio.timeout(30):
            while True:
                pending = self.scheduler.status()['approval']
                if pending and pending['approval_id'] != previous:
                    return pending
                if not self.scheduler.sequences.reserved and not self.scheduler.busy:
                    self.fail(str(self.scheduler.sequences.status()))
                await asyncio.sleep(.01)

    async def settled(self):
        await asyncio.gather(self.scheduler.sequences.task, return_exceptions=True)
        return self.scheduler.sequences.report

    def test_schema_and_fixture_reject_scope_and_repeated_steps(self):
        for kinds in ([], ['hello'], ['hello', 'hello'], ['hello', 'shell'], ['hello'] * 4):
            with self.assertRaises(ValidationError):
                SequencePlan(kinds=kinds)
        for field in ('scope', 'shell', 'goal', 'execution_authorized'):
            with self.assertRaises(ValidationError):
                SequencePlan.model_validate({'kinds': ['hello', 'browser_form'], field: 'arbitrary'})
        fixture = json.loads((REPO_ROOT / 'examples/task_sequence.json').read_text())
        self.assertTrue(fixture['synthetic'])
        for name, model, value in [('sequence_start', SequenceStart, fixture['request']),
                                   ('sequence_status', SequenceStatus, fixture['status'])]:
            schema = json.loads((REPO_ROOT / f'schemas/{name}.schema.json').read_text())
            self.assertEqual(schema, {'$schema': 'https://json-schema.org/draft/2020-12/schema', **model.model_json_schema()})
            jsonschema.validate(value, schema)
            model.model_validate(value)
        for change in ({'completed': 2}, {'plan_sha256': '0' * 64}, {'automatic_replay_allowed': True}):
            with self.assertRaises(ValidationError):
                SequenceStatus.model_validate({**fixture['status'], **change})

    async def test_admission_is_atomic_and_rejects_unavailable_or_stale(self):
        before = self.store.connection.total_changes
        with self.assertRaises(AOSFault):
            self.scheduler.sequences.start(self.request())
        self.assertEqual(self.store.connection.total_changes, before)
        request = self.request()
        self.scheduler.browser_manifest = self.browser_manifest
        self.controller.control('pause')
        with self.assertRaises(AOSFault):
            self.scheduler.sequences.start(request)
        self.assertEqual(self.scheduler.status()['jobs'], [])
        self.controller.control('resume')
        self.start()
        self.assertTrue(self.scheduler.reserved)
        with self.assertRaises(AOSFault):
            self.start()
        state = self.controller.state()
        with self.assertRaises(AOSFault):
            self.scheduler.start(state['lease_id'], state['generation'])
        await self.scheduler.cancel()
        self.assertEqual(self.scheduler.status()['jobs'], [])

    async def test_rejection_stops_remainder_without_effect(self):
        self.start()
        approval = await self.approval()
        self.scheduler.respond(approval['approval_id'], approval['action_sha256'], False)
        report = await self.settled()
        self.assertEqual((report.status, report.completed, len(report.job_ids)), ('failed', 0, 1))
        self.assertFalse((self.runtime.root / 'hello.txt').exists())
        self.assertEqual(len(self.scheduler.status()['jobs']), 1)

    async def test_expiry_stops_remainder_without_effect(self):
        self.scheduler.approval_seconds = .03
        self.start()
        report = await self.settled()
        self.assertEqual(report.status, 'failed')
        self.assertEqual(len(report.job_ids), 1)
        self.assertFalse((self.runtime.root / 'hello.txt').exists())

    async def test_takeover_cancels_sequence_and_revokes_pending(self):
        self.start()
        approval = await self.approval()
        self.controller.control('take-control')
        await self.scheduler.cancel('take_control')
        self.assertEqual(self.scheduler.sequences.report.status, 'cancelled')
        self.assertFalse(self.scheduler.reserved)
        self.assertEqual(len(self.scheduler.status()['jobs']), 1)
        with self.assertRaises(AOSFault):
            self.scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
        self.assertFalse((self.runtime.root / 'hello.txt').exists())

    async def test_pause_cancels_remainder_but_preserves_current_single_job(self):
        self.start()
        first = await self.approval()
        self.controller.control('pause')
        await self.scheduler.pause()
        self.assertTrue(self.scheduler.paused)
        self.assertEqual(self.scheduler.sequences.report.status, 'cancelled')
        state = self.controller.control('resume')
        self.scheduler.resume(state['lease_id'], state['generation'])
        second = await self.approval(first['approval_id'])
        self.assertNotEqual(first['action_sha256'], second['action_sha256'])
        self.scheduler.respond(second['approval_id'], second['action_sha256'], True)
        await self.scheduler.task
        self.assertEqual(len(self.scheduler.status()['jobs']), 1)
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'succeeded')
        self.assertEqual(self.scheduler.sequences.report.status, 'cancelled')

    async def test_deployment_change_before_dispatch_denies_all_steps(self):
        self.start()
        self.scheduler.engine.identity = {**self.scheduler.engine.identity, 'deployment_id': 'changed-synthetic'}
        report = await self.settled()
        self.assertEqual(report.status, 'failed')
        self.assertEqual(report.job_ids, [])
        self.assertEqual(self.scheduler.status()['jobs'], [])

    async def test_missing_independent_evidence_prevents_next_job(self):
        self.start()
        approval = await self.approval()
        with patch.object(self.scheduler.sequences, 'verified', return_value=False):
            self.scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
            report = await self.settled()
        self.assertEqual((report.status, report.completed, len(report.job_ids)), ('failed', 0, 1))

    async def test_authenticated_api_denies_plan_expansion_and_input_during_sequence(self):
        self.scheduler.browser_manifest = self.browser_manifest
        origin = 'http://127.0.0.1:8765'
        app = create_console(self.controller, 'synthetic-token', origin, self.root, scheduler=self.scheduler)
        request = self.request().model_dump()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin, headers={'Origin': origin}) as client:
            self.assertEqual((await client.get('/api/sequences')).status_code, 401)
            self.assertEqual((await client.post('/api/sequences', json=request)).status_code, 401)
            await client.post('/api/login', json={'token': 'synthetic-token'})
            self.assertEqual((await client.post('/api/sequences', json=request, headers={'Origin': 'http://evil.invalid'})).status_code, 403)
            for bad in ({}, {**request, 'resume': True}, {**request, 'plan': {'kinds': ['hello', 'shell']}},
                        {**request, 'generation': True}):
                self.assertEqual((await client.post('/api/sequences', json=bad)).status_code, 400)
            self.assertEqual((await client.post('/api/sequences?execute=true', json=request)).status_code, 400)
            self.assertEqual((await client.post('/api/sequences', json=request)).status_code, 200)
            self.assertEqual((await client.post('/api/sequences', json=request)).status_code, 409)
            self.assertEqual((await client.post('/api/control', json={'command': 'resume'})).status_code, 409)
            self.assertEqual((await client.post('/api/input', json={key: request[key] for key in ('lease_id', 'generation')})).status_code, 409)
            await client.post('/api/logout', json={})
        self.assertEqual(self.scheduler.sequences.report.status, 'cancelled')
        self.assertFalse((self.runtime.root / 'hello.txt').exists())

    async def test_restart_quiesce_closes_admission_until_explicit_release(self):
        origin = 'http://127.0.0.1:8765'
        app = create_console(self.controller, 'synthetic-token', origin, self.root, scheduler=self.scheduler)
        state = self.controller.state()
        restart_request = {'session_id': state['session_id']}
        admission = {'lease_id': state['lease_id'], 'generation': state['generation']}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin,
                                     headers={'Origin': origin}) as client:
            self.assertEqual((await client.post('/api/restart/quiesce', json=restart_request)).status_code, 401)
            await client.post('/api/login', json={'token': 'synthetic-token'})
            self.assertEqual((await client.post('/api/restart/quiesce?force=1', json=restart_request)).status_code, 400)
            self.assertEqual((await client.post('/api/restart/quiesce', json={'force': True})).status_code, 400)
            self.assertEqual((await client.post('/api/restart/quiesce', json={'session_id': 'desktop-session-' + '0' * 32})).status_code, 409)
            self.assertEqual((await client.post('/api/restart/quiesce', json=restart_request)).json(),
                             {'quiesced': True, 'session_id': self.controller.session_id})
            self.assertTrue((await client.get('/api/tasks')).json()['restart_quiesced'])
            self.assertEqual((await client.post('/api/restart/quiesce', json=restart_request)).status_code, 409)
            self.assertEqual((await client.post('/api/tasks', json={'kind': 'hello', **admission})).status_code, 409)
            self.assertEqual((await client.post('/api/sequences', json=self.request().model_dump())).status_code, 409)
            self.assertEqual((await client.post('/api/control', json={'command': 'pause'})).status_code, 409)
            self.assertEqual((await client.post('/api/input', json=admission)).status_code, 409)
            self.assertEqual((await client.post('/api/input/not-a-real-id/execute', json={})).status_code, 409)
            self.assertEqual((await client.post('/api/restart/release', json={'force': True})).status_code, 400)
            self.assertEqual((await client.post('/api/restart/release', json={'session_id': 'desktop-session-' + '0' * 32})).status_code, 409)
            self.assertTrue((await client.get('/api/tasks')).json()['restart_quiesced'])
            self.assertEqual((await client.post('/api/restart/release', json=restart_request)).json(),
                             {'quiesced': False, 'session_id': self.controller.session_id})
            self.assertFalse((await client.get('/api/tasks')).json()['restart_quiesced'])
            self.assertEqual((await client.post('/api/restart/release', json=restart_request)).status_code, 409)
            self.assertEqual((await client.post('/api/tasks', json={'kind': 'hello', **admission})).status_code, 200)
            self.assertEqual((await client.post('/api/restart/quiesce', json=restart_request)).status_code, 409)
        await self.scheduler.cancel()

    async def test_inter_step_control_cancels_before_next_job(self):
        original = self.scheduler.sequences.publish
        cancelling = []
        def publish(**changes):
            original(**changes)
            if changes.get('reason') == 'step_verified':
                self.controller.control('take-control')
                cancelling.append(asyncio.create_task(self.scheduler.cancel('take_control')))
        with patch.object(self.scheduler.sequences, 'publish', side_effect=publish):
            self.start()
            approval = await self.approval()
            self.scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
            await self.settled()
            await asyncio.gather(*cancelling)
        report = self.scheduler.sequences.report
        self.assertEqual((report.status, report.completed, len(report.job_ids)), ('cancelled', 1, 1))
        self.assertFalse(self.scheduler.reserved)

    async def test_audit_failure_after_admission_drains_and_cancels_unstarted_job(self):
        original = self.scheduler.sequences.publish
        def publish(**changes):
            if changes.get('reason') == 'step_started':
                raise OSError('Synthetic audit write failure')
            original(**changes)
        with patch.object(self.scheduler.sequences, 'publish', side_effect=publish):
            self.start()
            await self.settled()
        self.assertEqual(self.scheduler.sequences.report.status, 'failed')
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'cancelled')
        self.assertFalse(self.scheduler.reserved)
        self.assertFalse((self.runtime.root / 'hello.txt').exists())

    async def test_forged_matching_verification_cannot_authorize_next_step(self):
        original = self.scheduler.sequences.verified
        def verify(job_id):
            with self.store.connection:
                self.store.connection.execute("UPDATE verifications SET expected_json='\"synthetic-other\"',actual_json='\"synthetic-other\"'")
            return original(job_id)
        with patch.object(self.scheduler.sequences, 'verified', side_effect=verify):
            self.start()
            approval = await self.approval()
            self.scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
            report = await self.settled()
        self.assertEqual((report.status, report.completed, len(report.job_ids)), ('failed', 0, 1))

    @unittest.skipUnless(os.environ.get('AOS_BROWSER_TESTS') == '1', 'Requires pinned real isolated Chromium')
    async def test_all_three_tasks_are_sequential_verified_and_individually_approved(self):
        self.start(['hello', 'browser_form', 'vision_canvas'],
                   browser_manifest=REPO_ROOT / 'models/browser-manifest.json')
        previous = None
        for tool in ('filesystem.write', 'browser.fill', 'browser.submit', 'vision.click'):
            approval = await self.approval(previous)
            self.assertEqual(approval['action']['tool'], tool)
            previous = approval['approval_id']
            self.scheduler.respond(previous, approval['action_sha256'], True)
        report = await self.settled()
        self.assertEqual((report.status, report.completed), ('succeeded', 3))
        self.assertTrue(all(self.scheduler.sequences.verified(job_id) for job_id in report.job_ids))
        rows = self.store.connection.execute("SELECT payload_json FROM desktop_events WHERE kind='task_sequence' ORDER BY rowid").fetchall()
        self.assertEqual(len(rows), 8)
        for row in rows:
            SequenceStatus.model_validate_json(row[0])
        self.assertEqual(json.loads(rows[-1][0]), report.model_dump())
        self.assertIsNone(self.scheduler.active_runtime)
        self.assertFalse(self.scheduler.reserved)

    async def test_actual_process_crash_never_resumes_event_log(self):
        database = self.root / 'crash.sqlite'
        workspace = self.root / 'crash-workspace'
        script = '''
import asyncio, os, sys
from pathlib import Path
sys.path.insert(0, 'tests')
from test_desktop_tasks import FixtureDesktop
from aos.contracts import Settings
from aos.storage import TrajectoryStore
from aos.desktop_control import DesktopController
from aos.desktop_tasks import DesktopScheduler
from aos.decision import FixtureDecisionEngine
from aos.task_sequence import SequenceStart, SequencePlan
async def main():
    settings = Settings(database=Path(sys.argv[1]), workspace=Path(sys.argv[2]))
    store = TrajectoryStore(settings.database)
    runtime = FixtureDesktop(settings.workspace)
    runtime.start()
    controller = DesktopController(store, runtime)
    scheduler = DesktopScheduler(controller, settings, FixtureDecisionEngine(), browser_manifest=Path(sys.argv[3]))
    state = controller.state()
    scheduler.sequences.start(SequenceStart(plan=SequencePlan(kinds=['hello','browser_form']), lease_id=state['lease_id'], generation=state['generation']))
    for attempt in range(100):
        if scheduler.status()['approval']:
            os._exit(73)
        await asyncio.sleep(.01)
    raise AssertionError('No pending approval')
asyncio.run(main())
'''
        result = await asyncio.to_thread(subprocess.run, [sys.executable, '-c', script, str(database), str(workspace),
                                                        str(self.browser_manifest)],
                                         capture_output=True, text=True, timeout=10, cwd=REPO_ROOT)
        self.assertEqual(result.returncode, 73, result.stderr)
        store = TrajectoryStore(database)
        try:
            controller = DesktopController(store, self.runtime)
            scheduler = DesktopScheduler(controller, self.settings, FixtureDecisionEngine())
            self.assertIsNone(scheduler.sequences.report)
            self.assertFalse(scheduler.reserved)
            self.assertEqual(store.connection.execute('SELECT status FROM desktop_tasks').fetchone()[0], 'cancelled')
            self.assertEqual(store.connection.execute('SELECT status FROM desktop_approvals').fetchone()[0], 'revoked')
            self.assertEqual(store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)
            self.assertFalse((workspace / 'hello.txt').exists())
            await scheduler.close()
        finally:
            store.close()
