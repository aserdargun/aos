import asyncio
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import httpx

from aos.contracts import REPO_ROOT, canonical, digest
from aos.desktop_console import create_console
from aos.desktop_tasks import DesktopScheduler
from aos.owned_learning_workspace import OwnedLearningWorkspace
from aos.owned_parameter_skill_review_requests import (
    OwnedParameterSkillReviewAcceptRequest, OwnedParameterSkillReviewPreviewRequest,
    OwnedParameterSkillReviewReadRequest, OwnedParameterSkillReviewRevokeRequest,
    OwnedParameterSkillReviewRecoveryPreviewRequest, OwnedParameterSkillReviewRecoverRequest)

import test_owned_parameter_skill_console as candidate_console


class OwnedParameterSkillReviewConsoleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        candidate_console.OwnedParameterSkillConsoleTests.setUp(self)
        self.scheduler.parameter_skill_review_session = lambda: self.service
        self.review = {'synthetic': True, 'fixture': 'mocked API envelope, not review evidence'}
        self.checksum = digest(self.review)
        for operation in ('preview', 'accept', 'read', 'revocation_preview', 'revoke',
                          'recovery_preview', 'recover'):
            getattr(self.service, operation).return_value = (self.review, self.checksum)
        self.endpoint = '/api/tasks/parameter-project-skill-review/'

    client = candidate_console.OwnedParameterSkillConsoleTests.client

    def write_request(self, operation):
        control = self.controller.state()
        common = {'schema_version': '1.0', 'lease_id': control['lease_id'],
                  'generation': control['generation']}
        if operation == 'accept':
            return common | {'candidate_sha256': 'a' * 64, 'confirm_review_sha256': self.checksum,
                             'human_confirmation': 'ACCEPT_MANUAL_REVIEW'}
        if operation == 'recover':
            return common | {'record_sha256': 'c' * 64, 'confirm_recovery_sha256': self.checksum,
                             'human_confirmation': 'RESTORE_ANCHORED_REVIEW_RECORD'}
        return common | {'review_sha256': 'b' * 64, 'confirm_revocation_sha256': self.checksum,
                         'human_confirmation': 'REVOKE_MANUAL_REVIEW'}

    async def test_authenticated_origin_and_explicit_write_envelopes(self):
        async with self.client() as client:
            value = {'schema_version': '1.0', 'candidate_sha256': 'a' * 64}
            self.assertEqual((await client.post(self.endpoint + 'preview', json=value)).status_code, 401)
            await client.post('/api/login', json={'token': 'synthetic-token'})
            self.assertEqual((await client.post(self.endpoint + 'preview', json=value,
                headers={'Origin': 'https://foreign.aos.invalid'})).status_code, 403)
            for operation, payload, key in (
                ('preview', value, 'review'), ('accept', self.write_request('accept'), 'review'),
                ('read', {'schema_version': '1.0', 'review_sha256': 'b' * 64}, 'status'),
                ('revoke-preview', {'schema_version': '1.0', 'review_sha256': 'b' * 64}, 'revocation'),
                ('revoke', self.write_request('revoke'), 'revocation'),
                ('recovery-preview', {'schema_version': '1.0', 'record_sha256': 'c' * 64}, 'recovery'),
                ('recover', self.write_request('recover'), 'recovery')):
                response = await client.post(self.endpoint + operation, json=payload)
                self.assertEqual(response.status_code, 200, response.text)
                report = response.json()
                self.assertEqual(report[key + '_canonical'], canonical(self.review))
                self.assertEqual(report[key + '_sha256'], digest(report[key]))
                for flag in ('native_model_verified', 'activation_authorized', 'execution_authorized',
                             'training_ready', 'gpu_release_verified'):
                    self.assertIs(report[flag], False)

    async def test_busy_stale_human_control_blocks_writes_not_readback(self):
        async with self.client() as client:
            await client.post('/api/login', json={'token': 'synthetic-token'})
            for operation in ('accept', 'revoke', 'recover'):
                payload = self.write_request(operation)
                for changed in ({'generation': True}, {'human_confirmation': False}, {'extra': True}):
                    self.assertEqual((await client.post(self.endpoint + operation,
                        json=payload | changed)).status_code, 400)
                self.assertEqual((await client.post(self.endpoint + operation,
                    json=payload | {'lease_id': 'foreign'})).status_code, 409)
                self.scheduler.reserved = True
                self.assertEqual((await client.post(self.endpoint + operation, json=payload)).status_code, 409)
                self.scheduler.reserved = False
            self.controller.control('take-control')
            for operation in ('accept', 'revoke', 'recover'):
                self.assertEqual((await client.post(self.endpoint + operation,
                    json=self.write_request(operation))).status_code, 409)
            self.assertEqual((await client.post(self.endpoint + 'read', json={
                'schema_version': '1.0', 'review_sha256': 'b' * 64})).status_code, 200)
            self.service.accept.assert_not_called()
            self.service.revoke.assert_not_called()
            self.service.recover.assert_not_called()

    async def test_closed_unconfigured_duplicate_and_uncertain_private_errors(self):
        async with self.client() as client:
            await client.post('/api/login', json={'token': 'synthetic-token'})
            payload = {'schema_version': '1.0', 'candidate_sha256': 'a' * 64}
            for path, content, expected in (
                ('preview?scope=foreign', '{}', 400), ('activate', '{}', 400),
                ('preview', ' ' * 4097, 413),
                ('preview', '{"schema_version":"1.0","schema_version":"1.0","candidate_sha256":"' + 'a' * 64 + '"}', 400)):
                self.assertEqual((await client.post(self.endpoint + path, content=content,
                    headers={'Content-Type': 'application/json'})).status_code, expected)
            self.scheduler.closed = True
            self.assertEqual((await client.post(self.endpoint + 'accept',
                json=self.write_request('accept'))).status_code, 409)
            self.scheduler.closed = False
            self.scheduler.owned_parameter_project_execution = None
            self.assertEqual((await client.post(self.endpoint + 'preview', json=payload)).status_code, 409)
            self.scheduler.owned_parameter_project_execution = object()
            for error in (ValueError('private fixture record must not be disclosed'),
                          sqlite3.DatabaseError('private fixture record: corrupt database')):
                self.service.preview.side_effect = error
                response = await client.post(self.endpoint + 'preview', json=payload)
                self.assertEqual(response.status_code, 409)
                self.assertNotIn('private fixture record', response.text)

    def test_canonical_request_schemas(self):
        for name, model in (('preview', OwnedParameterSkillReviewPreviewRequest),
                            ('accept', OwnedParameterSkillReviewAcceptRequest),
                            ('read', OwnedParameterSkillReviewReadRequest),
                            ('revoke', OwnedParameterSkillReviewRevokeRequest),
                            ('recovery_preview', OwnedParameterSkillReviewRecoveryPreviewRequest),
                            ('recover', OwnedParameterSkillReviewRecoverRequest)):
            expected = json.loads((REPO_ROOT / 'schemas' /
                ('owned_parameter_skill_review_' + name + '_request.schema.json')).read_text())
            self.assertEqual(expected, model.model_json_schema())

    async def test_actual_managed_evidence_candidate_review_revoke_without_task_replay(self):
        from test_owned_parameter_skill_candidate import create_accepted_bootstrap

        managers = []
        original = DesktopScheduler.configure_owned_parameter_project_execution

        def configure(manager, *arguments, **keywords):
            original(manager, *arguments, **keywords)
            managers.append(manager)

        def execute():
            cleanup = unittest.TestCase()
            try:
                runtime = tempfile.TemporaryDirectory(prefix='parameter-skill-review-api-', dir=REPO_ROOT / 'data')
                cleanup.addCleanup(runtime.cleanup)
                with patch.object(DesktopScheduler, 'configure_owned_parameter_project_execution', configure):
                    evidence = create_accepted_bootstrap(cleanup, runtime_directory=runtime.name)
                manager, = managers
                controller = manager.controller
                assets = evidence.root / 'review-api-assets'
                assets.mkdir()
                app = create_console(controller, 'synthetic-token', self.origin, assets, scheduler=manager)

                async def requests():
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                                 base_url=self.origin, headers={'Origin': self.origin}) as client:
                        await client.post('/api/login', json={'token': 'synthetic-token'})
                        candidate_endpoint = '/api/tasks/parameter-project-skill/'
                        candidate = await client.post(candidate_endpoint + 'preview', json={
                            'schema_version': '1.0', 'intent_sha256': evidence.intent_sha256})
                        self.assertEqual(candidate.status_code, 200, candidate.text)
                        candidate_sha = candidate.json()['candidate_sha256']
                        control = controller.state()
                        fence = {'lease_id': control['lease_id'], 'generation': control['generation']}
                        publication = await client.post(candidate_endpoint + 'publish', json={
                            'schema_version': '1.0', 'intent_sha256': evidence.intent_sha256,
                            'confirm_candidate_sha256': candidate_sha,
                            'human_confirmation': 'PUBLISH_MANUAL_CANDIDATE'} | fence)
                        self.assertEqual(publication.status_code, 200, publication.text)
                        response = await client.post(self.endpoint + 'preview', json={
                            'schema_version': '1.0', 'candidate_sha256': candidate_sha})
                        self.assertEqual(response.status_code, 200, response.text)
                        review_sha = response.json()['review_sha256']
                        accept = {'schema_version': '1.0', 'candidate_sha256': candidate_sha,
                                  'confirm_review_sha256': review_sha,
                                  'human_confirmation': 'ACCEPT_MANUAL_REVIEW'} | fence
                        response = await client.post(self.endpoint + 'accept', json=accept)
                        self.assertEqual(response.status_code, 200, response.text)
                        read = {'schema_version': '1.0', 'review_sha256': review_sha}
                        response = await client.post(self.endpoint + 'read', json=read)
                        self.assertEqual(response.status_code, 200, response.text)
                        self.assertEqual(response.json()['status']['status'], 'accepted')
                        response = await client.post(self.endpoint + 'revoke-preview', json=read)
                        self.assertEqual(response.status_code, 200, response.text)
                        revoke = read | {'confirm_revocation_sha256': response.json()['revocation_sha256'],
                                         'human_confirmation': 'REVOKE_MANUAL_REVIEW'} | fence
                        response = await client.post(self.endpoint + 'revoke', json=revoke)
                        self.assertEqual(response.status_code, 200, response.text)
                        response = await client.post(self.endpoint + 'read', json=read)
                        self.assertEqual(response.status_code, 200, response.text)
                        self.assertEqual(response.json()['status']['status'], 'revoked')
                        self.assertIs(response.json()['status']['reviewed'], False)
                        self.assertEqual((await client.post(self.endpoint + 'accept', json=accept)).status_code, 409)
                        review_store = manager.parameter_skill_review_session().store.directory
                        revocation_path, = review_store.glob('*.revoke.json')
                        revocation_path.unlink()
                        self.assertEqual((await client.post(self.endpoint + 'read', json=read)).status_code, 409)
                        self.assertEqual((await client.post(self.endpoint + 'accept', json=accept)).status_code, 409)
                        recovery_request = {'schema_version': '1.0', 'record_sha256': revocation_path.name[:64]}
                        recovery = await client.post(self.endpoint + 'recovery-preview', json=recovery_request)
                        self.assertEqual(recovery.status_code, 200, recovery.text)
                        self.assertEqual(recovery.json()['recovery']['record_stage'], 'revoke')
                        restore = recovery_request | {
                            'confirm_recovery_sha256': recovery.json()['recovery_sha256'],
                            'human_confirmation': 'RESTORE_ANCHORED_REVIEW_RECORD'} | fence
                        response = await client.post(self.endpoint + 'recover', json=restore)
                        self.assertEqual(response.status_code, 200, response.text)
                        response = await client.post(self.endpoint + 'read', json=read)
                        self.assertEqual(response.status_code, 200, response.text)
                        self.assertEqual(response.json()['status']['status'], 'revoked')
                        self.assertEqual((await client.post(self.endpoint + 'accept', json=accept)).status_code, 409)
                        self.assertEqual((await client.post(self.endpoint + 'recover', json=restore)).status_code, 409)
                        self.assertEqual(manager.store.connection.execute(
                            'SELECT count(*) FROM owned_parameter_skill_review_history').fetchone()[0], 2)
                        self.assertEqual(manager.store.connection.execute('SELECT COUNT(*) FROM actions').fetchone()[0], 7)
                        self.assertEqual(manager.store.connection.execute('SELECT COUNT(*) FROM model_calls').fetchone()[0], 0)

                startup = manager.owned_parameter_project_execution.loader.__self__
                with OwnedLearningWorkspace.acquire(evidence.directory, create=False) as source_lock:
                    startup.workspace_lock = source_lock
                    asyncio.run(requests())
            finally:
                cleanup.doCleanups()

        await asyncio.to_thread(execute)
