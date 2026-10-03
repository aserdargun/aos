import json
import unittest
from unittest.mock import AsyncMock

from aos import owned_parameter_skill_reuse_requests as models
from aos.contracts import REPO_ROOT

import test_owned_parameter_skill_console as candidate_console


class OwnedParameterSkillReuseConsoleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        candidate_console.OwnedParameterSkillConsoleTests.setUp(self)
        self.scheduler.parameter_skill_reuse_execution_session = lambda: self.service
        self.scheduler._parameter_skill_reuse_execution = self.service
        self.service.preview.return_value = {
            'schema_version': '1.0', 'admission': {'synthetic': True, 'mock_envelope_only': True},
            'confirm_sha256': 'a' * 64, 'execution_authorized': False}
        self.service.start.return_value = {
            'job_id': 'job-' + 'b' * 32, 'intent_sha256': 'c' * 64,
            'independently_verified': False, 'training_ready': False, 'gpu_release_verified': False}
        self.service.status.return_value = {'status': 'unresolved', 'independently_verified': False}
        self.service.read.return_value = self.service.status.return_value
        self.service.transition_blocked = False
        self.service.transitioning = False
        self.service.next_preview.return_value = self.service.preview.return_value
        self.service.next_start = AsyncMock(return_value=self.service.start.return_value)
        self.endpoint = '/api/tasks/parameter-project-skill-reuse/'

    client = candidate_console.OwnedParameterSkillConsoleTests.client

    def payload(self):
        state = self.controller.state()
        return {'schema_version': '1.0', 'release_sha256': 'd' * 64,
                'selection_sha256': 'e' * 64,
                'parameters': {'record-id': 'New record', 'note-text': 'New note'},
                'lease_id': state['lease_id'], 'generation': state['generation']}

    async def test_auth_origin_preview_start_and_readonly_status(self):
        async with self.client() as client:
            payload = self.payload()
            self.assertEqual((await client.post(self.endpoint + 'preview', json=payload)).status_code, 401)
            self.service.preview.assert_not_called()
            await client.post('/api/login', json={'token': 'synthetic-token'})
            self.assertEqual((await client.post(self.endpoint + 'preview', json=payload,
                headers={'Origin': 'https://foreign.aos.invalid'})).status_code, 403)
            response = await client.post(self.endpoint + 'preview', json=payload)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(response.json()['execution_authorized'])
            response = await client.post(self.endpoint + 'start', json=payload | {
                'confirm_sha256': 'a' * 64, 'human_confirmation': True})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(response.json()['independently_verified'])
            self.service.start.assert_called_once_with(
                self.service.preview.return_value['admission'], confirm_sha256='a' * 64,
                human_confirmation=True, lease_id=payload['lease_id'], generation=payload['generation'])
            before = self.service.preview.call_count
            response = await client.post(self.endpoint + 'status', json={'schema_version': '1.0'})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['status'], 'unresolved')
            self.assertFalse(response.json()['transition_blocked'])
            self.assertFalse(response.json()['transition_in_progress'])
            self.assertEqual(self.service.preview.call_count, before)

    async def test_explicit_next_pair_and_historical_read_do_not_reset_initial_start(self):
        async with self.client() as client:
            await client.post('/api/login', json={'token': 'synthetic-token'})
            payload = self.payload() | {'previous_intent_sha256': '1' * 64,
                                       'previous_receipt_sha256': '2' * 64}
            response = await client.post(self.endpoint + 'next-preview', json=payload)
            self.assertEqual(response.status_code, 200, response.text)
            self.service.next_preview.assert_called_once_with(**{
                key: value for key, value in payload.items() if key != 'schema_version'})
            response = await client.post(self.endpoint + 'next-start', json=payload | {
                'confirm_sha256': 'a' * 64, 'human_confirmation': True})
            self.assertEqual(response.status_code, 200, response.text)
            self.service.next_start.assert_awaited_once_with(
                self.service.preview.return_value['admission'],
                previous_intent_sha256='1' * 64, previous_receipt_sha256='2' * 64,
                confirm_sha256='a' * 64, human_confirmation=True,
                lease_id=payload['lease_id'], generation=payload['generation'])
            self.service.start.assert_not_called()
            before = self.service.next_preview.call_count
            response = await client.post(self.endpoint + 'read', json={
                'schema_version': '1.0', 'intent_sha256': '1' * 64})
            self.assertEqual(response.status_code, 200, response.text)
            self.service.read.assert_called_once_with('1' * 64)
            self.assertEqual(self.service.next_preview.call_count, before)
            for change in ({'previous_receipt_sha256': None}, {'previous_intent_sha256': 'invalid'},
                           {'human_confirmation': 1}, {'generation': True}):
                response = await client.post(self.endpoint + 'next-start', json=payload | {
                    'confirm_sha256': 'a' * 64, 'human_confirmation': True} | change)
                self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(self.service.next_start.await_count, 1)

    async def test_unknown_duplicate_unconfirmed_and_stale_failures_do_not_replay(self):
        async with self.client() as client:
            await client.post('/api/login', json={'token': 'synthetic-token'})
            payload = self.payload() | {'confirm_sha256': 'a' * 64, 'human_confirmation': True}
            for change in ({'human_confirmation': 1}, {'human_confirmation': False},
                           {'generation': True}, {'path': '/private'},
                           {'parameters': {'record-id': 42, 'note-text': 'New note'}}):
                response = await client.post(self.endpoint + 'start', json=payload | change)
                self.assertEqual(response.status_code, 422, response.text)
            self.service.start.assert_not_called()
            response = await client.post(self.endpoint + 'status', content='{"schema_version":"1.0","schema_version":"1.0"}',
                headers={'Content-Type': 'application/json'})
            self.assertEqual(response.status_code, 400)
            response = await client.post(self.endpoint + 'status?operation=start', json={'schema_version': '1.0'})
            self.assertEqual(response.status_code, 400)
            self.service.preview.side_effect = ValueError('/private/source/stale')
            response = await client.post(self.endpoint + 'start', json=payload)
            self.assertEqual(response.status_code, 409)
            self.assertNotIn('/private', response.text)
            self.service.start.assert_not_called()

    def test_canonical_request_schemas_match_models(self):
        for operation, model in models.REQUESTS.items():
            with self.subTest(operation=operation):
                path = REPO_ROOT / 'schemas' / ('owned_parameter_skill_reuse_' + operation.replace('-', '_') + '_request.schema.json')
                self.assertEqual(json.loads(path.read_text()), model.model_json_schema())
