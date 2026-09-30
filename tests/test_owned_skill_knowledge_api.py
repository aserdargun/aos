import copy
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from aos.desktop_console import create_console
from aos.dataset import validator
from aos.contracts import canonical, digest
from test_owned_skill_knowledge_service import FixtureKnowledgePlanner, SCOPE, make_fixture


class OwnedSkillKnowledgeApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.planner = FixtureKnowledgePlanner()
        self.scheduler, self.store, self.service, self.preview_arguments = make_fixture(self.root, self.planner)
        self.planning = self.scheduler.owned_skill_planning
        self.app = create_console(self.scheduler.controller, 'synthetic-token', 'http://testserver', self.root,
                                  scheduler=self.scheduler, web_profiles_root=self.root / 'profiles')
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://testserver',
                                       headers={'Origin': 'http://testserver'})
        await self.client.post('/api/login', json={'token': 'synthetic-token'})

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.planning.close()
        self.temporary.cleanup()

    async def request(self, operation, payload):
        return await self.client.post('/api/tasks/owned-skill-knowledge/' + operation, json=payload)

    async def preview(self):
        response = await self.request('preview', self.preview_arguments)
        self.assertEqual(response.status_code, 200, response.text)
        transport = response.json()
        validator('owned_skill_knowledge_preview_transport').validate(transport)
        return transport['preview']

    def start_arguments(self, preview):
        return {'schema_version': '1.0', 'preview': preview, 'confirm_sha256': preview['confirm_sha256'],
                'inference_consent': True, 'storage_consent': True,
                'lease_id': preview['authority']['lease_id'], 'generation': preview['authority']['generation']}

    async def test_authenticated_preview_start_report_and_fixture_boundary(self):
        preview = await self.preview()
        validator('owned_skill_knowledge_preview_response').validate(preview)
        self.assertEqual(self.planner.calls, 0)
        self.assertFalse((self.root / 'plans').exists())
        response = await self.request('start', self.start_arguments(preview))
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()['status'], 'pending')
        await self.planning.task
        self.assertEqual(self.planning.status()['status'], 'ready')
        checksum = self.planning.status()['bundle_sha256']
        response = await self.request('report', {'schema_version': '1.0', 'planning_bundle_sha256': checksum})
        self.assertEqual(response.status_code, 200, response.text)
        transport = response.json()
        validator('owned_skill_knowledge_report_transport').validate(transport)
        report = transport['report']
        validator('owned_skill_knowledge_report_response').validate(report)
        self.assertTrue(report['dispatch_recorded'])
        self.assertTrue(report['historical_binding_verified'])
        self.assertFalse(report['knowledge_applied'])
        self.assertFalse(report['model_request_verified'])
        self.assertFalse(report['real_model'])
        self.assertEqual(report['bundle']['model_request'], self.planner.last_request)
        replay = await self.request('start', self.start_arguments(preview))
        self.assertEqual(replay.status_code, 409)
        self.assertEqual(self.planner.calls, 1)

    async def test_canonical_start_preserves_float_pins_and_legacy_start_remains_available(self):
        self.planner.pins['temperature'] = 0.0
        self.planner.identity['deployment_id'] = 'fixture-' + digest(self.planner.pins)
        response = await self.request('preview', self.preview_arguments)
        self.assertEqual(response.status_code, 200, response.text)
        transport = response.json()
        self.assertIn('"temperature":0.0', transport['model_pins_canonical'])
        preview = transport['preview']
        request = {'schema_version': '1.1', 'preview_canonical': transport['preview_canonical'],
                   **{key: value for key, value in self.start_arguments(preview).items()
                      if key not in {'schema_version', 'preview'}}}
        for changes in ({'inference_consent': 1}, {'storage_consent': False},
                        {'preview_canonical': json.dumps(preview)}, {'extra': True}):
            denied = await self.request('start', request | changes)
            self.assertEqual(denied.status_code, 400, denied.text)
        response = await self.request('start', request)
        self.assertEqual(response.status_code, 202, response.text)
        await self.planning.task
        self.assertEqual(self.planning.status()['status'], 'ready')
        bundle = self.planning.load(self.planning.status()['bundle_sha256'])
        self.assertEqual(canonical(bundle['model_pins']), transport['model_pins_canonical'])
        self.assertIs(type(bundle['model_pins']['temperature']), float)

    async def test_authentication_host_origin_and_query_boundaries(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://testserver',
                                     headers={'Origin': 'http://testserver'}) as anonymous:
            response = await anonymous.post('/api/tasks/owned-skill-knowledge/preview', json=self.preview_arguments)
            self.assertEqual(response.status_code, 401)
        for headers in ({'Host': 'hostile.example'}, {'Origin': 'http://hostile.example'}):
            response = await self.client.post('/api/tasks/owned-skill-knowledge/preview',
                                              json=self.preview_arguments, headers=headers)
            self.assertEqual(response.status_code, 403)
        response = await self.client.post('/api/tasks/owned-skill-knowledge/preview?scope=extra',
                                          json=self.preview_arguments)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.planner.calls, 0)

    async def test_exact_json_duplicates_extra_fields_and_bounded_body(self):
        malformed = ['[]', 'null', '{}', '{', '{"schema_version":"1.0","schema_version":"1.0"}',
                     json.dumps(self.preview_arguments | {'extra': True}),
                     json.dumps(self.preview_arguments | {'schema_version': '2.0'})]
        for payload in malformed:
            with self.subTest(payload=payload):
                response = await self.client.post('/api/tasks/owned-skill-knowledge/preview', content=payload,
                                                  headers={'Content-Type': 'application/json'})
                self.assertEqual(response.status_code, 400)
        response = await self.client.post('/api/tasks/owned-skill-knowledge/preview', content=' ' * 131073,
                                          headers={'Content-Type': 'application/json'})
        self.assertEqual(response.status_code, 413)
        self.assertEqual((await self.request('unknown', self.preview_arguments)).status_code, 400)
        self.assertEqual(self.planner.calls, 0)
        self.assertFalse((self.root / 'plans').exists())

    async def test_separate_consents_learning_and_mutated_preview_rejected(self):
        preview = await self.preview()
        request = self.start_arguments(preview)
        for changes in ({'inference_consent': False}, {'storage_consent': False},
                        {'inference_consent': 1}, {'collect_learning': True},
                        {'confirm_sha256': '0' * 64}, {'lease_id': 'wrong'}):
            response = await self.request('start', request | changes)
            self.assertEqual(response.status_code, 400, response.text)
        changed = copy.deepcopy(preview)
        changed['goal'] = 'Save message "different"'
        response = await self.request('start', request | {'preview': changed})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.planner.calls, 0)

    async def test_current_source_or_control_drift_fail_with_sanitized_conflict(self):
        preview = await self.preview()
        self.scheduler.control['owner'] = 'HUMAN'
        response = await self.request('start', self.start_arguments(preview))
        self.assertEqual(response.status_code, 409)
        self.scheduler.control['owner'] = 'AGENT'
        review = self.store.review_preview(scope=SCOPE,
            document_sha256=self.scheduler.synthetic_document['document_sha256'], decision='revoke')
        self.store.review(preview=review, confirm_sha256=review['preview_sha256'])
        response = await self.request('start', self.start_arguments(preview))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {'detail': 'Planning context evidence, authority or confirmation unavailable'})
        self.assertNotIn(preview['context_text'], response.text)
        self.assertEqual(self.planner.calls, 0)

    async def test_unconfigured_and_missing_historical_bundle_return_conflicts(self):
        report = await self.request('report', {'schema_version': '1.0', 'planning_bundle_sha256': '0' * 64})
        self.assertEqual(report.status_code, 409)
        self.planning.knowledge = None
        response = await self.request('preview', self.preview_arguments)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {'detail': 'Planning context requires a configured selected-skill session'})
