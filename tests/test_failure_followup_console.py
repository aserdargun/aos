import asyncio
import copy
import json
import os
import unittest
from unittest.mock import patch

import httpx

from aos.contracts import AOSFault, ErrorCode, HELLO_CONTENT, REPO_ROOT
from aos.decision import FixtureDecisionEngine
from aos.desktop_console import create_console
import test_desktop_tasks as desktop_fixtures


class FailedFixtureEngine(FixtureDecisionEngine):
    async def decide(self, state, options):
        raise AOSFault(ErrorCode.MODEL_FAILURE, 'synthetic private failure detail')


class FailureFollowupConsoleTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = desktop_fixtures.DesktopTaskTests.asyncSetUp
    asyncTearDown = desktop_fixtures.DesktopTaskTests.asyncTearDown

    async def approval(self, previous=None):
        async with asyncio.timeout(30):
            while True:
                approval = self.scheduler.status()['approval']
                if approval is not None and approval['approval_id'] != previous:
                    return approval
                if self.scheduler.task is not None and self.scheduler.task.done():
                    self.fail('Task stopped before its separately required approval')
                await asyncio.sleep(.01)

    def authority(self):
        control = self.controller.state()
        return {'lease_id': control['lease_id'], 'generation': control['generation']}

    def review(self, job_id):
        service = self.scheduler.failure_improvements
        candidate = service.preview(job_id)
        service.save(job_id, candidate['candidate_sha256'], True)
        option = next(choice for choice in candidate['review_options']
                      if choice['correction_code'] == 'refresh_observation')
        reviewed = service.review(job_id, candidate['candidate_sha256'], **option)
        return {'schema_version': '1.0', 'source_job_id': job_id,
                'candidate_sha256': reviewed['candidate_sha256'],
                'receipt_sha256': reviewed['review']['receipt_sha256']}

    async def failed_source(self):
        self.scheduler.engine = FailedFixtureEngine()
        started = self.scheduler.start(**self.authority())
        await self.scheduler.task
        self.scheduler.engine = FixtureDecisionEngine()
        return self.review(started['job_id'])

    def client(self):
        origin = 'http://testserver'
        app = create_console(self.controller, 'synthetic-token', origin, self.root,
                             scheduler=self.scheduler, web_profiles_root=self.root / 'profiles')
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin,
                                headers={'Origin': origin})

    async def preview(self, client, selection):
        response = await client.post('/api/tasks/failure-followup/preview', json=selection)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn('synthetic private failure detail', response.text)
        return response.json()

    def start_request(self, preview):
        return {'schema_version': '1.0', 'preview': preview,
                'confirm_sha256': preview['confirm_sha256'], 'consent': True, **self.authority()}

    async def test_actual_failed_task_to_fresh_approved_task_and_independent_result(self):
        selection = await self.failed_source()
        before_jobs = self.store.connection.execute('SELECT count(*) FROM desktop_tasks').fetchone()[0]
        async with self.client() as client:
            denied = await client.post('/api/tasks/failure-followup/preview', json=selection)
            self.assertEqual(denied.status_code, 401)
            await client.post('/api/login', json={'token': 'synthetic-token'})
            preview = await self.preview(client, selection)
            self.assertFalse((self.root / 'failure-followups').exists())
            self.assertEqual(self.store.connection.execute('SELECT count(*) FROM desktop_tasks').fetchone()[0], before_jobs)
            response = await client.post('/api/tasks/failure-followup/start', json=self.start_request(preview))
            self.assertEqual(response.status_code, 200, response.text)
            started = response.json()
            self.assertNotEqual(started['job_id'], selection['source_job_id'])
            self.assertTrue(started['manual_approval_required'])
            approval = await self.approval()
            self.assertIsNone(self.scheduler.status()['auto_approval'])
            self.assertFalse((self.runtime.root / 'hello.txt').exists())
            approved = await client.post('/api/approvals/' + approval['approval_id'], json={
                'action_sha256': approval['action_sha256'], 'accept': True})
            self.assertEqual(approved.status_code, 200, approved.text)
            await self.scheduler.task
            self.assertEqual((self.runtime.root / 'hello.txt').read_text(), HELLO_CONTENT)
            report = await client.post('/api/tasks/failure-followup/inspect',
                                      json={'schema_version': '1.0', 'job_id': started['job_id']})
            self.assertEqual(report.status_code, 200, report.text)
            body = report.json()
            self.assertEqual(body['intent_sha256'], started['intent_sha256'])
            self.assertTrue(body['historical_binding_verified'])
            self.assertTrue(body['current_source_valid'])
            self.assertTrue(body['current_review_valid'])
            self.assertEqual(body['outcome']['status'], 'verified')
            for flag in ('guidance_applied', 'causality_verified', 'gold', 'training_ready'):
                self.assertIs(body[flag], False)
            self.assertEqual(self.scheduler.status()['jobs'][0]['failure_followup'], {
                'intent_sha256': started['intent_sha256'], 'source_job_id': selection['source_job_id']})
            repeated = await client.post('/api/tasks/failure-followup/start', json=self.start_request(preview))
            self.assertEqual(repeated.status_code, 409)
            self.assertEqual(self.store.connection.execute('SELECT count(*) FROM desktop_tasks').fetchone()[0], before_jobs + 1)
        source = self.store.connection.execute('SELECT status,run_id FROM desktop_tasks WHERE job_id=?',
                                               (selection['source_job_id'],)).fetchone()
        self.assertEqual(source['status'], 'failed')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions WHERE run_id=?',
                                                       (source['run_id'],)).fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM model_calls').fetchone()[0], 0)

    async def test_body_origin_consent_and_control_fail_before_new_job(self):
        selection = await self.failed_source()
        async with self.client() as client:
            await client.post('/api/login', json={'token': 'synthetic-token'})
            preview = await self.preview(client, selection)
            for changes in ({'consent': False}, {'consent': 1}, {'generation': True},
                            {'generation': -1}, {'lease_id': 'wrong'}, {'confirm_sha256': '0' * 64}):
                response = await client.post('/api/tasks/failure-followup/start',
                                             json=self.start_request(preview) | changes)
                self.assertEqual(response.status_code, 409, response.text)
            for payload in (selection | {'extra': True}, {'schema_version': '1.0'},
                            selection | {'schema_version': '2.0'}):
                self.assertEqual((await client.post('/api/tasks/failure-followup/preview', json=payload)).status_code, 400)
            self.assertEqual((await client.post('/api/tasks/failure-followup/preview?other=1', json=selection)).status_code, 400)
            self.assertEqual((await client.post('/api/tasks/failure-followup/preview',
                headers={'Origin': 'http://wrong.example'}, json=selection)).status_code, 403)
            duplicate = '{"schema_version":"1.0","job_id":"one","job_id":"two"}'
            self.assertEqual((await client.post('/api/tasks/failure-followup/inspect', content=duplicate)).status_code, 400)
            self.assertFalse((self.root / 'failure-followups').exists())
            self.assertEqual(self.store.connection.execute('SELECT count(*) FROM desktop_tasks').fetchone()[0], 1)

    async def test_review_revoked_during_pending_approval_blocks_actual_effect(self):
        selection = await self.failed_source()
        preview = self.scheduler.preview_failure_followup(**{key: value for key, value in selection.items()
                                                            if key != 'schema_version'})
        self.scheduler.start_failure_followup(**{key: value for key, value in self.start_request(preview).items()
                                               if key != 'schema_version'})
        approval = await self.approval()
        self.scheduler.failure_improvements.revoke(selection['source_job_id'], selection['candidate_sha256'],
                                                   selection['receipt_sha256'])
        self.scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
        await self.scheduler.task
        self.assertFalse((self.runtime.root / 'hello.txt').exists())
        job = self.scheduler.status()['jobs'][0]
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions WHERE run_id=?',
                                                       (job['run_id'],)).fetchone()[0], 0)

    async def test_changed_configuration_and_preview_are_not_new_authority(self):
        selection = await self.failed_source()
        preview = self.scheduler.preview_failure_followup(**{key: value for key, value in selection.items()
                                                            if key != 'schema_version'})
        request = {key: value for key, value in self.start_request(preview).items() if key != 'schema_version'}
        changed = copy.deepcopy(preview)
        changed['target']['task_kind'] = 'browser_form'
        with self.assertRaises((ValueError, AOSFault)):
            self.scheduler.start_failure_followup(**(request | {'preview': changed}))
        self.scheduler.settings = self.settings.model_copy(update={'model_timeout_seconds': 121.0})
        with self.assertRaises((ValueError, AOSFault)):
            self.scheduler.start_failure_followup(**request)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM desktop_tasks').fetchone()[0], 1)

    async def test_failure_after_commit_burns_attempt_without_execution(self):
        selection = await self.failed_source()
        preview = self.scheduler.preview_failure_followup(**{key: value for key, value in selection.items()
                                                            if key != 'schema_version'})
        request = {key: value for key, value in self.start_request(preview).items() if key != 'schema_version'}
        with patch.object(self.scheduler, '_start', side_effect=ValueError('synthetic startup failure')):
            with self.assertRaises(ValueError):
                self.scheduler.start_failure_followup(**request)
        with self.assertRaises((ValueError, OSError)):
            self.scheduler.start_failure_followup(**request)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM desktop_tasks').fetchone()[0], 1)

    @unittest.skipUnless(os.environ.get('AOS_BROWSER_TESTS') == '1', 'Requires the pinned local Chromium runtime')
    async def test_real_browser_rejected_fill_then_fresh_two_approval_followup(self):
        self.scheduler.browser_manifest = REPO_ROOT / 'models/browser-manifest.json'
        source = self.scheduler.start(**self.authority(), kind='browser_form')
        approval = await self.approval()
        self.scheduler.respond(approval['approval_id'], approval['action_sha256'], False)
        await asyncio.gather(self.scheduler.task, return_exceptions=True)
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'cancelled')
        selection = self.review(source['job_id'])
        preview = self.scheduler.preview_failure_followup(**{key: value for key, value in selection.items()
                                                            if key != 'schema_version'})
        started = self.scheduler.start_failure_followup(**{key: value for key, value in self.start_request(preview).items()
                                                          if key != 'schema_version'})
        previous = None
        for expected in ('browser.fill', 'browser.submit'):
            approval = await self.approval(previous)
            self.assertEqual(approval['action']['tool'], expected)
            self.scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
            previous = approval['approval_id']
        await self.scheduler.task
        report = self.scheduler.failure_followups.inspect(started['job_id'])
        self.assertEqual(report['outcome']['status'], 'verified', report)
        self.assertEqual(report['outcome']['verification_count'], 2)
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'succeeded')
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM actions WHERE tool='browser.submit' AND status='ok'").fetchone()[0], 1)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM model_calls').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
