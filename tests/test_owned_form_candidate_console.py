import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import httpx

from aos.contracts import REPO_ROOT, canonical, digest
from aos.desktop_console import create_console
from aos.site_skill_form_recipe_candidate import parse_site_skill_form_recipe_candidate


class OwnedFormCandidateConsoleTests(unittest.TestCase):
    def setUp(self):
        fixture = json.loads((REPO_ROOT / 'examples/site_skill_form_recipe_candidate.json').read_text())
        value = dict(fixture['candidate'], schema_version='1.1', source_context={
            'kind': 'owned_fixed_template_v1', 'manifest_sha256': 'a' * 64,
            'invocation_sha256': 'b' * 64})
        self.candidate = parse_site_skill_form_recipe_candidate(value).model_dump(mode='json')
        self.checksum = digest(self.candidate)
        self.context = {
            'schema_version': '1.0', 'available': True,
            'mode': 'owned_synthetic_form_invocation', 'lifecycle': 'audited',
            'source_run_ref': self.candidate['source_run_ref'],
            'invocation_sha256': self.candidate['source_context']['invocation_sha256'],
            'profile_sha256': self.candidate['profile_sha256'],
            'task_sha256': self.candidate['task_sha256'],
            'page_draft_sha256': self.candidate['annotation']['page_draft_sha256'],
            'task_key': self.candidate['annotation']['task_key'],
            'form_fields': ['message'],
            'annotation_seed': self.candidate['annotation'],
        }
        self.scheduler = Mock()
        self.scheduler.busy = False
        self.scheduler.reserved = False
        self.scheduler.owned_form_candidate_context.return_value = self.context
        self.session = Mock()
        self.scheduler.remote_form_owned_candidate_session = self.session
        self.scheduler.remote_form_owned_manifest = {'mode': 'owned_synthetic_form_invocation'}
        self.scheduler._owned_form_candidate_run.return_value = (self.session, 'synthetic-run')
        self.session.preview.return_value = (self.candidate, self.checksum)
        def publish(_run_ref, _invocation_sha256, _annotation, confirmation):
            if confirmation != self.checksum:
                raise ValueError('exact_recipe_candidate_confirmation_required')
            return self.candidate, self.checksum
        self.session.publish.side_effect = publish
        self.session.reinspect.return_value = (self.candidate, self.checksum)
        origin = 'http://127.0.0.1:19048'
        self.origin = origin
        assets = tempfile.TemporaryDirectory(prefix='owned-form-candidate-console-assets-')
        self.addCleanup(assets.cleanup)
        self.app = create_console(object(), 'synthetic-token', origin,
                                  Path(assets.name), scheduler=self.scheduler)

    def test_context_and_preview_are_authenticated_bounded_and_read_only(self):
        async def check():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                         base_url=self.origin,
                                         headers={'Origin': self.origin}) as client:
                endpoint = '/api/tasks/owned-form-candidate/context'
                self.assertEqual((await client.get(endpoint)).status_code, 401)
                await client.post('/api/login', json={'token': 'synthetic-token'})
                self.assertEqual((await client.get(endpoint + '?run_id=arbitrary')).status_code, 400)
                self.assertEqual((await client.request('GET', endpoint, content=b'{}')).status_code, 400)
                self.assertEqual((await client.get(endpoint)).json(), self.context)
                self.scheduler.owned_form_candidate_context.assert_called_once_with()

                payload = {'schema_version': '1.0',
                           'source_run_ref': self.context['source_run_ref'],
                           'invocation_sha256': self.context['invocation_sha256'],
                           'annotation': self.context['annotation_seed']}
                preview = await client.post('/api/tasks/owned-form-candidate/preview', json=payload)
                self.assertEqual(preview.status_code, 200)
                value = preview.json()
                self.assertEqual(value['status'], 'preview')
                self.assertIs(value['persisted'], False)
                self.assertEqual(value['candidate_sha256'], self.checksum)
                self.assertEqual(value['summary']['candidate_schema_version'], '1.1')
                self.assertIs(value['summary']['skill_executed'], False)
                self.assertEqual(value['summary']['invocation_sha256'],
                                 self.context['invocation_sha256'])
                self.assertNotIn('source_run_id', value['summary'])
                self.session.preview.assert_called_once()
                self.session.publish.assert_not_called()

        asyncio.run(check())

    def test_publish_and_inspect_require_exact_hash_and_source_pins(self):
        async def check():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                         base_url=self.origin,
                                         headers={'Origin': self.origin}) as client:
                await client.post('/api/login', json={'token': 'synthetic-token'})
                common = {'schema_version': '1.0',
                          'source_run_ref': self.context['source_run_ref'],
                          'invocation_sha256': self.context['invocation_sha256']}
                annotation = self.context['annotation_seed']
                published = await client.post('/api/tasks/owned-form-candidate/publish', json={
                    **common, 'annotation': annotation, 'confirm_sha256': self.checksum})
                self.assertEqual(published.status_code, 200)
                self.assertEqual(published.json()['status'], 'published')
                self.assertIs(published.json()['persisted'], True)
                self.session.publish.assert_called_once()

                inspected = await client.post('/api/tasks/owned-form-candidate/inspect', json={
                    **common, 'candidate_sha256': self.checksum})
                self.assertEqual(inspected.status_code, 200)
                self.assertEqual(inspected.json()['status'], 'reinspected')
                self.assertEqual(inspected.json()['candidate_sha256'], self.checksum)

                wrong = await client.post('/api/tasks/owned-form-candidate/publish', json={
                    **common, 'annotation': annotation, 'confirm_sha256': '0' * 64})
                self.assertEqual(wrong.status_code, 409)
                self.assertEqual(self.session.publish.call_args.args[3],
                                 '0' * 64)

        asyncio.run(check())

    def test_malformed_extra_duplicate_and_stale_requests_fail_closed(self):
        async def check():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                         base_url=self.origin,
                                         headers={'Origin': self.origin}) as client:
                await client.post('/api/login', json={'token': 'synthetic-token'})
                common = {'schema_version': '1.0',
                          'source_run_ref': self.context['source_run_ref'],
                          'invocation_sha256': self.context['invocation_sha256'],
                          'annotation': self.context['annotation_seed']}
                extra = await client.post('/api/tasks/owned-form-candidate/preview',
                                          json={**common, 'run_id': 'other'})
                self.assertEqual(extra.status_code, 400)
                duplicate = await client.post('/api/tasks/owned-form-candidate/preview',
                    content=(canonical(common)[:-1] + ',"source_run_ref":"' + '0' * 64 + '"}'),
                    headers={'content-type': 'application/json', 'Origin': self.origin})
                self.assertEqual(duplicate.status_code, 400)
                self.scheduler.owned_form_candidate_context.return_value = {
                    'schema_version': '1.0', 'available': False, 'reason': 'unavailable'}
                stale = await client.post('/api/tasks/owned-form-candidate/preview', json=common)
                self.assertEqual(stale.status_code, 409)
                self.session.preview.assert_not_called()

        asyncio.run(check())


if __name__ == '__main__':
    unittest.main()
