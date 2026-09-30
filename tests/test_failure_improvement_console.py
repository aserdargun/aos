import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import httpx
from fastapi.testclient import TestClient

from aos.contracts import AOSFault, ErrorCode, REPO_ROOT
from aos.decision import FixtureDecisionEngine
from aos.desktop_console import create_console
import test_desktop_tasks as desktop_fixtures


class FailureImprovementConsoleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.response = json.loads((REPO_ROOT / 'examples/failure_improvement.json').read_text())
        self.calls = []
        self.control = {'owner': 'AGENT', 'status': 'running', 'lease_id': 'lease-synthetic', 'generation': 2}
        self.controller = SimpleNamespace(state=lambda: dict(self.control))
        service = SimpleNamespace(**{operation: self.operation(operation)
                                   for operation in ('preview', 'save', 'review', 'revoke')})
        self.scheduler = SimpleNamespace(failure_improvements=service, reserved=False,
                                         closed=False, restart_quiesced=False)
        self.app = create_console(self.controller, 'synthetic-token', 'http://testserver', self.root,
                                  scheduler=self.scheduler, web_profiles_root=self.root / 'profiles')
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.headers = {'Origin': 'http://testserver'}
        self.client.post('/api/login', headers=self.headers, json={'token': 'synthetic-token'})
        self.selection = {'schema_version': '1.0', 'job_id': self.response['job_id']}
        self.authority = {'lease_id': 'lease-synthetic', 'generation': 2}

    def operation(self, name):
        def invoke(**arguments):
            self.calls.append((name, arguments))
            return copy.deepcopy(self.response)
        return invoke

    def request(self, operation, payload):
        return self.client.post('/api/tasks/failure-improvement/' + operation,
                                headers=self.headers, json=payload)

    def save_request(self):
        return self.selection | self.authority | {
            'confirm_sha256': self.response['candidate_sha256'], 'consent': True}

    def test_preview_is_authenticated_and_does_not_require_or_create_write_authority(self):
        self.control['owner'] = 'HUMAN'
        result = self.request('preview', self.selection)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.calls, [('preview', {'job_id': self.response['job_id']})])
        self.assertEqual(result.json(), self.response)
        with TestClient(self.app) as anonymous:
            result = anonymous.post('/api/tasks/failure-improvement/preview', headers=self.headers,
                                    json=self.selection)
        self.assertEqual(result.status_code, 401)
        self.assertEqual(len(self.calls), 1)

    def test_explicit_save_review_and_revoke_forward_exact_selections(self):
        request = self.save_request()
        self.assertEqual(self.request('save', request).status_code, 200)
        self.assertEqual(self.calls[-1], ('save', {'job_id': request['job_id'],
            'confirm_sha256': request['confirm_sha256'], 'consent': True}))
        option = self.response['review_options'][0]
        review = self.selection | self.authority | option | {
            'candidate_sha256': self.response['candidate_sha256']}
        self.assertEqual(self.request('review', review).status_code, 200)
        self.assertEqual(self.calls[-1][0], 'review')
        self.assertNotIn('lease_id', self.calls[-1][1])
        revoke = self.selection | self.authority | {'candidate_sha256': self.response['candidate_sha256'],
                                                   'receipt_sha256': 'b' * 64}
        self.assertEqual(self.request('revoke', revoke).status_code, 200)
        self.assertEqual(self.calls[-1], ('revoke', {'job_id': revoke['job_id'],
            'candidate_sha256': revoke['candidate_sha256'], 'receipt_sha256': 'b' * 64}))

    def test_stale_control_reservation_shutdown_and_restart_never_reach_service(self):
        original = dict(self.control)
        for changes in ({'owner': 'HUMAN'}, {'status': 'paused'}, {'generation': 3}, {'lease_id': 'other'}):
            with self.subTest(changes=changes):
                self.control.update(changes)
                self.assertEqual(self.request('save', self.save_request()).status_code, 409)
                self.control = dict(original)
        for field in ('reserved', 'closed', 'restart_quiesced'):
            with self.subTest(field=field):
                setattr(self.scheduler, field, True)
                self.assertEqual(self.request('save', self.save_request()).status_code, 409)
                setattr(self.scheduler, field, False)
        for changes in ({'generation': True}, {'generation': '2'}, {'lease_id': None}):
            self.assertEqual(self.request('save', self.save_request() | changes).status_code, 409)
        self.assertEqual(self.calls, [])

    def test_strict_body_query_duplicate_keys_and_origin_checks(self):
        for payload in (self.selection | {'extra': 'no'}, self.selection | {'job_id': '../outside'},
                        self.selection | {'job_id': 5}, self.selection | {'schema_version': '2.0'},
                        {'schema_version': '1.0'}):
            self.assertEqual(self.request('preview', payload).status_code, 400)
        self.assertEqual(self.request('save', self.selection | self.authority).status_code, 400)
        self.assertEqual(self.request('train', self.selection).status_code, 400)
        self.assertEqual(self.request('preview?job_id=elsewhere', self.selection).status_code, 400)
        result = self.client.post('/api/tasks/failure-improvement/preview', headers=self.headers,
            content='{"schema_version":"1.0","job_id":"one","job_id":"two"}')
        self.assertEqual(result.status_code, 400)
        result = self.client.post('/api/tasks/failure-improvement/save',
            headers={'Origin': 'http://wrong.example'}, json=self.save_request())
        self.assertEqual(result.status_code, 403)
        self.assertEqual(self.calls, [])

    def test_missing_backend_corrupted_response_and_source_errors_are_sanitized(self):
        service = self.scheduler.failure_improvements
        self.scheduler.failure_improvements = None
        self.assertEqual(self.request('preview', self.selection).status_code, 409)
        self.scheduler.failure_improvements = service
        original = copy.deepcopy(self.response)
        for modification in ({'secret': 'synthetic-private-sentinel'}, {'job_id': 'job-other'},
                             {'available': False}, {'candidate': {'training_ready': True}}):
            self.response = original | modification
            result = self.request('preview', self.selection)
            self.assertEqual(result.status_code, 409)
            self.assertNotIn('synthetic-private-sentinel', result.text)
        self.response = original

        def unavailable(**arguments):
            raise ValueError('synthetic-private-source-path')

        service.preview = unavailable
        result = self.request('preview', self.selection)
        self.assertEqual(result.status_code, 409)
        self.assertNotIn('synthetic-private-source-path', result.text)


