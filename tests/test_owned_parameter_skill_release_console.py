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
from aos import owned_parameter_skill_release_requests as models

import test_owned_parameter_skill_console as candidate_console


class OwnedParameterSkillReleaseConsoleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        candidate_console.OwnedParameterSkillConsoleTests.setUp(self)
        self.scheduler.parameter_skill_release_session = lambda: self.service
        self.record = {'synthetic': True, 'fixture': 'mocked API envelope, not release evidence'}
        self.checksum = digest(self.record)
        for operation in ('preview', 'release', 'read', 'selection_preview', 'select', 'recovery_preview', 'recover'):
            getattr(self.service, operation).return_value = (self.record, self.checksum)
        self.service.inventory.return_value = {'families': [], 'source_current_verified': False}
        self.endpoint = '/api/tasks/parameter-project-skill-release/'

    client = candidate_console.OwnedParameterSkillConsoleTests.client

    def payload(self, operation):
        value = {'schema_version': '1.0'}
        if operation in {'preview', 'release'}:
            value |= {'review_sha256': 'a' * 64, 'expected_parent_release_sha256': None}
        elif operation in {'recovery-preview', 'recover'}:
            value |= {'record_sha256': 'b' * 64}
        elif operation != 'inventory':
            value |= {'release_sha256': 'b' * 64}
        if operation in {'select-preview', 'select', 'rollback-preview', 'rollback'}:
            value |= {'expected_selection_sha256': None}
        if operation in {'release', 'select', 'rollback', 'recover'}:
            control = self.controller.state()
            value |= {'lease_id': control['lease_id'], 'generation': control['generation']}
            key = ('confirm_release_sha256' if operation == 'release' else
                   'confirm_recovery_sha256' if operation == 'recover' else 'confirm_selection_sha256')
            value[key] = self.checksum
            value['human_confirmation'] = {'release': 'RELEASE_MANUAL_SKILL',
                'select': 'SELECT_MANUAL_SKILL', 'rollback': 'ROLLBACK_MANUAL_SKILL',
                'recover': 'RESTORE_ANCHORED_RELEASE_RECORD'}[operation]
        return value

    async def test_auth_origin_exact_envelopes_and_distinct_selection_operations(self):
        async with self.client() as client:
            self.assertEqual((await client.post(self.endpoint + 'preview', json=self.payload('preview'))).status_code, 401)
            await client.post('/api/login', json={'token': 'synthetic-token'})
            self.assertEqual((await client.post(self.endpoint + 'preview', json=self.payload('preview'),
                headers={'Origin': 'https://foreign.aos.invalid'})).status_code, 403)
            for operation in ('preview', 'release', 'read', 'inventory', 'select-preview', 'select',
                              'rollback-preview', 'rollback', 'recovery-preview', 'recover'):
                response = await client.post(self.endpoint + operation, json=self.payload(operation))
                self.assertEqual(response.status_code, 200, response.text)
                report = response.json()
                if operation != 'inventory':
                    key = ('release' if operation in {'preview', 'release', 'read'} else
                           'recovery' if operation in {'recovery-preview', 'recover'} else 'selection')
                    self.assertEqual(report[key + '_canonical'], canonical(self.record))
                    self.assertEqual(report[key + '_sha256'], digest(report[key]))
                for flag in ('native_model_verified', 'activation_authorized', 'execution_authorized',
                             'training_ready', 'gpu_release_verified'):
                    self.assertIs(report[flag], False)
            self.assertEqual([call.kwargs['operation'] for call in self.service.select.call_args_list],
                             ['select', 'rollback'])

    async def test_stale_busy_human_and_invalid_fences_deny_all_writes(self):
        async with self.client() as client:
            await client.post('/api/login', json={'token': 'synthetic-token'})
            for operation in ('release', 'select', 'rollback', 'recover'):
                payload = self.payload(operation)
                for changed in ({'generation': True}, {'human_confirmation': False}, {'extra': True}):
                    self.assertEqual((await client.post(self.endpoint + operation,
                        json=payload | changed)).status_code, 400)
                self.assertEqual((await client.post(self.endpoint + operation,
                    json=payload | {'lease_id': 'foreign'})).status_code, 409)
                self.scheduler.reserved = True
                self.assertEqual((await client.post(self.endpoint + operation, json=payload)).status_code, 409)
                self.scheduler.reserved = False
                for attribute in ('closed', 'restart_quiesced'):
                    setattr(self.scheduler, attribute, True)
                    self.assertEqual((await client.post(self.endpoint + operation, json=payload)).status_code, 409)
                    setattr(self.scheduler, attribute, False)
                self.assertEqual((await client.post(self.endpoint + operation,
                    json=payload | {'generation': payload['generation'] + 1})).status_code, 409)
            self.controller.control('take-control')
            for operation in ('release', 'select', 'rollback', 'recover'):
                self.assertEqual((await client.post(self.endpoint + operation,
                    json=self.payload(operation))).status_code, 409)
            self.assertEqual((await client.post(self.endpoint + 'read', json=self.payload('read'))).status_code, 200)
            self.service.release.assert_not_called()
            self.service.select.assert_not_called()
            self.service.recover.assert_not_called()

    async def test_duplicate_missing_heads_unknown_query_and_private_sql_errors_fail_closed(self):
        async with self.client() as client:
            await client.post('/api/login', json={'token': 'synthetic-token'})
            for path, content, expected in (
                ('activate', '{}', 400), ('inventory?source=foreign', '{}', 400),
                ('preview', ' ' * 4097, 413),
                ('preview', '{"schema_version":"1.0","schema_version":"1.0"}', 400),
                ('preview', canonical({'schema_version': '1.0', 'review_sha256': 'a' * 64}), 400)):
                self.assertEqual((await client.post(self.endpoint + path, content=content,
                    headers={'Content-Type': 'application/json'})).status_code, expected)
            self.scheduler.closed = True
            self.assertEqual((await client.post(self.endpoint + 'release', json=self.payload('release'))).status_code, 409)
            self.scheduler.closed = False
            self.scheduler.owned_parameter_project_execution = None
            self.assertEqual((await client.post(self.endpoint + 'preview', json=self.payload('preview'))).status_code, 409)
            self.scheduler.owned_parameter_project_execution = object()
            self.service.preview.side_effect = sqlite3.DatabaseError('private source database location')
            response = await client.post(self.endpoint + 'preview', json=self.payload('preview'))
            self.assertEqual(response.status_code, 409)
            self.assertNotIn('private source', response.text)

    def test_canonical_request_schemas(self):
        for name, model in (('preview', models.OwnedParameterSkillReleasePreviewRequest),
                            ('release', models.OwnedParameterSkillReleaseRequest),
                            ('read', models.OwnedParameterSkillReleaseReadRequest),
                            ('inventory', models.OwnedParameterSkillReleaseInventoryRequest),
                            ('selection_preview', models.OwnedParameterSkillSelectionPreviewRequest),
                            ('select', models.OwnedParameterSkillSelectRequest),
                            ('rollback', models.OwnedParameterSkillRollbackRequest),
                            ('recovery_preview', models.OwnedParameterSkillReleaseRecoveryPreviewRequest),
                            ('recover', models.OwnedParameterSkillReleaseRecoverRequest)):
            schema = json.loads((REPO_ROOT / 'schemas' /
                ('owned_parameter_skill_release_' + name + '_request.schema.json')).read_text())
            self.assertEqual(schema, model.model_json_schema())

    async def test_actual_bootstrap_review_release_selection_and_revoked_denial_no_job_replay(self):
        from test_owned_parameter_skill_candidate import create_accepted_bootstrap

        managers = []
        original = DesktopScheduler.configure_owned_parameter_project_execution

        def configure(manager, *arguments, **keywords):
            original(manager, *arguments, **keywords)
            managers.append(manager)

        def execute():
            cleanup = unittest.TestCase()
            try:
                runtime = tempfile.TemporaryDirectory(prefix='parameter-release-api-', dir=REPO_ROOT / 'data')
                cleanup.addCleanup(runtime.cleanup)
                with patch.object(DesktopScheduler, 'configure_owned_parameter_project_execution', configure):
                    evidence = create_accepted_bootstrap(cleanup, runtime_directory=runtime.name)
                manager, = managers
                controller = manager.controller
                startup = manager.owned_parameter_project_execution.loader.__self__
                with OwnedLearningWorkspace.acquire(evidence.directory, create=False) as source_lock:
                    startup.workspace_lock = source_lock
                    candidates = manager.parameter_skill_candidate_session()
                    _, candidate_sha = candidates.preview(evidence.intent_sha256)
                    candidates.publish(evidence.intent_sha256, candidate_sha, 'PUBLISH_MANUAL_CANDIDATE')
                    reviews = manager.parameter_skill_review_session()
                    _, review_sha = reviews.preview(candidate_sha)
                    reviews.accept(candidate_sha, review_sha, 'ACCEPT_MANUAL_REVIEW')
                    assets = evidence.root / 'release-api-assets'
                    assets.mkdir()
                    app = create_console(controller, 'synthetic-token', self.origin, assets, scheduler=manager)

                    async def requests():
                        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                                     base_url=self.origin, headers={'Origin': self.origin}) as client:
                            await client.post('/api/login', json={'token': 'synthetic-token'})
                            control = controller.state()
                            fence = {'lease_id': control['lease_id'], 'generation': control['generation']}
                            preview = {'schema_version': '1.0', 'review_sha256': review_sha,
                                       'expected_parent_release_sha256': None}
                            response = await client.post(self.endpoint + 'preview', json=preview)
                            self.assertEqual(response.status_code, 200, response.text)
                            release_sha = response.json()['release_sha256']
                            response = await client.post(self.endpoint + 'release', json=preview | fence | {
                                'confirm_release_sha256': release_sha, 'human_confirmation': 'RELEASE_MANUAL_SKILL'})
                            self.assertEqual(response.status_code, 200, response.text)
                            self.assertIs(response.json()['release']['execution_authorized'], False)
                            selection = {'schema_version': '1.0', 'release_sha256': release_sha,
                                         'expected_selection_sha256': None}
                            response = await client.post(self.endpoint + 'select-preview', json=selection)
                            self.assertEqual(response.status_code, 200, response.text)
                            response = await client.post(self.endpoint + 'select', json=selection | fence | {
                                'confirm_selection_sha256': response.json()['selection_sha256'],
                                'human_confirmation': 'SELECT_MANUAL_SKILL'})
                            self.assertEqual(response.status_code, 200, response.text)
                            selection_sha = response.json()['selection_sha256']
                            releases = manager.parameter_skill_release_session()
                            anchors_before = tuple(manager.store.connection.execute(
                                'SELECT * FROM owned_parameter_skill_release_history').fetchall())
                            recoveries = {}
                            for stage, record_sha in (('release', release_sha), ('selection', selection_sha)):
                                path = releases.store.directory / (record_sha + '.' + stage + '.json')
                                original_bytes = path.read_bytes()
                                path.unlink()
                                recovery_preview = {'schema_version': '1.0', 'record_sha256': record_sha}
                                response = await client.post(self.endpoint + 'recovery-preview', json=recovery_preview)
                                self.assertEqual(response.status_code, 200, response.text)
                                proposal = response.json()
                                self.assertEqual(proposal['recovery']['record_stage'], stage)
                                self.assertEqual(proposal['recovery_canonical'], canonical(proposal['recovery']))
                                self.assertEqual(proposal['recovery_sha256'], digest(proposal['recovery']))
                                self.assertFalse(path.exists())
                                restore = recovery_preview | fence | {
                                    'confirm_recovery_sha256': proposal['recovery_sha256'],
                                    'human_confirmation': 'RESTORE_ANCHORED_RELEASE_RECORD'}
                                recoveries[stage] = restore
                                for changes in ({'confirm_recovery_sha256': 'f' * 64},
                                                {'record_sha256': 'e' * 64},
                                                {'generation': fence['generation'] + 1}):
                                    response = await client.post(self.endpoint + 'recover', json=restore | changes)
                                    self.assertEqual(response.status_code, 409, response.text)
                                    self.assertFalse(path.exists())
                                response = await client.post(self.endpoint + 'recover', json=restore,
                                    headers={'Host': 'foreign.aos.invalid'})
                                self.assertEqual(response.status_code, 403)
                                self.assertFalse(path.exists())
                                duplicate = canonical(restore).replace('"record_sha256":',
                                    '"record_sha256":"' + record_sha + '","record_sha256":')
                                response = await client.post(self.endpoint + 'recover', content=duplicate,
                                    headers={'Content-Type': 'application/json'})
                                self.assertEqual(response.status_code, 400)
                                self.assertFalse(path.exists())
                                response = await client.post(self.endpoint + 'recover', json=restore)
                                self.assertEqual(response.status_code, 200, response.text)
                                self.assertEqual(response.json(), proposal)
                                self.assertEqual(path.read_bytes(), original_bytes)
                                self.assertEqual(tuple(manager.store.connection.execute(
                                    'SELECT * FROM owned_parameter_skill_release_history').fetchall()), anchors_before)
                                response = await client.post(self.endpoint + 'recover', json=restore)
                                self.assertEqual(response.status_code, 409)
                                self.assertEqual(path.read_bytes(), original_bytes)
                            response = await client.post(self.endpoint + 'inventory', json={'schema_version': '1.0'})
                            self.assertEqual(response.status_code, 200, response.text)
                            self.assertEqual(response.json()['inventory']['families'][0]['selected_release_sha256'], release_sha)
                            revocation, revocation_sha = reviews.revocation_preview(review_sha)
                            self.assertEqual(revocation['review_sha256'], review_sha)
                            reviews.revoke(review_sha, revocation_sha, 'REVOKE_MANUAL_REVIEW')
                            release_path = releases.store.directory / (release_sha + '.release.json')
                            release_path.unlink()
                            response = await client.post(self.endpoint + 'recover', json=recoveries['release'])
                            self.assertEqual(response.status_code, 409)
                            self.assertFalse(release_path.exists())
                            response = await client.post(self.endpoint + 'recovery-preview', json={
                                'schema_version': '1.0', 'record_sha256': release_sha})
                            self.assertEqual(response.status_code, 409)
                            response = await client.post(self.endpoint + 'read', json={
                                'schema_version': '1.0', 'release_sha256': release_sha})
                            self.assertEqual(response.status_code, 409)
                            self.assertEqual(manager.store.connection.execute('SELECT COUNT(*) FROM actions').fetchone()[0], 7)
                            self.assertEqual(manager.store.connection.execute('SELECT COUNT(*) FROM model_calls').fetchone()[0], 0)

                    asyncio.run(requests())
            finally:
                cleanup.doCleanups()

        await asyncio.to_thread(execute)
