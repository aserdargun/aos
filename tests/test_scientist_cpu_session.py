"""Controlled API integration with a synthetic CPU identity and transport, not model or user acceptance."""

import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import httpx

from aos.desktop_console import create_console
from aos.desktop_control import DesktopController
from aos.scientist_cpu_capability import cpu_capability_sha256
from aos.scientist_cpu_session import create_scientist_cpu_service
from aos.scientist_lab import ScientistLabClient
from aos.scientist_transport import ScientistAdmissionError
from aos.storage import TrajectoryStore

import test_scientist_cpu_capability as capability_fixtures


class ScientistCpuSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = capability_fixtures.ScientistCpuCapabilityTests()
        self.fixture.setUp()
        self.store = TrajectoryStore(self.fixture.root / 'synthetic-cpu-session.sqlite')
        self.controller = DesktopController(self.store,
            SimpleNamespace(runtime_id='synthetic-cpu-controller', pins={'image_id': 'synthetic-no-desktop-image'}))
        self.requests = []
        self.remote_state = 'queued'
        self.remote_run = '11111111-1111-1111-1111-111111111111'
        self.body = {'run_id': self.remote_run, 'status': 'completed',
            'synthetic_scorer': {'experiment_count': 1, 'model_tokens': 0}}
        self.report_sha = cpu_capability_sha256(self.body)
        self.fixture.client._request = Mock(side_effect=self.transport)
        self.service = create_scientist_cpu_service(self.controller, self.fixture.client, self.fixture.grant)
        self.app = create_console(self.controller, 'synthetic-console-token', 'http://testserver', self.fixture.root,
            scientist_lab=self.service, web_profiles_root=self.fixture.root / 'synthetic-profiles')
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://testserver')
        self.headers = {'Origin': 'http://testserver'}
        self.proposal = {'suite': self.fixture.grant.capability.suite_id, 'track': 'mode',
            'program_version': 'mode-grid.v1', 'budget': {'experiments': 1, 'wall_seconds': 30, 'model_tokens': 0}}

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.service.close_async(timeout_seconds=1)
        self.store.close()
        self.fixture.doCleanups()

    def transport(self, method, path, body, deadline, *, bound):
        self.requests.append((method, path))
        if path == '/v1/aos-cpu-capability/' + self.fixture.grant.capability.suite_id:
            self.assertEqual((method, body, bound), ('GET', b'', 16384))
            return json.dumps(self.fixture.wire).encode()
        if path == '/v1/runs':
            self.assertEqual(method, 'POST')
            with closing(sqlite3.connect('file:' + str(self.fixture.root / 'synthetic-cpu-session.sqlite') + '?mode=ro', uri=True)) as reader:
                self.assertEqual(reader.execute('SELECT state FROM scientist_lab_actions').fetchone()[0], 'intent')
            self.assertEqual(json.loads(body)['budget']['model_tokens'], 0)
            return json.dumps({'run_id': self.remote_run, 'state': self.remote_state, 'reused': False}).encode()
        if path == '/v1/runs/' + self.remote_run:
            return json.dumps({'run_id': self.remote_run, 'origin': 'aos', 'purpose': 'research',
                'state': self.remote_state, 'created_at': '2026-10-03T00:00:00Z',
                'updated_at': '2026-10-03T00:00:01Z', 'stop_requested': False,
                'report_sha256': self.report_sha if self.remote_state == 'completed' else None}).encode()
        if path == '/v1/runs/' + self.remote_run + '/report':
            return json.dumps({'run_id': self.remote_run, 'report_sha256': self.report_sha,
                'report': self.body, 'verified_at': '2026-10-03T00:00:02Z'}).encode()
        raise AssertionError('Unexpected synthetic transport request')

    async def post(self, operation, value):
        return await self.client.post('/api/scientist/' + operation, headers=self.headers, json=value)

    async def login(self):
        result = await self.client.post('/api/login', headers=self.headers, json={'token': 'synthetic-console-token'})
        self.assertEqual(result.status_code, 200)

    async def test_actual_asgi_login_approval_intent_status_and_independent_report(self):
        self.assertEqual(self.requests, [])
        self.assertEqual((await self.post('propose', self.proposal)).status_code, 401)
        await self.login()
        approval_response = await self.post('propose', self.proposal)
        self.assertEqual(approval_response.status_code, 200, approval_response.text)
        approval = approval_response.json()
        self.assertEqual((await self.post('execute', {'action_id': approval['action_id']})).status_code, 409)
        self.assertFalse(any(method == 'POST' for method, _path in self.requests))
        response = await self.post('approve', {'action_id': approval['action_id'],
            'envelope_sha256': approval['envelope_sha256'], 'accept': True})
        self.assertEqual(response.status_code, 200, response.text)
        started = await self.post('execute', {'action_id': approval['action_id']})
        self.assertEqual(started.status_code, 200, started.text)
        run_id = approval['envelope']['task']['request']['external_run_id']
        self.assertEqual((await self.post('status', {'run_id': run_id})).json()['state'], 'queued')
        self.assertEqual((await self.post('report', {'run_id': run_id})).status_code, 400)
        self.remote_state = 'completed'
        report = await self.post('report', {'run_id': run_id})
        self.assertEqual(report.status_code, 200, report.text)
        self.assertEqual(report.json()['report_sha256'], self.report_sha)
        retained = await self.post('save_report', {'run_id': run_id, 'expected_report_sha256': self.report_sha})
        self.assertEqual(retained.status_code, 200, retained.text)
        self.assertEqual(self.store.connection.execute('SELECT state FROM scientist_lab_actions').fetchone()[0], 'acknowledged')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM scientist_lab_readbacks').fetchone()[0], 1)
        self.assertEqual(sum(method == 'POST' for method, _path in self.requests), 1)
        await self.service.close_async(timeout_seconds=1)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM scientist_lab_jobs').fetchone()[0], 1)
        self.assertTrue(self.fixture.client._closed)

    async def test_no_human_approval_or_stale_envelope_never_posts(self):
        await self.login()
        approval = (await self.post('propose', self.proposal)).json()
        self.assertEqual((await self.post('execute', {'action_id': approval['action_id']})).status_code, 409)
        denied = await self.post('approve', {'action_id': approval['action_id'],
            'envelope_sha256': '0' * 64, 'accept': True})
        self.assertEqual(denied.status_code, 409)
        self.assertEqual(self.store.connection.execute('SELECT state FROM scientist_lab_actions').fetchone()[0], 'pending')
        self.assertFalse(any(method == 'POST' for method, _path in self.requests))

    async def test_capability_drift_and_controller_generation_deny_before_start(self):
        await self.login()
        approval = (await self.post('propose', self.proposal)).json()
        self.assertEqual((await self.post('approve', {'action_id': approval['action_id'],
            'envelope_sha256': approval['envelope_sha256'], 'accept': True})).status_code, 200)
        self.fixture.wire['snapshot_sha256'] = '0' * 64
        self.assertEqual((await self.post('execute', {'action_id': approval['action_id']})).status_code, 409)
        self.fixture.wire['snapshot_sha256'] = self.fixture.grant.capability.snapshot_sha256
        with self.store.connection:
            self.store.connection.execute('UPDATE desktop_sessions SET generation=generation+1 WHERE session_id=?',
                (self.controller.session_id,))
        self.assertEqual((await self.post('execute', {'action_id': approval['action_id']})).status_code, 409)
        self.assertFalse(any(method == 'POST' for method, _path in self.requests))

    async def test_factory_rejects_rebinding_or_broader_suite_without_remote_calls(self):
        with self.assertRaises(ScientistAdmissionError):
            create_scientist_cpu_service(self.controller, self.fixture.client, self.fixture.grant)
        fresh = ScientistLabClient(self.fixture.client.authority_url, self.fixture.client.token_file,
            principal_id=self.fixture.client.principal_id,
            allowed_suites=frozenset({self.fixture.grant.capability.suite_id, 'synthetic.extra.v1'}))
        with self.assertRaises(ScientistAdmissionError):
            create_scientist_cpu_service(self.controller, fresh, self.fixture.grant)
        with self.assertRaises(ScientistAdmissionError):
            create_scientist_cpu_service(SimpleNamespace(store=self.store), fresh, self.fixture.grant)
        self.assertEqual(self.requests, [])


if __name__ == '__main__':
    unittest.main()
