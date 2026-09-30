import json
import copy
from pathlib import Path
import tempfile
import unittest

import httpx

from aos.contracts import REPO_ROOT
from aos.desktop_console import create_console
from aos.web_application import WebApplicationProfile, WebApplicationProfiles, profile_report


class WebApplicationConsoleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='aos-web-console-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = WebApplicationProfiles(self.root / 'profiles')
        self.profile = json.loads((REPO_ROOT / 'examples/web_application_profile.json').read_text())['profile']
        self.origin = 'http://127.0.0.1:8765'
        app = create_console(object(), 'synthetic-token', self.origin, REPO_ROOT / 'computer',
                             trajectory_database=self.root / 'trajectory.sqlite', web_profiles_root=self.store.root)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=self.origin,
                                        headers={'Origin': self.origin})

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_authentication_empty_store_and_no_client_selected_path(self):
        self.assertEqual((await self.client.get('/api/web-applications')).status_code, 401)
        self.assertEqual((await self.client.post('/api/login', json={'token': 'synthetic-token'})).status_code, 200)
        response = await self.client.get('/api/web-applications')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])
        self.assertFalse(self.store.root.exists())
        self.assertEqual((await self.client.get('/api/web-applications?store=/tmp/other')).status_code, 400)

    async def test_only_canonical_draft_metadata_and_corruption_fails_closed(self):
        fixture = json.loads((REPO_ROOT / 'examples/web_application_profile.json').read_text())
        profile = WebApplicationProfile.model_validate(fixture['profile'])
        report = profile_report(profile)
        self.store.register(profile, confirm_sha256=report.profile_sha256)
        await self.client.post('/api/login', json={'token': 'synthetic-token'})
        response = await self.client.get('/api/web-applications')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [report.model_dump()])
        self.assertNotIn(profile.entry_url, response.text)
        self.assertNotIn(profile.tenant_key, response.text)
        self.assertNotIn('account_role', response.json()[0])
        (self.store.root / 'unexpected.json').write_text('{}')
        failed = await self.client.get('/api/web-applications')
        self.assertEqual(failed.status_code, 409)
        self.assertEqual(failed.json(), {'detail': 'Profile inventory unavailable'})

    async def test_preview_requires_session_and_origin_and_never_writes(self):
        payload = {'profile': self.profile}
        route = '/api/web-applications/preview'
        self.assertEqual((await self.client.post(route, json=payload)).status_code, 401)
        await self.client.post('/api/login', json={'token': 'synthetic-token'})
        self.assertEqual((await self.client.post(route, json=payload,
                                                 headers={'Origin': 'http://other.invalid'})).status_code, 403)
        self.assertEqual((await self.client.post(route, json=payload,
                                                 headers={'Host': 'other.invalid'})).status_code, 403)
        response = await self.client.post(route, json=payload)
        report = profile_report(WebApplicationProfile.model_validate(self.profile))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), report.model_dump())
        self.assertFalse(any(response.json()[field] for field in
                             ('execution_authorized', 'collection_authorized', 'training_ready', 'promotion_authorized')))
        self.assertNotIn(self.profile['entry_url'], response.text)
        self.assertNotIn(self.profile['tenant_key'], response.text)
        missing_parent = {**self.profile, 'revision': 2, 'previous_sha256': '0' * 64}
        rejected = await self.client.post(route, json={'profile': missing_parent})
        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(rejected.json(), {'detail': 'Profile lineage unavailable'})
        self.assertFalse(self.store.root.exists())
        self.assertFalse((self.root / 'trajectory.sqlite').exists())

    async def test_register_requires_exact_preview_hash_and_is_idempotent(self):
        route = '/api/web-applications/register'
        preview = profile_report(WebApplicationProfile.model_validate(self.profile))
        payload = {'profile': self.profile, 'confirm_sha256': preview.profile_sha256}
        self.assertEqual((await self.client.post(route, json=payload)).status_code, 401)
        await self.client.post('/api/login', json={'token': 'synthetic-token'})
        self.assertEqual((await self.client.post(route, json=payload,
                                                 headers={'Origin': 'http://other.invalid'})).status_code, 403)
        self.assertEqual((await self.client.post(route + '?store=/tmp/other', json=payload)).status_code, 400)
        wrong_hash = await self.client.post(route, json={**payload, 'confirm_sha256': '0' * 64})
        self.assertEqual(wrong_hash.status_code, 409)
        self.assertEqual(wrong_hash.json(), {'detail': 'Profile confirmation mismatch'})
        self.assertFalse(self.store.root.exists())
        first = await self.client.post(route, json=payload)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), preview.model_dump())
        filename = self.store.root / (preview.profile_sha256 + '.json')
        original = filename.read_bytes()
        second = await self.client.post(route, json=payload)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json(), first.json())
        self.assertEqual(filename.read_bytes(), original)
        self.assertEqual(len(list(self.store.root.iterdir())), 1)
        self.assertFalse((self.root / 'trajectory.sqlite').exists())

    async def test_invalid_or_noncanonical_payloads_fail_without_leaking_scope(self):
        await self.client.post('/api/login', json={'token': 'synthetic-token'})
        cases = [
            {'profile': {**self.profile, 'entry_url': 'https://private.example.invalid/path'}},
            {'profile': {**self.profile, 'unexpected': 'private-tenant'}},
            {'profile': {**self.profile, 'learning': {**self.profile['learning'], 'automatic_training': True}}},
            {'profile': {**self.profile, 'task_keys': list(reversed(self.profile['task_keys']))}},
            {'profile': self.profile, 'store': '/tmp/other'},
            {'profile': []},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                response = await self.client.post('/api/web-applications/preview', json=payload)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json(), {'detail': 'Invalid profile request'})
                self.assertNotIn('private', response.text)
        response = await self.client.post('/api/web-applications/preview',
                                          content='{"profile":{},"profile":{}}',
                                          headers={'Content-Type': 'application/json'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual((await self.client.post('/api/web-applications/preview?store=/tmp/other',
                                                  json={'profile': self.profile})).status_code, 400)
        oversized = await self.client.post('/api/web-applications/preview',
                                           json={'profile': self.profile, 'padding': 'x' * 4096})
        self.assertEqual(oversized.status_code, 413)
        self.assertFalse(self.store.root.exists())
        self.assertFalse((self.root / 'trajectory.sqlite').exists())

    async def test_register_rejects_unknown_fields_and_preserves_immutable_lineage(self):
        await self.client.post('/api/login', json={'token': 'synthetic-token'})
        route = '/api/web-applications/register'
        first_report = profile_report(WebApplicationProfile.model_validate(self.profile))
        base = {'profile': self.profile, 'confirm_sha256': first_report.profile_sha256}
        for payload in ({**base, 'path': '/tmp/other'},
                        {**base, 'confirm_sha256': 1},
                        {**base, 'confirm_sha256': 'x' * 64},
                        {**base, 'profile': {**self.profile, 'execution_authorized': True}}):
            response = await self.client.post(route, json=payload)
            self.assertEqual(response.status_code, 400)
            self.assertFalse(self.store.root.exists())
        self.assertEqual((await self.client.post(route, json=base)).status_code, 200)
        revision = copy.deepcopy(self.profile)
        revision['revision'] = 2
        revision['previous_sha256'] = first_report.profile_sha256
        revision['task_keys'] = ['find-record']
        second_report = profile_report(WebApplicationProfile.model_validate(revision))
        second = await self.client.post(route, json={'profile': revision,
                                                      'confirm_sha256': second_report.profile_sha256})
        self.assertEqual(second.status_code, 200)
        self.assertEqual(self.store.get(second_report.profile_sha256).previous_sha256, first_report.profile_sha256)
        preview = await self.client.post('/api/web-applications/preview', json={'profile': revision})
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json(), second_report.model_dump())
        illegal = {**revision, 'tenant_key': 'other-tenant'}
        illegal_report = profile_report(WebApplicationProfile.model_validate(illegal))
        invalid_preview = await self.client.post('/api/web-applications/preview', json={'profile': illegal})
        self.assertEqual(invalid_preview.status_code, 409)
        self.assertEqual(invalid_preview.json(), {'detail': 'Profile lineage unavailable'})
        rejected = await self.client.post(route, json={'profile': illegal,
                                                        'confirm_sha256': illegal_report.profile_sha256})
        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(rejected.json(), {'detail': 'Profile registration unavailable'})
        self.assertNotIn('other-tenant', rejected.text)
        self.assertEqual(len(list(self.store.root.iterdir())), 2)
        self.assertFalse((self.root / 'trajectory.sqlite').exists())


if __name__ == '__main__':
    unittest.main()
