import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx

from aos.contracts import REPO_ROOT, canonical, digest, identifier
from aos.desktop_console import create_console
from aos.desktop_control import DesktopController
from aos.desktop_tasks import DesktopScheduler
from aos.owned_learning_workspace import OwnedLearningWorkspace
from aos.owned_parameter_skill_requests import (
    OwnedParameterSkillPreviewRequest, OwnedParameterSkillPublishRequest,
    OwnedParameterSkillReadRequest)
from aos.storage import TrajectoryStore


class OwnedParameterSkillConsoleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = TrajectoryStore(self.root / 'trajectory.sqlite')
        self.addCleanup(self.store.close)
        runtime = SimpleNamespace(runtime_id=identifier('desktop'), pins={'image_id': 'synthetic-image'})
        self.controller = DesktopController(self.store, runtime)
        self.candidate = {'synthetic': True, 'fixture': 'mocked API response, not provenance evidence'}
        self.checksum = digest(self.candidate)
        self.service = Mock()
        for operation in ('preview', 'publish', 'read'):
            getattr(self.service, operation).return_value = (self.candidate, self.checksum)
        self.scheduler = SimpleNamespace(
            owned_parameter_project_execution=object(), reserved=False, closed=False,
            restart_quiesced=False, parameter_skill_candidate_session=lambda: self.service)
        self.origin = 'http://127.0.0.1:19483'
        self.root.joinpath('assets').mkdir()
        self.app = create_console(self.controller, 'synthetic-token', self.origin,
                                  self.root / 'assets', scheduler=self.scheduler)

    def client(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                 base_url=self.origin, headers={'Origin': self.origin})

    def publication(self):
        control = self.controller.state()
        return {'schema_version': '1.0', 'intent_sha256': 'a' * 64,
                'confirm_candidate_sha256': self.checksum,
                'human_confirmation': 'PUBLISH_MANUAL_CANDIDATE',
                'lease_id': control['lease_id'], 'generation': control['generation']}

    async def test_auth_origin_and_explicit_canonical_publication_without_runtime_effects(self):
        async with self.client() as client:
            endpoint = '/api/tasks/parameter-project-skill/'
            value = {'schema_version': '1.0', 'intent_sha256': 'a' * 64}
            self.assertEqual((await client.post(endpoint + 'preview', json=value)).status_code, 401)
            await client.post('/api/login', json={'token': 'synthetic-token'})
            self.assertEqual((await client.post(endpoint + 'preview', json=value,
                headers={'Origin': 'https://foreign.aos.invalid'})).status_code, 403)
            response = await client.post(endpoint + 'preview', json=value)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['candidate_canonical'], canonical(self.candidate))
            self.assertEqual(response.json()['candidate_sha256'], self.checksum)
            self.assertFalse(response.json()['native_model_verified'])
            self.service.publish.assert_not_called()
            response = await client.post(endpoint + 'publish', json=self.publication())
            self.assertEqual(response.status_code, 200)
            self.service.publish.assert_called_once_with('a' * 64,
                confirm_candidate_sha256=self.checksum, human_confirmation='PUBLISH_MANUAL_CANDIDATE')
            self.assertFalse(response.json()['activation_authorized'])

    async def test_stale_control_busy_and_wrong_owner_reject_publication_but_allow_read(self):
        async with self.client() as client:
            await client.post('/api/login', json={'token': 'synthetic-token'})
            endpoint = '/api/tasks/parameter-project-skill/'
            for changes in ({'generation': True}, {'generation': -1}, {'human_confirmation': True}):
                self.assertEqual((await client.post(endpoint + 'publish',
                    json=self.publication() | changes)).status_code, 400)
            self.assertEqual((await client.post(endpoint + 'publish',
                json=self.publication() | {'lease_id': 'foreign'})).status_code, 409)
            self.scheduler.reserved = True
            self.assertEqual((await client.post(endpoint + 'publish', json=self.publication())).status_code, 409)
            self.scheduler.reserved = False
            self.controller.control('take-control')
            self.assertEqual((await client.post(endpoint + 'publish', json=self.publication())).status_code, 409)
            response = await client.post(endpoint + 'read', json={
                'schema_version': '1.0', 'candidate_sha256': self.checksum})
            self.assertEqual(response.status_code, 200)
            self.service.publish.assert_not_called()

    async def test_duplicate_oversize_extra_query_and_unknown_operation_are_rejected(self):
        async with self.client() as client:
            await client.post('/api/login', json={'token': 'synthetic-token'})
            endpoint = '/api/tasks/parameter-project-skill/'
            for path, content, expected in (
                ('preview', '{"schema_version":"1.0","schema_version":"1.0","intent_sha256":"' + 'a' * 64 + '"}', 400),
                ('preview', ' ' * 4097, 413),
                ('preview', canonical({'schema_version': '1.0', 'intent_sha256': 'a' * 64, 'native_model_verified': True}), 400),
                ('preview?source=foreign', '{}', 400), ('activate', '{}', 400)):
                self.assertEqual((await client.post(endpoint + path, content=content,
                    headers={'Content-Type': 'application/json'})).status_code, expected)
            self.service.preview.assert_not_called()
            self.service.publish.assert_not_called()

    async def test_unconfigured_host_and_private_errors_do_not_authorize_or_disclose(self):
        async with self.client() as client:
            await client.post('/api/login', json={'token': 'synthetic-token'})
            value = {'schema_version': '1.0', 'intent_sha256': 'a' * 64}
            self.scheduler.owned_parameter_project_execution = None
            response = await client.post('/api/tasks/parameter-project-skill/preview', json=value)
            self.assertEqual(response.status_code, 409)
            self.scheduler.owned_parameter_project_execution = object()
            self.service.preview.side_effect = ValueError('private fixture parameter must not escape')
            response = await client.post('/api/tasks/parameter-project-skill/preview', json=value)
            self.assertEqual(response.status_code, 409)
            self.assertNotIn('private fixture parameter', response.text)

    def test_request_schemas_are_canonical(self):
        for name, model in (('preview', OwnedParameterSkillPreviewRequest),
                            ('publish', OwnedParameterSkillPublishRequest),
                            ('read', OwnedParameterSkillReadRequest)):
            expected = json.loads((REPO_ROOT / 'schemas' /
                ('owned_parameter_skill_' + name + '_request.schema.json')).read_text())
            self.assertEqual(expected, model.model_json_schema())

    async def test_actual_bootstrap_candidate_api_reaudits_without_runtime_replay(self):
        from test_owned_parameter_skill_candidate import create_accepted_bootstrap

        managers = []
        original = DesktopScheduler.configure_owned_parameter_project_execution

        def configure(manager, *arguments, **keywords):
            original(manager, *arguments, **keywords)
            managers.append(manager)

        def execute():
            cleanup = unittest.TestCase()
            try:
                runtime = tempfile.TemporaryDirectory(prefix='parameter-skill-api-', dir=REPO_ROOT / 'data')
                cleanup.addCleanup(runtime.cleanup)
                with patch.object(DesktopScheduler, 'configure_owned_parameter_project_execution', configure):
                    evidence = create_accepted_bootstrap(cleanup, runtime_directory=runtime.name)
                manager, = managers
                controller = manager.controller
                assets = evidence.root / 'api-assets'
                assets.mkdir()
                app = create_console(controller, 'synthetic-token', self.origin, assets, scheduler=manager)

                async def requests():
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                                 base_url=self.origin, headers={'Origin': self.origin}) as client:
                        await client.post('/api/login', json={'token': 'synthetic-token'})
                        endpoint = '/api/tasks/parameter-project-skill/'
                        response = await client.post(endpoint + 'preview', json={
                            'schema_version': '1.0', 'intent_sha256': evidence.intent_sha256})
                        self.assertEqual(response.status_code, 200, response.text)
                        checksum = response.json()['candidate_sha256']
                        self.assertEqual(response.json()['candidate']['kind'], 'audited_manual_bootstrap_candidate')
                        self.assertFalse(response.json()['candidate']['native_model_verified'])
                        control = controller.state()
                        response = await client.post(endpoint + 'publish', json={
                            'schema_version': '1.0', 'intent_sha256': evidence.intent_sha256,
                            'confirm_candidate_sha256': checksum, 'human_confirmation': 'PUBLISH_MANUAL_CANDIDATE',
                            'lease_id': control['lease_id'], 'generation': control['generation']})
                        self.assertEqual(response.status_code, 200, response.text)
                        response = await client.post(endpoint + 'read', json={
                            'schema_version': '1.0', 'candidate_sha256': checksum})
                        self.assertEqual(response.status_code, 200, response.text)
                        self.assertEqual(response.json()['candidate_sha256'], checksum)
                        self.assertEqual(manager.store.connection.execute('SELECT COUNT(*) FROM actions').fetchone()[0], 7)
                        self.assertEqual(manager.store.connection.execute('SELECT COUNT(*) FROM model_calls').fetchone()[0], 0)

                startup = manager.owned_parameter_project_execution.loader.__self__
                with OwnedLearningWorkspace.acquire(evidence.directory, create=False) as source_lock:
                    startup.workspace_lock = source_lock
                    asyncio.run(requests())
            finally:
                cleanup.doCleanups()

        await asyncio.to_thread(execute)
