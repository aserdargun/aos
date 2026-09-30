import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch

import httpx

from aos.contracts import HELLO_CONTENT, canonical, digest
from aos.desktop_console import create_console
from aos.task_knowledge import FLAGS, PREFIX, TaskKnowledgeService
import test_desktop_tasks as desktop_fixtures


SCOPE = {'application_id': 'synthetic-app', 'tenant_id': 'synthetic-tenant', 'account_role': 'reader'}
TEXT = 'Synthetic hello manual: use the fresh file observation and verify exact authorized content.'


class TaskKnowledgeConsoleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await desktop_fixtures.DesktopTaskTests.asyncSetUp(self)
        self.app = create_console(self.controller, 'synthetic-token', 'http://testserver', self.root,
            scheduler=self.scheduler, recovery_database=self.settings.database,
            knowledge_root=self.root / 'knowledge', web_profiles_root=self.root / 'profiles')
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
            base_url='http://testserver', headers={'Origin': 'http://testserver'})
        self.service = self.scheduler.task_knowledge
        self.assertIsInstance(self.service, TaskKnowledgeService)
        self.knowledge = self.service.store
        preview = self.knowledge.publish_preview(scope=SCOPE, source_id='manual',
            title='Synthetic hello manual', text=TEXT, previous_sha256=None,
            expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            rights_attested=True, storage_consent=True, synthetic=True)
        self.document = self.knowledge.publish(preview=preview, confirm_sha256=preview['preview_sha256'])
        self.review()

    async def asyncTearDown(self):
        await self.client.aclose()
        await desktop_fixtures.DesktopTaskTests.asyncTearDown(self)

    def review(self, decision='accept'):
        preview = self.knowledge.review_preview(scope=SCOPE,
            document_sha256=self.document['document_sha256'], decision=decision)
        return self.knowledge.review(preview=preview, confirm_sha256=preview['preview_sha256'])

    def authority(self):
        control = self.controller.state()
        return {'lease_id': control['lease_id'], 'generation': control['generation']}

    def preview_request(self, **changes):
        return {'schema_version': '1.0', 'scope': SCOPE, 'query': 'hello', 'top_k': 4,
                'context_chars': 2048, 'task_kind': 'hello', **self.authority(), **changes}

    def start_request(self, preview, **changes):
        return {'schema_version': '1.0', 'preview': preview,
                'confirm_sha256': preview['confirm_sha256'], 'consent': True,
                **self.authority(), **changes}

    async def login(self, client=None):
        response = await (client or self.client).post('/api/login', json={'token': 'synthetic-token'})
        self.assertEqual(response.status_code, 200, response.text)

    async def request(self, operation, value):
        return await self.client.post('/api/tasks/knowledge/' + operation, json=value)

    async def preview(self):
        response = await self.request('preview', self.preview_request())
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def start(self, preview):
        response = await self.request('start', self.start_request(preview))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def approval(self):
        async with asyncio.timeout(3):
            while True:
                status = self.scheduler.status()
                if status['approval'] is not None:
                    return status['approval']
                if self.scheduler.task is not None and self.scheduler.task.done():
                    self.fail('Task ended before its separately required manual approval')
                await asyncio.sleep(.001)

    async def approve(self, started):
        approval = await self.approval()
        self.assertEqual(approval['job_id'], started['job_id'])
        response = await self.client.post('/api/approvals/' + approval['approval_id'], json={
            'action_sha256': approval['action_sha256'], 'accept': True})
        self.assertEqual(response.status_code, 200, response.text)
        await self.scheduler.task

    async def report(self, started):
        response = await self.request('report', {'schema_version': '1.0', 'job_id': started['job_id']})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def no_task_effects(self):
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM desktop_tasks').fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)
        self.assertFalse((self.runtime.root / 'hello.txt').exists())

    async def test_authentication_origin_host_and_capability_boundary(self):
        for operation, value in (
            ('preview', self.preview_request()),
            ('start', {'schema_version': '1.0'}),
            ('report', {'schema_version': '1.0', 'job_id': 'synthetic-unknown-job'}),
        ):
            with self.subTest(operation=operation):
                response = await self.request(operation, value)
                self.assertEqual(response.status_code, 401)
        rejected = await self.client.post('/api/login', json={'token': 'wrong-synthetic-token'})
        self.assertEqual(rejected.status_code, 401)
        await self.login()
        capabilities = await self.client.get('/api/tasks')
        self.assertEqual(capabilities.status_code, 200)
        self.assertTrue(capabilities.json()['task_knowledge_available'])
        self.assertTrue(capabilities.json()['knowledge_available'])
        self.assertFalse(capabilities.json()['knowledge_answer_available'])
        for headers in ({'Origin': 'http://other.invalid'}, {'Host': 'other.invalid'}):
            with self.subTest(headers=headers):
                response = await self.client.post('/api/tasks/knowledge/preview',
                    json=self.preview_request(), headers=headers)
                self.assertEqual(response.status_code, 403)
        self.assertEqual((await self.client.post('/api/logout')).status_code, 200)
        self.assertEqual((await self.request('preview', self.preview_request())).status_code, 401)
        self.no_task_effects()

    async def test_exact_body_duplicate_nested_fields_limits_and_no_implicit_consent(self):
        await self.login()
        preview = await self.preview()
        requests = {'preview': self.preview_request(), 'start': self.start_request(preview),
                    'report': {'schema_version': '1.0', 'job_id': 'synthetic-unknown-job'}}
        for operation, value in requests.items():
            for invalid in (value | {'extra': True}, {'schema_version': '1.0'}):
                with self.subTest(operation=operation, invalid=invalid):
                    self.assertEqual((await self.request(operation, invalid)).status_code, 400)
            duplicate = canonical(value).replace('"schema_version":"1.0"',
                '"schema_version":"1.0","schema_version":"1.0"', 1)
            self.assertEqual((await self.client.post('/api/tasks/knowledge/' + operation,
                content=duplicate)).status_code, 400)
            self.assertEqual((await self.client.post('/api/tasks/knowledge/' + operation + '?unexpected=1',
                json=value)).status_code, 400)
        duplicate_scope = canonical(self.preview_request()).replace('"tenant_id":"synthetic-tenant"',
            '"tenant_id":"synthetic-tenant","tenant_id":"foreign-tenant"')
        self.assertEqual((await self.client.post('/api/tasks/knowledge/preview', content=duplicate_scope)).status_code, 400)
        for changes in ({'consent': False}, {'consent': 1}, {'consent': 'true'},
                        {'generation': True}, {'approve_all': True}, {'learning_metadata': True}):
            with self.subTest(changes=changes):
                self.assertEqual((await self.request('start', self.start_request(preview, **changes))).status_code, 400)
        for changes in ({'top_k': 5}, {'context_chars': 2049}, {'query': 'x' * 513}):
            with self.subTest(changes=changes):
                self.assertEqual((await self.request('preview', self.preview_request(**changes))).status_code, 400)
        self.assertEqual((await self.request('unknown', self.preview_request())).status_code, 400)
        oversized = '{"padding":"' + 'x' * 524288 + '"}'
        self.assertEqual((await self.client.post('/api/tasks/knowledge/preview', content=oversized)).status_code, 413)
        self.assertFalse(self.service.directory.exists())
        self.no_task_effects()

    async def test_preview_start_manual_effect_report_and_replay_are_bound_over_http(self):
        await self.login()
        before = self.store.connection.total_changes
        preview = await self.preview()
        self.assertEqual(self.store.connection.total_changes, before)
        self.assertFalse(self.service.directory.exists())
        self.assertTrue(preview['context_text'].startswith(PREFIX))
        self.assertIn(TEXT, preview['context_text'])
        self.assertEqual(preview['confirm_sha256'], digest({
            key: value for key, value in preview.items() if key != 'confirm_sha256'}))
        self.no_task_effects()
        started = await self.start(preview)
        self.assertTrue(started['manual_approval_required'])
        approval = await self.approval()
        self.assertEqual(approval['job_id'], started['job_id'])
        self.assertIsNone(self.scheduler.status()['auto_approval'])
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)
        self.assertFalse((self.runtime.root / 'hello.txt').exists())
        pending = await self.report(started)
        self.assertTrue(pending['context_binding_verified'])
        self.assertFalse(pending['model_request_verified'])
        self.assertFalse(pending['knowledge_applied'])
        self.assertEqual(pending['outcome']['status'], 'not_verified')
        await self.approve(started)
        self.assertEqual((self.runtime.root / 'hello.txt').read_text(), HELLO_CONTENT)
        report = await self.report(started)
        self.assertEqual(report['intent_sha256'], started['intent_sha256'])
        self.assertEqual(report['intent_sha256'], digest(report['intent']))
        self.assertEqual(report['admission_sha256'], digest(report['admission']))
        self.assertEqual(report['admission']['job_id'], started['job_id'])
        self.assertTrue(report['historical_binding_verified'])
        self.assertTrue(report['context_binding_verified'])
        self.assertTrue(report['current_source_valid'])
        self.assertTrue(report['current_authority_valid'])
        self.assertFalse(report['model_request_verified'])
        self.assertFalse(report['knowledge_applied'])
        self.assertEqual(report['prepared_context_count'], 1)
        self.assertEqual(report['dispatched_context_count'], 0)
        self.assertEqual(report['successful_model_call_count'], 0)
        self.assertEqual(report['outcome']['status'], 'verified')
        self.assertIsNone(report['context_proofs'][0]['call_id'])
        for key, expected in FLAGS.items():
            self.assertIs(report[key], expected)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM model_calls').fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute('SELECT training_eligible FROM runs').fetchone()[0], 0)
        repeated = await self.request('start', self.start_request(preview))
        self.assertEqual(repeated.status_code, 409)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM desktop_tasks').fetchone()[0], 1)
        self.review('revoke')
        historical = await self.report(started)
        self.assertTrue(historical['historical_binding_verified'])
        self.assertTrue(historical['context_binding_verified'])
        self.assertFalse(historical['current_source_valid'])
        self.assertFalse(historical['knowledge_applied'])
        self.assertEqual(historical['outcome']['status'], 'verified')

    async def test_unavailable_backend_returns_no_task_knowledge_capability(self):
        app = create_console(self.controller, 'synthetic-token', 'http://testserver', self.root,
            scheduler=None, knowledge_root=self.knowledge.root, web_profiles_root=self.root / 'profiles')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver',
                                    headers={'Origin': 'http://testserver'}) as client:
            await self.login(client)
            capabilities = (await client.get('/api/tasks')).json()
            self.assertTrue(capabilities['knowledge_available'])
            self.assertFalse(capabilities['task_knowledge_available'])
            preview = self.service.preview(**{key: value for key, value in self.preview_request().items()
                                              if key != 'schema_version'})
            for operation, value in (
                ('preview', self.preview_request()), ('start', self.start_request(preview)),
                ('report', {'schema_version': '1.0', 'job_id': 'synthetic-unknown-job'}),
            ):
                with self.subTest(operation=operation):
                    response = await client.post('/api/tasks/knowledge/' + operation, json=value)
                    self.assertEqual(response.status_code, 409)
        self.no_task_effects()

    async def test_changed_target_hash_and_configuration_cannot_borrow_preview(self):
        await self.login()
        preview = await self.preview()
        for changes in (
            {'session_id': 'desktop-session-' + '0' * 32},
            {'parent_runtime_id': 'synthetic-other-runtime'},
            {'configuration_sha256': '0' * 64},
            {'system1_deployment_id': 'fixture-other-decision'},
            {'task_kind': 'browser_form'},
        ):
            with self.subTest(changes=changes):
                mutated = deepcopy(preview)
                mutated['target'].update(changes)
                mutated['confirm_sha256'] = digest({key: value for key, value in mutated.items()
                                                   if key != 'confirm_sha256'})
                response = await self.request('start', self.start_request(mutated))
                self.assertEqual(response.status_code, 409, response.text)
        wrong_hash = await self.request('start', self.start_request(preview, confirm_sha256='0' * 64))
        self.assertEqual(wrong_hash.status_code, 409)
        self.scheduler.settings = self.scheduler.settings.model_copy(update={'execute_min': .89})
        changed_config = await self.request('start', self.start_request(preview))
        self.assertEqual(changed_config.status_code, 409)
        self.assertFalse(self.service.directory.exists())
        self.no_task_effects()

    async def test_control_lease_rotation_rejects_old_and_rebound_preview(self):
        await self.login()
        preview = await self.preview()
        old_request = self.start_request(preview)
        takeover = await self.client.post('/api/control', json={'command': 'take-control'})
        self.assertEqual(takeover.status_code, 200, takeover.text)
        self.assertEqual((await self.request('start', old_request)).status_code, 409)
        returned = await self.client.post('/api/control', json={'command': 'return-control'})
        self.assertEqual(returned.status_code, 200, returned.text)
        self.assertNotEqual(self.authority()['lease_id'], preview['target']['lease_id'])
        rebound = await self.request('start', self.start_request(preview))
        self.assertEqual(rebound.status_code, 409)
        fresh = await self.preview()
        self.assertEqual(fresh['target']['lease_id'], self.authority()['lease_id'])
        self.assertNotEqual(fresh['confirm_sha256'], preview['confirm_sha256'])
        self.no_task_effects()

    async def test_foreign_empty_and_incompatible_lanes_fail_before_new_job(self):
        await self.login()
        for changes in (
            {'scope': SCOPE | {'tenant_id': 'foreign-tenant'}},
            {'query': 'unmatchedsyntheticterm'}, {'task_kind': 'browser_remote_form'},
        ):
            with self.subTest(changes=changes):
                response = await self.request('preview', self.preview_request(**changes))
                self.assertEqual(response.status_code, 409, response.text)
        with patch.object(self.scheduler, '_owned_skill_reuse', {'synthetic': True}):
            response = await self.request('preview', self.preview_request())
            self.assertEqual(response.status_code, 409)
        report = await self.request('report', {'schema_version': '1.0', 'job_id': 'synthetic-unknown-job'})
        self.assertEqual(report.status_code, 409)
        self.no_task_effects()

    async def test_revoked_source_during_http_approval_prevents_write(self):
        await self.login()
        started = await self.start(await self.preview())
        approval = await self.approval()
        self.review('revoke')
        rejected = await self.client.post('/api/approvals/' + approval['approval_id'], json={
            'action_sha256': approval['action_sha256'], 'accept': True})
        self.assertEqual(rejected.status_code, 409, rejected.text)
        self.assertFalse((self.runtime.root / 'hello.txt').exists())
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)
        report = await self.report(started)
        self.assertFalse(report['current_source_valid'])
        self.assertFalse(report['model_request_verified'])
        self.assertFalse(report['knowledge_applied'])
        self.assertNotEqual(report['outcome']['status'], 'verified')


if __name__ == '__main__':
    unittest.main()