class FailedFixtureEngine(FixtureDecisionEngine):
    async def decide(self, state, options):
        raise AOSFault(ErrorCode.MODEL_FAILURE, 'synthetic private diagnostic detail')


class FailureImprovementFlowTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = desktop_fixtures.DesktopTaskTests.asyncSetUp
    asyncTearDown = desktop_fixtures.DesktopTaskTests.asyncTearDown

    async def test_actual_failed_fixture_task_to_authenticated_review_without_retry(self):
        self.scheduler.engine = FailedFixtureEngine()
        control = self.controller.state()
        job = self.scheduler.start(control['lease_id'], control['generation'])
        await self.scheduler.task
        job_id = job['job_id']
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'failed')
        self.assertTrue(self.scheduler.status()['failure_improvement_available'])
        before_changes = self.store.connection.total_changes
        self.assertFalse((self.root / 'failure-improvements').exists())
        origin = 'http://testserver'
        app = create_console(self.controller, 'synthetic-token', origin, self.root,
                             scheduler=self.scheduler, web_profiles_root=self.root / 'profiles')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin,
                                     headers={'Origin': origin}) as client:
            await client.post('/api/login', json={'token': 'synthetic-token'})
            selection = {'schema_version': '1.0', 'job_id': job_id}
            endpoint = '/api/tasks/failure-improvement/'
            result = await client.post(endpoint + 'preview', json=selection)
            self.assertEqual(result.status_code, 200, result.text)
            preview = result.json()
            self.assertEqual(preview['candidate']['failure_code'], 'MODEL_FAILURE')
            self.assertTrue(preview['candidate']['synthetic'])
            self.assertFalse(preview['candidate']['gold'])
            self.assertFalse(preview['saved'])
            self.assertNotIn('synthetic private diagnostic detail', result.text)
            self.assertFalse((self.root / 'failure-improvements').exists())
            control = self.controller.state()
            authority = {'lease_id': control['lease_id'], 'generation': control['generation']}
            save = selection | authority | {'consent': False, 'confirm_sha256': preview['candidate_sha256']}
            self.assertEqual((await client.post(endpoint + 'save', json=save)).status_code, 409)
            self.assertFalse((self.root / 'failure-improvements').exists())
            save['consent'] = True
            result = await client.post(endpoint + 'save', json=save)
            self.assertEqual(result.status_code, 200, result.text)
            saved = result.json()
            self.assertTrue(saved['saved'])
            option = next(option for option in saved['review_options']
                          if option['decision'] == 'accept' and option['correction_code'] == 'repair_environment')
            review = selection | authority | option | {'candidate_sha256': saved['candidate_sha256']}
            result = await client.post(endpoint + 'review', json=review)
            self.assertEqual(result.status_code, 200, result.text)
            reviewed = result.json()
            self.assertFalse(reviewed['candidate']['training_ready'])
            self.assertEqual(reviewed['review']['correction_code'], 'repair_environment')
            revoke = selection | authority | {'candidate_sha256': reviewed['candidate_sha256'],
                                               'receipt_sha256': reviewed['review']['receipt_sha256']}
            result = await client.post(endpoint + 'revoke', json=revoke)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertTrue(result.json()['review']['revoked'])
        self.assertEqual(self.store.connection.total_changes, before_changes)
        self.assertFalse(self.scheduler.busy)
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM actions').fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM desktop_tasks').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
