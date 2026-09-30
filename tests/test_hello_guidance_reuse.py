import copy
import json
import unittest
from unittest.mock import patch

import jsonschema

from aos.contracts import AOSFault, HELLO_CONTENT, REPO_ROOT, digest, identifier
from aos.hello_guidance_reuse import (ADMISSION, HelloGuidanceEntry, HelloReusePublicationPreview,
    HelloReuseEntryResponse, HelloReusePreview, HelloReuseStart, HelloReuseReport)
import test_failure_guidance as guidance_fixtures


class HelloGuidanceReuseTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = guidance_fixtures.FailureGuidanceTests.asyncSetUp
    asyncTearDown = guidance_fixtures.FailureGuidanceTests.asyncTearDown
    authority = guidance_fixtures.FailureGuidanceTests.authority
    approval = guidance_fixtures.FailureGuidanceTests.approval
    client = guidance_fixtures.FailureGuidanceTests.client
    source = guidance_fixtures.FailureGuidanceTests.source
    request = guidance_fixtures.FailureGuidanceTests.request
    complete = guidance_fixtures.FailureGuidanceTests.complete

    def revoke(self, selection):
        self.scheduler.failure_improvements.revoke(selection['source_job_id'], selection['candidate_sha256'],
                                                   selection['receipt_sha256'])

    async def published(self, code='refresh_observation'):
        selection = await self.source(code)
        service = self.scheduler.hello_guidance_reuse
        preview = service.publish_preview(**selection)
        saved = self.scheduler.publish_hello_guidance_reuse(**self.request(preview))
        return selection, preview, saved

    async def test_explicit_publication_is_immutable_without_execution_authority(self):
        selection = await self.source()
        service = self.scheduler.hello_guidance_reuse
        preview = service.publish_preview(**selection)
        self.assertFalse(service.directory.exists())
        for consent in (False, None, 1):
            with self.assertRaises(ValueError):
                self.scheduler.publish_hello_guidance_reuse(**(self.request(preview) | {'consent': consent}))
        self.assertFalse(service.directory.exists())
        saved = self.scheduler.publish_hello_guidance_reuse(**self.request(preview))
        self.assertTrue(saved['saved'] and saved['current_valid'])
        self.assertFalse(saved['execution_authorized'])
        entry_path = service.directory / ('entry-' + saved['entry_sha256'] + '.json')
        metadata = entry_path.stat()
        self.assertEqual(saved, self.scheduler.publish_hello_guidance_reuse(**self.request(preview)))
        self.assertEqual(metadata.st_ino, entry_path.stat().st_ino)
        self.assertEqual(len(self.scheduler.status()['jobs']), 1)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)

    async def test_two_fresh_runs_require_separate_consent_and_manual_approval(self):
        selection, unused_publication, saved = await self.published('inspect_before_retry')
        service = self.scheduler.hello_guidance_reuse
        runs = []
        for attempt in range(2):
            preview = service.reuse_preview(saved['entry_sha256'])
            with self.assertRaises(ValueError):
                self.scheduler.start_hello_guidance_reuse(**(self.request(preview) | {'consent': False}))
            started = self.scheduler.start_hello_guidance_reuse(**self.request(preview))
            runs.append(started)
            await self.complete(started)
            report = service.report(started['job_id'])
            self.assertTrue(report['entry_binding_verified'] and report['current_entry_valid'])
            self.assertTrue(report['guidance']['context_binding_verified'])
            self.assertFalse(report['guidance']['model_request_verified'])
            self.assertFalse(report['guidance']['guidance_applied'])
            self.assertEqual(report['guidance']['followup']['outcome']['status'], 'verified')
            self.assertEqual(report['guidance']['followup']['source_job_id'], selection['source_job_id'])
            with self.assertRaises((ValueError, OSError)):
                self.scheduler.start_hello_guidance_reuse(**self.request(preview))
        self.assertNotEqual(runs[0]['job_id'], runs[1]['job_id'])
        self.assertNotEqual(runs[0]['reuse_intent_sha256'], runs[1]['reuse_intent_sha256'])
        self.assertEqual((self.runtime.root / 'hello.txt').read_text(), HELLO_CONTENT)
        source = self.store.connection.execute('SELECT status,run_id FROM desktop_tasks WHERE job_id=?',
                                               (selection['source_job_id'],)).fetchone()
        self.assertEqual(source['status'], 'failed')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions WHERE run_id=?',
                                                       (source['run_id'],)).fetchone()[0], 0)

    async def test_revoked_source_blocks_publication_and_reuse_but_retains_history(self):
        selection, publication, saved = await self.published()
        service = self.scheduler.hello_guidance_reuse
        started = self.scheduler.start_hello_guidance_reuse(**self.request(service.reuse_preview(saved['entry_sha256'])))
        await self.complete(started)
        self.revoke(selection)
        self.assertFalse(service.inspect(saved['entry_sha256'])['current_valid'])
        for operation in (lambda: service.reuse_preview(saved['entry_sha256']),
                          lambda: self.scheduler.publish_hello_guidance_reuse(**self.request(publication))):
            with self.assertRaises(ValueError):
                operation()
        report = service.report(started['job_id'])
        self.assertTrue(report['entry_binding_verified'])
        self.assertFalse(report['current_entry_valid'])
        self.assertEqual(report['guidance']['followup']['outcome']['status'], 'verified')

    async def test_revocation_before_publication_leaves_no_entry(self):
        selection = await self.source()
        service = self.scheduler.hello_guidance_reuse
        preview = service.publish_preview(**selection)
        self.revoke(selection)
        with self.assertRaises(ValueError):
            self.scheduler.publish_hello_guidance_reuse(**self.request(preview))
        self.assertFalse(service.directory.exists())

    async def test_foreign_role_scope_context_and_configuration_rejected(self):
        selection = await self.source()
        service = self.scheduler.hello_guidance_reuse
        original = service.publish_preview(**selection)
        for path, value in [('scope.session_id', 'session-foreign'), ('scope.workspace_sha256', 'd'*64),
                            ('scope.configuration_sha256', 'e'*64), ('scope.role', 'system2'),
                            ('scope.parent_runtime_id', 'runtime-foreign'), ('scope.image_id', 'foreign-image'),
                            ('scope.system1_deployment_id', 'deployment-foreign'), ('context_sha256', 'f'*64),
                            ('source.source_sha256', 'd'*64), ('context_version', 'unimplemented'),
                            ('guidance_code', 'repair_environment')]:
            preview = copy.deepcopy(original)
            target = preview['entry']
            parts = path.split('.')
            for component in parts[:-1]:
                target = target[component]
            target[parts[-1]] = value
            preview['entry_sha256'] = digest(preview['entry'])
            preview['confirm_sha256'] = digest({key: value for key, value in preview.items() if key != 'confirm_sha256'})
            with self.assertRaises(ValueError):
                self.scheduler.publish_hello_guidance_reuse(**self.request(preview))
        self.assertFalse(service.directory.exists())

    async def test_replaced_workspace_or_symlink_invalidates_entry(self):
        unused_selection, unused_publication, saved = await self.published()
        service = self.scheduler.hello_guidance_reuse
        original = self.runtime.root
        backup = original.with_name(original.name + '-pinned')
        original.rename(backup)
        try:
            original.mkdir()
            self.assertFalse(service.inspect(saved['entry_sha256'])['current_valid'])
            original.rmdir()
            original.symlink_to(backup, target_is_directory=True)
            self.assertFalse(service.inspect(saved['entry_sha256'])['current_valid'])
        finally:
            if original.is_symlink():
                original.unlink()
            elif original.exists():
                original.rmdir()
            backup.rename(original)
        self.assertTrue(service.inspect(saved['entry_sha256'])['current_valid'])

    async def test_configuration_and_deployment_changes_invalidate_saved_scope(self):
        unused_selection, unused_publication, saved = await self.published()
        service = self.scheduler.hello_guidance_reuse
        preview = service.reuse_preview(saved['entry_sha256'])
        target = self.scheduler.failure_followup_target('hello')
        for change in ({'configuration_sha256': 'e'*64}, {'system1_deployment_id': 'deployment-foreign'}):
            with patch.object(self.scheduler, 'failure_followup_target', return_value=target | change):
                self.assertFalse(service.inspect(saved['entry_sha256'])['current_valid'])
                with self.assertRaises(ValueError):
                    service.reuse_preview(saved['entry_sha256'])
                with self.assertRaises(ValueError):
                    self.scheduler.start_hello_guidance_reuse(**self.request(preview))
        self.assertEqual(len(self.scheduler.status()['jobs']), 1)
        self.assertFalse((self.runtime.root / 'hello.txt').exists())

    async def test_revocation_during_new_approval_blocks_effect(self):
        selection, unused_publication, saved = await self.published()
        service = self.scheduler.hello_guidance_reuse
        started = self.scheduler.start_hello_guidance_reuse(**self.request(service.reuse_preview(saved['entry_sha256'])))
        approval = await self.approval()
        self.revoke(selection)
        self.scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
        await self.scheduler.task
        self.assertFalse((self.runtime.root / 'hello.txt').exists())
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)
        self.assertFalse(service.report(started['job_id'])['current_entry_valid'])

    async def test_failed_admission_rolls_back_job_and_binding(self):
        unused_selection, unused_publication, saved = await self.published()
        service = self.scheduler.hello_guidance_reuse
        preview = service.reuse_preview(saved['entry_sha256'])
        jobs = len(self.scheduler.status()['jobs'])
        with patch.object(service, 'admit', side_effect=ValueError('synthetic_admission_rejected')):
            with self.assertRaises(ValueError):
                self.scheduler.start_hello_guidance_reuse(**self.request(preview))
        self.assertEqual(len(self.scheduler.status()['jobs']), jobs)
        self.assertFalse(self.scheduler._hello_guidance_reuse_jobs)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM desktop_events WHERE kind=?',
                                                       (ADMISSION,)).fetchone()[0], 0)

    async def test_internal_start_cannot_bypass_consent_and_guidance_admission(self):
        with self.assertRaises(AOSFault):
            self.scheduler._start(**self.authority(), kind='hello', hello_guidance_reuse='f'*64)
        self.assertFalse(self.scheduler.status()['jobs'])
        self.assertFalse(self.scheduler._hello_guidance_reuse_jobs)

    async def test_tampered_entry_before_decision_blocks_engine(self):
        from aos.failure_guidance import FailureGuidanceContext

        unused_selection, unused_publication, saved = await self.published()
        service = self.scheduler.hello_guidance_reuse
        preview = service.reuse_preview(saved['entry_sha256'])
        original = FailureGuidanceContext.record

        def corrupt_after_record(context, state, options, call_id):
            original(context, state, options, call_id)
            path = service.directory / ('entry-' + saved['entry_sha256'] + '.json')
            path.write_text('{}')

        with patch.object(FailureGuidanceContext, 'record', corrupt_after_record):
            self.scheduler.start_hello_guidance_reuse(**self.request(preview))
            await self.scheduler.task
        self.assertEqual(self.engine.states, [])
        self.assertFalse((self.runtime.root / 'hello.txt').exists())

    async def test_tampered_admission_and_intent_report_fail_closed(self):
        unused_selection, unused_publication, saved = await self.published()
        service = self.scheduler.hello_guidance_reuse
        started = self.scheduler.start_hello_guidance_reuse(**self.request(service.reuse_preview(saved['entry_sha256'])))
        await self.complete(started)
        event = self.store.connection.execute('SELECT event_id,payload_json FROM desktop_events WHERE kind=?',
                                               (ADMISSION,)).fetchone()
        payload = json.loads(event['payload_json'])
        payload['entry_sha256'] = 'f'*64
        with self.store.connection:
            self.store.connection.execute('UPDATE desktop_events SET payload_json=? WHERE event_id=?',
                                           (json.dumps(payload), event['event_id']))
        with self.assertRaises(ValueError):
            service.report(started['job_id'])
        with self.store.connection:
            self.store.connection.execute('UPDATE desktop_events SET payload_json=? WHERE event_id=?',
                                           (event['payload_json'], event['event_id']))
        path = service.directory / ('intent-' + started['reuse_intent_sha256'] + '.json')
        path.write_text('{}')
        with self.assertRaises(ValueError):
            service.report(started['job_id'])

    async def test_stale_lease_foreign_nested_source_and_prepared_hash_rejected(self):
        unused_selection, unused_publication, saved = await self.published()
        service = self.scheduler.hello_guidance_reuse
        preview = service.reuse_preview(saved['entry_sha256'])
        for change in ({'lease_id': 'lease-foreign'}, {'generation': True}, {'confirm_sha256': 'f'*64}):
            with self.assertRaises(ValueError):
                self.scheduler.start_hello_guidance_reuse(**(self.request(preview) | change))
        foreign = copy.deepcopy(preview)
        foreign['guidance']['followup']['source_job_id'] = 'job-foreign'
        foreign['confirm_sha256'] = digest({key: value for key, value in foreign.items() if key != 'confirm_sha256'})
        with self.assertRaises(ValueError):
            self.scheduler.start_hello_guidance_reuse(**self.request(foreign))
        self.assertEqual(len(self.scheduler.status()['jobs']), 1)

    async def test_duplicate_admission_invalidates_report(self):
        unused_selection, unused_publication, saved = await self.published()
        service = self.scheduler.hello_guidance_reuse
        started = self.scheduler.start_hello_guidance_reuse(**self.request(service.reuse_preview(saved['entry_sha256'])))
        await self.complete(started)
        event = dict(self.store.connection.execute('SELECT * FROM desktop_events WHERE kind=?',
                                                  (ADMISSION,)).fetchone())
        event['event_id'] = identifier('event')
        with self.store.connection:
            self.store.insert('desktop_events', **event)
        with self.assertRaises(ValueError):
            service.report(started['job_id'])

    async def test_report_reuses_one_frozen_snapshot(self):
        unused_selection, unused_publication, saved = await self.published()
        service = self.scheduler.hello_guidance_reuse
        started = self.scheduler.start_hello_guidance_reuse(**self.request(service.reuse_preview(saved['entry_sha256'])))
        await self.complete(started)
        with patch('aos.failure_guidance.audit_snapshot', side_effect=AssertionError('second guidance snapshot forbidden')):
            with patch('aos.failure_followup.audit_snapshot', side_effect=AssertionError('second followup snapshot forbidden')):
                report = service.report(started['job_id'])
        self.assertTrue(report['entry_binding_verified'])

    async def test_private_entry_links_and_unimplemented_codes_rejected(self):
        unused_selection, unused_publication, saved = await self.published()
        service = self.scheduler.hello_guidance_reuse
        path = service.directory / ('entry-' + saved['entry_sha256'] + '.json')
        backup = path.with_suffix('.saved')
        path.rename(backup)
        path.symlink_to(backup)
        try:
            with self.assertRaises((ValueError, OSError)):
                service.reuse_preview(saved['entry_sha256'])
        finally:
            path.unlink()
            backup.rename(path)
        for code in ('repair_environment', 'request_clarification'):
            selection = await self.source(code)
            with self.assertRaises(ValueError):
                service.publish_preview(**selection)

    async def test_exact_authenticated_api_and_separate_consents(self):
        selection = await self.source()
        base = '/api/tasks/hello-guidance-reuse/'
        async with self.client() as client:
            body = {'schema_version': '1.0', **selection}
            self.assertEqual((await client.post(base + 'publish-preview', json=body)).status_code, 401)
            await client.post('/api/login', json={'token': 'synthetic-token'})
            response = await client.post(base + 'publish-preview', json=body)
            self.assertEqual(response.status_code, 200, response.text)
            publication = response.json()
            for extra in ({'extra': True}, {'schema_version': '2.0'}):
                self.assertEqual((await client.post(base + 'publish-preview', json=body | extra)).status_code, 400)
            self.assertEqual((await client.post(base + 'publish-preview?extra=1', json=body)).status_code, 400)
            self.assertEqual((await client.post(base + 'inspect', content='{"schema_version":"1.0","entry_sha256":"one","entry_sha256":"two"}')).status_code, 400)
            request = {'schema_version': '1.0', **self.request(publication)}
            self.assertEqual((await client.post(base + 'publish', json=request | {'consent': False})).status_code, 409)
            response = await client.post(base + 'publish', json=request)
            self.assertEqual(response.status_code, 200, response.text)
            entry_request = {'schema_version': '1.0', 'entry_sha256': response.json()['entry_sha256']}
            response = await client.post(base + 'reuse-preview', json=entry_request)
            self.assertEqual(response.status_code, 200, response.text)
            preview = response.json()
            response = await client.post(base + 'start', json={'schema_version': '1.0', **self.request(preview)})
            self.assertEqual(response.status_code, 200, response.text)
            started = response.json()
            await self.complete(started)
            response = await client.post(base + 'report', json={'schema_version': '1.0', 'job_id': started['job_id']})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()['entry_binding_verified'])

    async def test_canonical_synthetic_examples_and_nested_evidence_relation(self):
        from aos.dataset import validator
        models = {'hello_guidance_entry': HelloGuidanceEntry, 'hello_reuse_publication_preview': HelloReusePublicationPreview,
                  'hello_reuse_entry_response': HelloReuseEntryResponse, 'hello_reuse_preview': HelloReusePreview,
                  'hello_reuse_start': HelloReuseStart, 'hello_reuse_report': HelloReuseReport}
        for name, model in models.items():
            example = json.loads((REPO_ROOT / 'examples' / (name + '.json')).read_text())
            validator(name).validate(example)
            self.assertEqual(model.model_validate(example).model_dump(mode='json'), example)
        example = json.loads((REPO_ROOT / 'examples/hello_reuse_report.json').read_text())
        example['guidance']['guidance_applied'] = True
        with self.assertRaises(ValueError):
            HelloReuseReport.model_validate(example)
        with self.assertRaises(jsonschema.ValidationError):
            validator('hello_reuse_report').validate(example)
